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
        "vs declining/disagreeing). Return {\"value\": \"YES|NO|UNCLEAR\"}.",
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
        "wants us to contact their driver (DRIVER). Return "
        "{\"value\": \"SELF|DRIVER|UNCLEAR\"}.",
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
    result = extract_structured(
        current_state,
        "Extract a date/time mentioned (any format) and normalize it to "
        "YYYY-MM-DD if possible, else return the raw text. Return "
        "{\"value\": \"<normalized date or raw text>\"}.",
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