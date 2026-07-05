"""
voice/caller.py

One function every other part of the app calls to actually place an
outbound phone call: place_call(to_phone).

This is the direct FastAPI/Python equivalent of your Node outbound.js:
  - same lazy-client pattern as getTwilioClient() there (env vars are
    guaranteed to be loaded by the time this file is imported)
  - same "from" number + webhook "url" that Twilio calls back once the
    person picks up — here that's PUBLIC_URL + /voice/answer
"""

from twilio.rest import Client
from config import settings

_client = None


def _get_client() -> Client:
    global _client
    if _client is None:
        if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
            raise RuntimeError("Twilio credentials are missing in environment variables")
        _client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    return _client


def _to_e164(phone: str) -> str:
    """
    The CSV stores phone numbers inconsistently — owner numbers with the
    country code but no '+' (e.g. "918882374849"), driver numbers as bare
    10-digit mobiles (e.g. "9871234560"). Twilio requires strict E.164
    ("+91XXXXXXXXXX"), so normalize whatever we're given before calling.
    Assumes India (+91) for bare 10-digit numbers — adjust if you operate
    in another country.
    """
    phone = (phone or "").strip()
    if phone.startswith("+"):
        return phone
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 10:
        return f"+91{digits}"
    return f"+{digits}"


def place_call(to_phone: str) -> dict:
    """
    Starts an outbound call to `to_phone`. Twilio will POST to
    PUBLIC_URL/voice/answer once the call connects, and that webhook (see
    voice/webhook.py) drives the rest of the conversation via TwiML —
    same role as the `url` param in outbound.js's client.calls.create().
    """
    if not settings.TWILIO_PHONE_NUMBER:
        raise RuntimeError("TWILIO_PHONE_NUMBER not configured")
    if not settings.PUBLIC_URL:
        raise RuntimeError("PUBLIC_URL (or TWILIO_WEBHOOK_URL) not configured")

    client = _get_client()

    call = client.calls.create(
        to=_to_e164(to_phone),
        from_=settings.TWILIO_PHONE_NUMBER,
        url=f"{settings.PUBLIC_URL}/voice/answer",
        method="POST",
    )
    return {"call_sid": call.sid}