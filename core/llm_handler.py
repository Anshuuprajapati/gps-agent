"""
core/llm_handler.py

The LLM's ONLY job in this system: look at the user's free-text reply for
the CURRENT state, and return small structured JSON. It never decides the
flow and it never writes the outgoing message — state_machine.py does that
using prompts/templates.py.

Supports providers, switchable via .env LLM_PROVIDER:
  - "bedrock"   -> OpenAI-compatible Bedrock endpoint (needs BEDROCK_API_KEY)
  - "gemini"    -> free tier (needs GEMINI_API_KEY)
  - "ollama"    -> 100% free, runs locally, no API key needed at all
  - "anthropic" -> paid, kept as an option if you want it later
"""

import json
import re
import requests
from datetime import date, datetime, timedelta
from config import settings

SYSTEM_PROMPT = (
    "You are an entity/intent extractor for a WhatsApp support bot. "
    "The bot messages are in Hindi/Hinglish. "
    "You are given the CURRENT_STATE, an INSTRUCTION describing exactly "
    "what to extract, and the USER_MESSAGE. "
    "Reply with ONLY a single valid JSON object — no prose, no markdown "
    "fences, nothing else."
)

_client_cache = {}


def _strip_json_fence(text: str) -> str:
    if text is None:
        return ""
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    return text


# ---------------------------------------------------------- provider calls --

