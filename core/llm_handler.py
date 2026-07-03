"""
core/llm_handler.py

The LLM's ONLY job in this system: look at the user's free-text reply for
the CURRENT state, and return small structured JSON. It never decides the
flow and it never writes the outgoing message — state_machine.py does that
using prompts/templates.py.

Supports 3 free-tier-friendly providers, switchable via .env LLM_PROVIDER:
  - "groq"      -> free, fast, recommended default (needs GROQ_API_KEY)
  - "gemini"    -> free tier (needs GEMINI_API_KEY)
  - "ollama"    -> 100% free, runs locally, no API key needed at all
  - "anthropic" -> paid, kept as an option if you want it later
"""

import json
import re
import requests
from datetime import date
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
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    return text


# ---------------------------------------------------------- provider calls --

def _call_groq(user_prompt: str) -> str:
    if "groq" not in _client_cache:
        from groq import Groq
        _client_cache["groq"] = Groq(api_key=settings.GROQ_API_KEY)
    client = _client_cache["groq"]

    response = client.chat.completions.create(
        model=settings.GROQ_MODEL,
        max_tokens=200,
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    return response.choices[0].message.content


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
        max_tokens=200,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return response.content[0].text


PROVIDERS = {
    "groq": _call_groq,
    "gemini": _call_gemini,
    "ollama": _call_ollama,
    "anthropic": _call_anthropic,
}


def _call_llm(user_prompt: str) -> str:
    provider_fn = PROVIDERS.get(settings.LLM_PROVIDER, _call_groq)
    return provider_fn(user_prompt)


# --------------------------------------------------------------- extraction --

def extract_structured(current_state: str, instruction: str, user_message: str) -> dict:
    """
    Generic call: pass what you want extracted, get back a dict.
    Falls back to {"value": "", "confidence": "low"} on any failure so a
    flaky LLM response never crashes the flow — state_machine.py treats
    low confidence / empty value as "ask again".
    """
    prompt = (
        f"CURRENT_STATE: {current_state}\n"
        f"INSTRUCTION: {instruction}\n"
        f"USER_MESSAGE: \"{user_message}\"\n\n"
        f"Return JSON only."
    )

    try:
        raw_text = _call_llm(prompt)
        parsed = json.loads(_strip_json_fence(raw_text))
        return parsed
    except Exception as e:
        print(f"[llm_handler] {settings.LLM_PROVIDER} call failed: {e}")
        return {"value": "", "confidence": "low"}


# ---- Small convenience wrappers used by state_machine.py ----

def classify_yes_no(current_state: str, user_message: str) -> str:
    result = extract_structured(
        current_state,
        "Classify the user's reply as YES, NO, or UNCLEAR (agreeing/confirming "
        "vs declining/disagreeing). Treat Hindi/Hinglish replies such as haan, "
        "yes, theek hai, thik hai, nahin, no, nahi, repair, fix, replace as YES/NO "
        "where appropriate. Return {\"value\": \"YES|NO|UNCLEAR\"}.",
        user_message,
    )
    return result.get("value", "UNCLEAR").upper()


def classify_wait_done_reply(current_state: str, user_message: str) -> str:
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
    )
    return result.get("value", "UNCLEAR").upper()


def classify_self_or_driver(current_state: str, user_message: str) -> str:
    result = extract_structured(
        current_state,
        "Classify whether the user wants to handle this themselves (SELF) or "
        "wants us to contact their driver (DRIVER). Treat short replies like "
        "haan/self/driver/repair as SELF or DRIVER if the meaning is clear. "
        "Return {\"value\": \"SELF|DRIVER|UNCLEAR\"}.",
        user_message,
    )
    return result.get("value", "UNCLEAR").upper()


def classify_vehicle_status(current_state: str, user_message: str) -> str:
    result = extract_structured(
        current_state,
        "Classify the vehicle status into one of: WORKSHOP, ACCIDENT, "
        "RUNNING, GPS_DAMAGED, GPS_REMOVED, UNCLEAR. Return "
        "{\"value\": \"<one of these>\"}.",
        user_message,
    )
    return result.get("value", "UNCLEAR").upper()