def _call_bedrock(user_prompt: str) -> str:
    response = requests.post(
        settings.BEDROCK_BASE_URL,
        headers={
            "Authorization": f"Bearer {settings.BEDROCK_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": settings.BEDROCK_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            # gpt-oss reasoning models spend part of this budget on hidden
            # reasoning tokens before the visible JSON — 200 was too low
            # and silently truncated mid-object on longer instructions
            # (e.g. extract_tech_dispatch_slots), making json.loads() fail
            # and falling back to the crude heuristic extractor.
            "max_tokens": 800,
            "temperature": 0,
        },
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


def _call_gemini(user_prompt: str) -> str:
    if "gemini" not in _client_cache:
        import google.generativeai as genai
        genai.configure(api_key=settings.GEMINI_API_KEY)
        _client_cache["gemini"] = genai.GenerativeModel(
            settings.GEMINI_MODEL, system_instruction=SYSTEM_PROMPT
        )
    model = _client_cache["gemini"]
    response = model.generate_content(user_prompt)
    return response.text


def _call_ollama(user_prompt: str) -> str:
    # No API key, no account — just needs `ollama serve` running locally
    # and the model pulled once: `ollama pull llama3.1`
    response = requests.post(
        f"{settings.OLLAMA_BASE_URL}/api/chat",
        json={
            "model": settings.OLLAMA_MODEL,
            "stream": False,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=30,
    )
    return response.json()["message"]["content"]


def _call_anthropic(user_prompt: str) -> str:
    if "anthropic" not in _client_cache:
        from anthropic import Anthropic
        _client_cache["anthropic"] = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    client = _client_cache["anthropic"]

    response = client.messages.create(
        model=settings.ANTHROPIC_MODEL,
        max_tokens=800,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return response.content[0].text


PROVIDERS = {
    "bedrock": _call_bedrock,
    "gemini": _call_gemini,
    "ollama": _call_ollama,
    "anthropic": _call_anthropic,
}


def _call_llm(user_prompt: str) -> str:
    provider_fn = PROVIDERS.get(settings.LLM_PROVIDER, _call_bedrock)
    return provider_fn(user_prompt)


# --------------------------------------------------------------- extraction --

def extract_structured(current_state: str, instruction: str, user_message: str, conversation_context: str = "") -> dict:
    """
    Generic call: pass what you want extracted, get back a dict.
    Falls back to {"value": "", "confidence": "low"} on any failure so a
    flaky LLM response never crashes the flow — state_machine.py treats
    low confidence / empty value as "ask again".
    """
    context_prefix = f"CONVERSATION_CONTEXT:\n{conversation_context}\n\n" if conversation_context.strip() else ""
    prompt = (
        f"{context_prefix}"
        f"CURRENT_STATE: {current_state}\n"
        f"INSTRUCTION: {instruction}\n"
        f"USER_MESSAGE: \"{user_message}\"\n\n"
        f"Return JSON only."
    )

    try:
        raw_text = _call_llm(prompt)
        if raw_text is None:
            return {"value": "", "confidence": "low"}
        parsed = json.loads(_strip_json_fence(raw_text))
        return parsed
    except Exception as e:
        print(f"[llm_handler] {settings.LLM_PROVIDER} call failed: {e}")
        return {"value": "", "confidence": "low"}


# ---- Small convenience wrappers used by state_machine.py ----

def classify_yes_no(current_state: str, user_message: str, conversation_context: str = "") -> str:
    result = extract_structured(
        current_state,
        "Classify the user's reply as YES, NO, or UNCLEAR (agreeing/confirming "
        "vs declining/disagreeing). Treat Hindi/Hinglish replies such as haan, "
        "yes, theek hai, thik hai, nahin, no, nahi, repair, fix, replace as YES/NO "
        "where appropriate. Return {\"value\": \"YES|NO|UNCLEAR\"}.",
        user_message,
        conversation_context,
    )
    return result.get("value", "UNCLEAR").upper()


def classify_wait_done_reply(current_state: str, user_message: str, conversation_context: str = "") -> str:
    """
    Used while the owner/driver is mid-troubleshooting (WAIT_DONE state).
    People don't only reply "Done" — they ask how to do it, or change their
    mind and want the driver involved instead. This classifies all of that
    in one call instead of only pattern-matching the word "done".
    """
    result = extract_structured(
        current_state,
        "The user was asked to check/charge a battery or check the main "
        "power wiring, and reply 'Done' once finished. Classify their "
        "reply as one of: "
        "DONE (confirming they finished), "
        "NEED_HELP (asking how to do it, confused, or needs step-by-step "
        "guidance), "
        "WANT_DRIVER (now wants us to contact the driver instead of doing "
        "it themselves), "
        "UNCLEAR (anything else). "
        "Return {\"value\": \"DONE|NEED_HELP|WANT_DRIVER|UNCLEAR\"}.",
        user_message,
        conversation_context,
    )
    return result.get("value", "UNCLEAR").upper()


def classify_self_or_driver(current_state: str, user_message: str, conversation_context: str = "") -> str:
    result = extract_structured(
        current_state,
        "Classify whether the user wants to handle this themselves (SELF) or "
        "wants us to contact their driver (DRIVER). Treat short replies like "
        "haan/self/driver/repair as SELF or DRIVER if the meaning is clear. "
        "Return {\"value\": \"SELF|DRIVER|UNCLEAR\"}.",
        user_message,
        conversation_context,
    )
    return result.get("value", "UNCLEAR").upper()


def classify_vehicle_status(current_state: str, user_message: str, conversation_context: str = "") -> str:
    result = extract_structured(
        current_state,
        "Classify the user's message into one of: WORKSHOP, ACCIDENT, "
        "RUNNING, GPS_DAMAGED, GPS_REMOVED, DEFER_UNKNOWN, UNCLEAR. If the "
        "message says the vehicle is bad/broken/kharaab, classify as WORKSHOP "
        "if it refers to the vehicle being unusable and as GPS_DAMAGED if it "
        "explicitly mentions GPS/device/tracker being broken, damaged, or "
        "removed. Classify as DEFER_UNKNOWN if the user does not currently "
        "know the vehicle's status/location and says they will inform or "
        "update once they find out (e.g. 'pata nahi kaha hai', 'jab aayegi "
        "tab bata denge', 'baad mein confirm karunga') without giving any "
        "of the other clear statuses. "
        "Return {\"value\": \"<one of these>\"}.",
        user_message,
        conversation_context,
    )
    return result.get("value", "UNCLEAR").upper()


def extract_date(current_state: str, user_message: str, conversation_context: str = "") -> str:
    today = date.today().isoformat()
    result = extract_structured(
        current_state,
        "CURRENT_DATE: " + today + "\n"
        "Extract a date mentioned (any format) and normalize it to "
        "YYYY-MM-DD if possible, else return the raw text. If the user says "
        "relative terms like 'parso' or 'day after tomorrow', compute the "
        "actual date based on CURRENT_DATE. Return {\"value\": \"<normalized "
        "date or raw text>\"}.",
        user_message,
        conversation_context,
    )
    return result.get("value", "")


def extract_time(current_state: str, user_message: str, conversation_context: str = "") -> str:
    result = extract_structured(
        current_state,
        "Extract a time or preferred visit time mentioned in the message. "
        "Normalize it to a 12-hour format like HH:MM AM/PM if possible, else "
        "return the raw text. For Hindi/Hinglish phrases like '5 baje', "
        "return '05:00 PM'. Return {\"value\": \"<normalized time or raw text>\"}.",
        user_message,
        conversation_context,
    )
    return result.get("value", "")


def extract_free_text(current_state: str, user_message: str, what: str, conversation_context: str = "") -> str:
    result = extract_structured(
        current_state,
        f"Extract the {what} mentioned in the message. Return "
        f"{{\"value\": \"<extracted {what}>\"}}.",
        user_message,
        conversation_context,
    )
    return result.get("value", "")


def extract_name_and_phone(current_state: str, user_message: str, conversation_context: str = "") -> dict:
    result = extract_structured(
        current_state,
        "Extract a person's name and a 10-digit Indian mobile number if "
        "present. Return {\"name\": \"...\", \"phone\": \"...\"}.",
        user_message,
        conversation_context,
    )
    return {"name": result.get("name", ""), "phone": result.get("phone", "")}


def extract_booking_slots(user_message: str, conversation_context: str = "") -> dict:
    """
    Customers frequently give several booking details in one message at
    once ("Nagpur bypass ke paas hu, Pune jaana hai, kal subah 10 baje,
    contact Rahul 9876543210"). This pulls out WHICHEVER of the booking
    fields are actually present so the bot doesn't ask questions that
    were already answered — used by state_machine's bulk-extraction fast
    path (see _apply_booking_slots / _next_missing_booking_state).
    Anything not mentioned comes back as an empty string.
    """
    result = extract_structured(
        "BOOKING_FLOW",
        "The customer may have given several booking details at once in "
        "a single message: current vehicle location, destination they're "
        "heading to, a preferred service date, a preferred time window, "
        "a contact person's name, and/or a contact phone number. Extract "
        "WHICHEVER of these are actually present in USER_MESSAGE — leave "
        "anything not mentioned as an empty string, do not guess. "
        "Resolve any date mentioned to YYYY-MM-DD format relative to "
        "today. Return JSON with exactly these keys: "
        '{"current_location": "", "destination_location": "", '
        '"service_date": "", "service_time_window": "", '
        '"contact_person": "", "contact_number": ""}',
        user_message,
        conversation_context,
    )
    return {
        "current_location": (result.get("current_location") or "").strip(),
        "destination_location": (result.get("destination_location") or "").strip(),
        "service_date": (result.get("service_date") or "").strip(),
        "service_time_window": (result.get("service_time_window") or "").strip(),
        "contact_person": (result.get("contact_person") or "").strip(),
        "contact_number": (result.get("contact_number") or "").strip(),
    }


def _normalize_tech_time_window(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""

    # Handle simple forms like "11 am", "11am", "11:30 pm", "5 baje"
    time_match = re.search(r"(\d{1,2})(?::(\d{1,2}))?\s*(am|pm)?", text)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        suffix = (time_match.group(3) or "").upper()
        if suffix == "AM" and hour == 12:
            hour = 0
        elif suffix == "PM" and hour < 12:
            hour += 12

        if suffix in {"AM", "PM"}:
            display_hour = hour % 12 or 12
            return f"{display_hour:02d}:{minute:02d} {suffix}"
        return f"{hour:02d}:{minute:02d}"

    if re.search(r"\b(\d{1,2})\s*baje?\b", text):
        return text
    return ""


def _normalize_tech_date(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""

    today = date.today()
    if any(word in text for word in ["tomorrow", "kal", "parso"]):
        return (today + timedelta(days=1)).isoformat()
    if any(word in text for word in ["today", "aaj"]):
        return today.isoformat()
    if "day after tomorrow" in text or "parso ke baad" in text:
        return (today + timedelta(days=2)).isoformat()
    return ""


def _heuristic_extract_tech_dispatch_slots(user_message: str) -> dict:
    text = re.sub(r"\s+", " ", (user_message or "")).strip()
    if not text:
        return {"service_location": "", "service_date": "", "service_time_window": "", "contact_person": "", "contact_number": ""}

    # Only isolate a location when one of these English markers is actually
    # present — this heuristic only understands "send tech at/to/on/for X"
    # phrasing. Without a marker there's no reliable way to tell where the
    # location starts, so it must stay empty (letting ASK_DIRECT_TECH_LOCATION
    # ask directly) rather than guessing the whole message is the location.
    lowered = text.lower()
    location_candidate = ""
    for marker in [" at ", " on ", " for ", " to "]:
        if marker in lowered:
            location_candidate = text.split(marker, 1)[1]
            break

    if location_candidate:
        time_match = re.search(r"\b(\d{1,2})(?::(\d{1,2}))?\s*(am|pm)?\b", location_candidate, re.I)
        if time_match:
            location_candidate = location_candidate[:time_match.start()].strip()

        location_candidate = re.sub(r"^(send|tech|technician|engineer|please|plz|kindly)\b", "", location_candidate, flags=re.I)
        location_candidate = re.sub(r"^(by|at|on|for|to)\b", "", location_candidate, flags=re.I)
        location_candidate = location_candidate.strip(" ,;:-")

    service_location = location_candidate if len(location_candidate) > 1 else ""

    service_date = _normalize_tech_date(text)
    service_time_window = _normalize_tech_time_window(text)

    contact_number = ""
    phone_match = re.search(r"(\d{10,13})", text)
    if phone_match:
        contact_number = phone_match.group(1)

    contact_person = ""
    name_match = re.search(r"\b(contact|person|name)\s+([A-Za-z][A-Za-z .'-]{1,30})", text, re.I)
    if name_match:
        contact_person = name_match.group(2).strip()

    return {
        "service_location": service_location,
        "service_date": service_date,
        "service_time_window": service_time_window,
        "contact_person": contact_person,
        "contact_number": contact_number,
    }


def extract_tech_dispatch_slots(user_message: str, conversation_context: str = "") -> dict:
    today = date.today().isoformat()
    result = extract_structured(
        "DIRECT_TECH_REQUEST",
        "CURRENT_DATE: " + today + "\n"
        "The user wants a technician, engineer, or tech sent directly. "
        "Extract whichever of these are actually present in USER_MESSAGE: "
        "service location where the technician should go, preferred service "
        "date, preferred service time window, contact person's name, and/or "
        "contact phone number. If a location is mentioned, treat it as the "
        "service location. If a date is mentioned (including relative terms "
        "like 'aj'/'aaj'/today, 'kal'/tomorrow, 'parso'), resolve it to "
        "YYYY-MM-DD based on CURRENT_DATE. Leave anything not mentioned as "
        "an empty string. Do not guess. Return JSON with exactly these "
        "keys: "
        '{"service_location": "", "service_date": "", '
        '"service_time_window": "", "contact_person": "", '
        '"contact_number": ""}',
        user_message,
        conversation_context,
    )
    fallback = _heuristic_extract_tech_dispatch_slots(user_message)

    return {
        "service_location": (result.get("service_location") or "").strip() or fallback.get("service_location", "").strip(),
        "service_date": (result.get("service_date") or "").strip() or fallback.get("service_date", "").strip(),
        "service_time_window": (result.get("service_time_window") or "").strip() or fallback.get("service_time_window", "").strip(),
        "contact_person": (result.get("contact_person") or "").strip() or fallback.get("contact_person", "").strip(),
        "contact_number": (result.get("contact_number") or "").strip() or fallback.get("contact_number", "").strip(),
    }


# --------------------------------------------------------- knowledge base --

def is_general_question(current_state: str, user_message: str, conversation_context: str = "") -> str:
    """
    Decides whether the message is a reply to whatever the bot most
    recently asked (a date, yes/no, a location, "Done", etc.) or an
    unrelated general question about the service/company (working hours,
    how GPS tracking works, pricing, how to check a complaint, etc.).
    Checked once per incoming message in state_machine.process_message(),
    BEFORE the current state's handler runs, so it works the same way no
    matter which state the conversation is in.
    """
    result = extract_structured(
        current_state,
        "Decide if USER_MESSAGE is a reasonable reply to whatever the bot "
        "asked for CURRENT_STATE, or if it's instead an unrelated general "
        "question about the service/company (working hours, how GPS "
        "tracking works, pricing, complaint status, etc.) that has "
        "nothing to do with continuing the current step. "
        "Return {\"value\": \"FLOW_REPLY\"} for the former, "
        "{\"value\": \"GENERAL_QUESTION\"} for the latter. "
        "When in doubt, prefer FLOW_REPLY so the ongoing flow isn't "
        "interrupted unnecessarily.",
        user_message,
        conversation_context,
    )
    return result.get("value", "FLOW_REPLY").upper()


def classify_ticket_inquiry(current_state: str, user_message: str, conversation_context: str = "") -> str:
    """
    Detects the user asking about the status/details of a complaint or
    ticket they already have — phrasing varies too much for a keyword
    list ("kya meri koi complaint register hai", "iski details batao",
    "mera ticket ka status kya hai"). Deliberately distinct from asking
    when the engineer/technician will personally arrive — that stays a
    separate flow (see _is_engineer_inquiry in state_machine.py).
    """
    result = extract_structured(
        current_state,
        "Classify whether the user is asking about the status or details "
        "of an existing complaint/service ticket (e.g. 'kya meri koi "
        "complaint register hai', 'iski details batao', 'ticket ka status "
        "kya hai', 'meri complaint ka kya hua'). Do NOT classify as this "
        "if they're instead asking when the engineer/technician will "
        "personally arrive/call — that's a different question. "
        "Return {\"value\": \"TICKET_INQUIRY\"} if it matches, else "
        "{\"value\": \"OTHER\"}.",
        user_message,
        conversation_context,
    )
    return result.get("value", "OTHER").upper()


def is_driver_update_intent(user_message: str, conversation_context: str = "") -> bool:
    """
    Uses LLM to detect if the user is trying to update/change driver information.
    Handles patterns like 'driver ye hai', 'new driver', 'driver badal', etc.
    """
    result = extract_structured(
        "DRIVER_UPDATE_CHECK",
        "Is the user providing or updating driver information? "
        "Look for patterns like: 'driver ye hai', 'new driver name/number', "
        "'driver badal', 'ye rahe driver details', 'driver change', "
        "or simply providing a name followed by a phone number in context "
        "of updating the driver. Return {\"value\": \"YES\"} if this is a "
        "driver update/new driver info, {\"value\": \"NO\"} otherwise.",
        user_message,
        conversation_context,
    )
    return result.get("value", "").upper() == "YES"


def classify_global_intent(current_state: str, user_message: str, conversation_context: str = "") -> str:
    """
    Consolidated top-level router — replaces what used to be three separate,
    independently-called LLM classifiers (is_driver_update_intent,
    is_general_question, classify_ticket_inquiry), each firing on every
    single incoming message regardless of state. Checked once per turn in
    state_machine.process_message(), BEFORE the current state's own handler
    runs, so cross-cutting intents (updating the driver, asking about an
    existing ticket, an unrelated question) are understood the same way no
    matter which state the conversation is in.

    Downstream handlers still do their own detailed extraction from the raw
    message (e.g. _handle_driver_update_message calls extract_name_and_phone
    itself) — this call's only job is deciding WHICH handler should run.
    """
    result = extract_structured(
        current_state,
        "Classify USER_MESSAGE into exactly one of these top-level intents, "
        "regardless of what CURRENT_STATE was expecting:\n"
        "DRIVER_UPDATE — the user is providing or updating driver "
        "information (e.g. 'driver ye hai', 'naya driver Ramesh "
        "9876543210', a name followed by a phone number in context of "
        "the driver).\n"
        "TICKET_INQUIRY — asking about the status or details of an "
        "existing complaint/service ticket (e.g. 'kya meri koi complaint "
        "register hai', 'iski details batao', 'ticket ka status kya "
        "hai'). Do NOT use this if they're instead asking when the "
        "engineer/technician will personally arrive/call — that's a "
        "different, separately-handled question.\n"
        "DIRECT_TECH_DISPATCH — the user wants a technician/engineer/person "
        "sent out now to physically handle the vehicle, in any phrasing "
        "('send your person', 'ladka bhej do', 'koi bhejo', 'bandaa bhej "
        "dena', 'send someone to fix this'), whether or not they also state "
        "a location. Do NOT use this for a plain status update with no "
        "request to dispatch anyone (that's FLOW_REPLY) or a question about "
        "an existing ticket (that's TICKET_INQUIRY).\n"
        "GENERAL_QUESTION — an unrelated question about the service/"
        "company (working hours, how GPS tracking works, pricing, etc.) "
        "that has nothing to do with continuing the current step.\n"
        "FLOW_REPLY — none of the above; a normal reply to whatever "
        "CURRENT_STATE was asking for.\n"
        "When in doubt, prefer FLOW_REPLY so the ongoing flow isn't "
        "interrupted unnecessarily. "
        "Return {\"value\": \"<one of these>\"}.",
        user_message,
        conversation_context,
    )
    # `or` (not .get's default) so a real LLM failure — which comes back as
    # {"value": "", ...} from extract_structured, not a missing key — also
    # safely falls back to FLOW_REPLY instead of an empty string.
    return (result.get("value") or "FLOW_REPLY").upper()


_NO_ANSWER_FALLBACK = "Iska jawab abhi available nahi hai, hum team se check karke aapko batayenge."

_kb_cache = {"text": None}


def _load_knowledge_base() -> str:
    if _kb_cache["text"] is None:
        try:
            with open(settings.KNOWLEDGE_BASE_PATH, "r", encoding="utf-8") as f:
                _kb_cache["text"] = f.read()
        except FileNotFoundError:
            print(f"[llm_handler] knowledge base file not found at {settings.KNOWLEDGE_BASE_PATH}")
            _kb_cache["text"] = ""
    return _kb_cache["text"]


def answer_from_knowledge_base(user_message: str) -> str:
    """
    Answers a general/off-topic question using ONLY the FAQ file at
    settings.KNOWLEDGE_BASE_PATH — grounded on purpose, so it can't
    invent prices, policies, or hours it was never given. Falls back to
    a "we'll check and get back to you" reply if the file is missing,
    empty, or doesn't cover the question.
    """
    kb_text = _load_knowledge_base()
    if not kb_text:
        return _NO_ANSWER_FALLBACK

    prompt = (
        f"KNOWLEDGE_BASE:\n{kb_text}\n\n"
        f'USER_QUESTION: "{user_message}"\n\n'
        "Answer the question in Hindi/Hinglish, in 1-2 short sentences, "
        "using ONLY the information in KNOWLEDGE_BASE above — do not add "
        "anything not stated there. If the question isn't covered by "
        f'KNOWLEDGE_BASE, reply with exactly: "{_NO_ANSWER_FALLBACK}". '
        'Return {"value": "<answer text>"} as JSON.'
    )
    try:
        raw_text = _call_llm(prompt)
        parsed = json.loads(_strip_json_fence(raw_text))
        answer = (parsed.get("value") or "").strip()
        return answer or _NO_ANSWER_FALLBACK
    except Exception as e:
        print(f"[llm_handler] {settings.LLM_PROVIDER} knowledge-base call failed: {e}")
        return _NO_ANSWER_FALLBACK