def extract_date(current_state: str, user_message: str) -> str:
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
    )
    return result.get("value", "")


def extract_time(current_state: str, user_message: str) -> str:
    result = extract_structured(
        current_state,
        "Extract a time or preferred visit time mentioned in the message. "
        "Normalize it to a 12-hour format like HH:MM AM/PM if possible, else "
        "return the raw text. For Hindi/Hinglish phrases like '5 baje', "
        "return '05:00 PM'. Return {\"value\": \"<normalized time or raw text>\"}.",
        user_message,
    )
    return result.get("value", "")


def extract_free_text(current_state: str, user_message: str, what: str) -> str:
    result = extract_structured(
        current_state,
        f"Extract the {what} mentioned in the message. Return "
        f"{{\"value\": \"<extracted {what}>\"}}.",
        user_message,
    )
    return result.get("value", "")


def extract_name_and_phone(current_state: str, user_message: str) -> dict:
    result = extract_structured(
        current_state,
        "Extract a person's name and a 10-digit Indian mobile number if "
        "present. Return {\"name\": \"...\", \"phone\": \"...\"}.",
        user_message,
    )
    return {"name": result.get("name", ""), "phone": result.get("phone", "")}


# ----------------------------------------- CONTEXTUAL RESPONSE GENERATION --

def generate_contextual_response(
    session: dict,
    user_message: str,
    current_state: str,
    missing_fields: list[str] = None,
    next_states: list[str] = None,
    root_cause: str = "",
) -> str:
    """
    Generate a natural, contextual Hinglish response based on the current state,
    session data, and what information is still missing.
    
    This is used for follow-up responses (never for the first message, which is
    hardcoded). The LLM understands the conversation context and generates
    replies that continue naturally instead of restarting or being robotic.
    
    Args:
        session: The current session dict with all collected information
        user_message: The user's latest reply
        current_state: The current state in the workflow
        missing_fields: List of fields still needed (e.g., ["service_date", "contact_number"])
        next_states: List of allowed next states/transitions
        root_cause: Root cause if applicable (e.g., "BATTERY_ISSUE", "MAIN_POWER_DISCONNECTED")
    
    Returns:
        A natural Hinglish response string
    """
    if not missing_fields:
        missing_fields = []
    if not next_states:
        next_states = []
    
    # Build context for LLM
    context_lines = []
    context_lines.append(f"CURRENT_STATE: {current_state}")
    context_lines.append(f"WORKFLOW_STAGE: Troubleshooting issue - {root_cause or 'Unknown'}")
    
    # Add collected information
    if session.get("vehicle_no"):
        context_lines.append(f"Vehicle: {session['vehicle_no']}")
    if session.get("current_location"):
        context_lines.append(f"Current Location: {session['current_location']}")
    if session.get("destination_location"):
        context_lines.append(f"Destination: {session['destination_location']}")
    if session.get("service_date"):
        context_lines.append(f"Service Date: {session['service_date']}")
    if session.get("service_time_window"):
        context_lines.append(f"Service Time: {session['service_time_window']}")
    if session.get("contact_person"):
        context_lines.append(f"Contact Person: {session['contact_person']}")
    if session.get("contact_number"):
        context_lines.append(f"Contact Number: {session['contact_number']}")
    if session.get("extracted_service_location"):
        context_lines.append(f"Service Location: {session['extracted_service_location']}")
    
    # Add what's still missing
    if missing_fields:
        context_lines.append(f"\nSTILL_NEEDED: {', '.join(missing_fields)}")
    
    context_str = "\n".join(context_lines)
    
    prompt = (
        f"{context_str}\n\n"
        f"USER_MESSAGE: \"{user_message}\"\n\n"
        f"Generate a SHORT, NATURAL Hinglish response that:\n"
        f"1. Acknowledges what the user said\n"
        f"2. Continues the CURRENT workflow WITHOUT restarting or changing context\n"
        f"3. Asks only for MISSING information (never repeat questions already answered)\n"
        f"4. Uses short, conversational Hinglish phrases like 'Thik hai', 'Shukriya', 'Ek aur cheez...'\n"
        f"5. NEVER greets again, NEVER asks 'Aapki problem kya hai?', NEVER starts a new conversation\n"
        f"6. For follow-up: If user confirmed something, acknowledge and ask next. If user is confused, provide brief help.\n"
        f"7. Keep response to 1-3 lines max, casual and natural\n"
        f"\n"
        f"Reply with ONLY the response text, no JSON, no formatting, no markdown."
    )
    
    try:
        raw_text = _call_llm(prompt)
        # Remove any markdown fences if present
        response = raw_text.strip()
        response = re.sub(r"^```(text|hinglish)?", "", response).strip()
        response = re.sub(r"```$", "", response).strip()
        return response if response else user_message  # Fallback
    except Exception as e:
        print(f"[llm_handler] contextual response generation failed: {e}")
        return f"Shukriya! Aapka message receive hua hai. Ek moment..."


def generate_nudge_or_help_response(
    session: dict,
    current_state: str,
    issue_type: str = "battery",
    context: str = "",
) -> str:
    """
    Generate a nudge or help message when the user is stuck or needs guidance.
    Used when user says things like "I don't know how", "How to do this?", etc.
    
    Args:
        session: Current session dict
        current_state: Current state (e.g., "WAIT_DONE", "ASK_CONTACT_NUMBER")
        issue_type: Type of issue (battery, power, etc.)
        context: Additional context about what the user is confused about
    
    Returns:
        A helpful Hinglish response
    """
    prompt = (
        f"Current State: {current_state}\n"
        f"Issue Type: {issue_type}\n"
        f"User Context: {context}\n\n"
        f"Generate a SHORT, step-by-step help message in natural Hinglish.\n"
        f"Keep it to 2-3 sentences MAX. Be friendly and practical.\n"
        f"Use phrases like 'Bilkul samajh sakte hain', 'Yeh steps follow kijiye'\n"
        f"Reply with ONLY the help text, no JSON, no formatting."
    )
    
    try:
        raw_text = _call_llm(prompt)
        response = raw_text.strip()
        response = re.sub(r"^```(text|hinglish)?", "", response).strip()
        response = re.sub(r"```$", "", response).strip()
        return response if response else "Koi baat nahi, samajh jayenge. Kripya ek baar aur try kijiye."
    except Exception as e:
        print(f"[llm_handler] help response generation failed: {e}")
        return "Koi baat nahi, samajh jayenge. Kripya ek baar aur try kijiye."


def should_continue_workflow(session: dict, user_message: str, current_state: str) -> bool:
    """
    Determine if the user is trying to continue the current workflow or
    if they've abandoned it or are confused. Helps prevent restarting
    conversations unnecessarily.
    """
    msg_lower = user_message.strip().lower()
    
    # Quick patterns for clear intentions
    if any(word in msg_lower for word in ["help", "how", "kaise", "samajh nahi", "nahi aata"]):
        return True  # User wants help with current task
    if any(word in msg_lower for word in ["done", "ho gaya", "bas", "theek"]):
        return True  # User is confirming completion
    if any(word in msg_lower for word in ["ok", "thik", "sahi", "haan", "yes"]):
        return True  # User is confirming
    if any(word in msg_lower for word in ["nahi", "no", "naa"]):
        return True  # User is declining
    
    # If we're unclear, ask LLM to classify intent
    result = extract_structured(
        current_state,
        "Is the user trying to CONTINUE the current support workflow, or are they "
        "trying to START A NEW ISSUE or CHANGE TOPICS? "
        "Return {\"value\": \"CONTINUE|NEW_ISSUE\"}.",
        user_message,
    )
    
    intent = result.get("value", "CONTINUE").upper()
    return intent == "CONTINUE"