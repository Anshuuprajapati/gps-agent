"""
voice/webhook.py

The voice counterpart of whatsapp/webhook.py. Same session storage and
same core.state_machine underneath — only the wire format changes
(TwiML + spoken text instead of WhatsApp JSON + buttons).

Routes:
  POST /voice/call                     -> place an outbound call.
      Direct equivalent of your Node outbound.js `POST /call`:
      body {"to": "+91XXXXXXXXXX"} -> {"success": true, "callSid": "..."}

  POST /voice/trigger-outage/{vehicle_no}
      -> voice version of whatsapp/webhook.py's /trigger-outage route:
         calls the OWNER of a session sitting at current_state=START.

  POST /voice/answer   -> Twilio hits this the moment the call connects.
  POST /voice/gather    -> Twilio hits this with the transcribed speech
                            (and/or keypad digits) after every turn.
  Both return TwiML (XML), not JSON — that's how Twilio Voice works.

Voice-quality and recognition-accuracy notes:
  - <Say> uses a neural voice (settings.TWILIO_VOICE_NAME) instead of
    Twilio's basic default, with short <break> pauses between sentences
    so it doesn't read like a run-on wall of text.
  - <Gather> is given per-state `hints` (the handful of words/phrases
    actually expected in that state) and speechModel="phone_call", which
    together meaningfully cut down misrecognition.
  - Menu-style states (yes/no, vehicle status, handler choice, date
    options) also accept DTMF keypad input as a 100%-reliable fallback
    to speech, and the prompt tells the caller they can press a number.
  - Low-confidence transcripts are NOT trusted blindly — below
    settings.SPEECH_CONFIDENCE_THRESHOLD, the caller is asked to repeat
    instead of letting a misheard word silently corrupt the flow.
"""

import re

from fastapi import APIRouter, Request, HTTPException, Form
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Gather

from config import settings
from core import session_manager, state_machine
from core import router as engine_router
from voice.caller import place_call
from voice.speech_format import to_speech

router = APIRouter(prefix="/voice")


# ------------------------------------------------ per-state speech tuning --
# Only states with a small, known vocabulary get hints/DTMF — free-text
# states (current location, contact name, etc.) would only be hurt by
# biasing recognition toward a fixed word list.

_STATE_HINTS = {
    "ASK_HANDLER": "self, khud, main khud, driver, driver ko bula do",
    "ASK_PHYSICAL_DAMAGE": "haan, nahi, haan ji, bilkul, nahi hai, damage nahi hai",
    "ASK_VEHICLE_STATUS": "workshop, accident, chal rahi hai, gps damaged, gps removed, gps nikal diya hai",
    "DRIVER_CONFIRM": "haan, nahi",
    "ASK_SERVICE_CITY_CONFIRMATION": "haan, nahi, sahi hai, galat hai",
    "CONFIRM_SUMMARY": "haan, nahi, confirm, sahi hai",
    "WAIT_DONE": "done, ho gaya, kar diya, nahi hua, abhi nahi hua",
    "ASK_SERVICE_DATE_OPTIONS": "ek, one, do, two, teen, three",
}

# (digit -> spoken label) — used both to build the "press N for ..." prompt
# and to translate a keypad press back into the same word the state's LLM
# classifier already knows how to handle.
_STATE_MENU = {
    "ASK_HANDLER": [("1", "Aap khud"), ("2", "Driver")],
    "ASK_PHYSICAL_DAMAGE": [("1", "Haan"), ("2", "Nahi")],
    "DRIVER_CONFIRM": [("1", "Haan"), ("2", "Nahi")],
    "ASK_SERVICE_CITY_CONFIRMATION": [("1", "Haan"), ("2", "Nahi")],
    "CONFIRM_SUMMARY": [("1", "Haan"), ("2", "Nahi")],
    "ASK_VEHICLE_STATUS": [
        ("1", "Workshop me"), ("2", "Accident hua hai"), ("3", "Vehicle chal rahi hai"),
        ("4", "GPS damaged"), ("5", "GPS removed"),
    ],
    "ASK_SERVICE_DATE_OPTIONS": [("1", "2 din baad"), ("2", "4 din baad"), ("3", "Ek specific date")],
}

_DIGIT_MAP = {state: {digit: label for digit, label in menu} for state, menu in _STATE_MENU.items()}


# --------------------------------------------------------------- helpers --

def _normalize_phone(raw: str) -> str:
    """
    Session rows store phone numbers as plain digits (e.g. "918882374849"),
    but Twilio always sends To/From in E.164 with a leading '+'
    (e.g. "+918882374849"). Strip it so session lookups actually match.
    """
    return (raw or "").lstrip("+").strip()


def _speech_text(out: dict) -> str:
    """
    Voice has no WhatsApp buttons. If a bot message was built with
    _button_message() (interactive, no "text" key), read out its body
    text instead so nothing goes silently missing on a call.

    Whatever text we get is still written for WhatsApp (emojis, numbered
    lists, "Reply YES ya NO", raw digit strings) — to_speech() converts it
    into something that sounds like a person talking, not a chat message
    being read aloud.
    """
    if out.get("text"):
        raw = out["text"]
    else:
        interactive = out.get("interactive") or {}
        raw = interactive.get("body", {}).get("text", "")
    return to_speech(raw)


def _dispatch_outbound(outbound_messages: list[dict], current_call_phone: str) -> str:
    """
    A single state_machine turn can address more than one phone number at
    once (the driver-handoff steps message BOTH the owner and the driver).
    Only the person actually on this call leg can be spoken to directly —
    anyone else addressed in the same turn gets a brand-new outbound call
    instead of being spoken into the wrong ear.
    """
    prompt_parts = []
    for out in outbound_messages:
        text = _speech_text(out)
        if out["phone"] == current_call_phone:
            if text:
                prompt_parts.append(text)
        elif text:
            try:
                place_call(out["phone"])
            except Exception as e:
                print(f"[voice] failed to call {out['phone']}: {e}")
    return " ".join(prompt_parts).strip()


def _say_with_pauses(container, text: str) -> None:
    """
    Splits text into sentences and inserts a short <break> between them,
    using the configured neural voice — reads far more naturally than
    one unbroken block of text through the default TTS voice.
    """
    text = (text or "").strip()
    if not text:
        return
    say = container.say("", voice=settings.TWILIO_VOICE_NAME, language=settings.TWILIO_VOICE_LANGUAGE)
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    for i, sentence in enumerate(sentences):
        say.append(sentence)
        if i < len(sentences) - 1:
            say.break_(time="300ms")


def _say_and_gather(vr: VoiceResponse, prompt: str, state: str) -> None:
    """
    Builds the <Gather> for whatever state the caller is about to reply
    to. Menu-style states get a spoken "press N for ..." add-on and accept
    DTMF alongside speech; every state gets hints (if any are defined) and
    the phone-call-tuned speech model.
    """
    menu = _STATE_MENU.get(state)
    if menu:
        menu_phrase = ", ".join(f"{digit} ke liye {label}" for digit, label in menu)
        prompt = f"{prompt} Bolkar bataiye, ya keypad se number dabaiye — {menu_phrase}."

    gather_kwargs = dict(
        input="speech dtmf" if menu else "speech",
        action="/voice/gather",
        method="POST",
        speech_timeout="auto",
        language=settings.TWILIO_VOICE_LANGUAGE,
        speech_model=settings.TWILIO_SPEECH_MODEL,
    )
    hints = _STATE_HINTS.get(state)
    if hints:
        gather_kwargs["hints"] = hints
    if menu:
        gather_kwargs["num_digits"] = 1

    gather = Gather(**gather_kwargs)
    _say_with_pauses(gather, prompt or "Kripya jawab dein.")
    vr.append(gather)

    # nothing heard/pressed at all -> ask again instead of silently hanging up
    _say_with_pauses(vr, "Mujhe kuch sunayi nahi diya.")
    vr.redirect("/voice/gather", method="POST")


def _is_low_confidence(confidence: str) -> bool:
    if not confidence:
        return False  # Twilio didn't give us a score — don't second-guess it
    try:
        return float(confidence) < settings.SPEECH_CONFIDENCE_THRESHOLD
    except ValueError:
        return False


def _run_turn(session: dict, message: str, calling_phone: str) -> VoiceResponse:
    updated_session, outbound_messages = engine_router.process_message(session, message, calling_phone)
    if updated_session is not session:
        session.clear()
        session.update(updated_session)

    prompt = _dispatch_outbound(outbound_messages, calling_phone)
    vr = VoiceResponse()

    # if the owner just handed the case to a driver, THIS leg (owner's
    # call) is done — the driver gets their own fresh call from
    # _dispatch_outbound above, they don't share this one.
    owner_just_handed_off = (
        updated_session.get("handler") == "DRIVER"
        and calling_phone == updated_session.get("phone_number")
    )

    if updated_session.get("current_state") == "COMPLETED" or owner_just_handed_off:
        _say_with_pauses(vr, prompt or "Dhanyawaad.")
        vr.hangup()
    else:
        _say_and_gather(vr, prompt, updated_session.get("current_state") or "")

    return vr


def _no_session_response() -> Response:
    vr = VoiceResponse()
    _say_with_pauses(vr, "Aapka koi active case nahi mila. Dhanyawaad.")
    vr.hangup()
    return Response(content=str(vr), media_type="application/xml")


# ------------------------------------------------------- outbound calling --

@router.post("/call")
async def start_call(request: Request):
    """
    Direct equivalent of outbound.js's `POST /call`.
    Body: {"to": "+91XXXXXXXXXX"}
    """
    body = await request.json()
    to = body.get("to")

    if not to:
        raise HTTPException(status_code=400, detail="`to` phone number is required")

    try:
        result = place_call(to)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"success": True, "callSid": result["call_sid"]}


@router.post("/trigger-outage/{vehicle_no}")
async def trigger_outage_call(vehicle_no: str):
    """
    curl -X POST http://127.0.0.1:8000/voice/trigger-outage/MH12AB1234
    """
    session = session_manager.find_session_by_vehicle(vehicle_no)
    if session is None:
        raise HTTPException(status_code=404, detail=f"No session found for vehicle {vehicle_no}")

    if session.get("current_state") != "START":
        raise HTTPException(
            status_code=400,
            detail=f"Session is already at state {session.get('current_state')}, not START",
        )

    try:
        result = place_call(session["phone_number"])
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"status": "calling", "vehicle_no": vehicle_no, "call_sid": result["call_sid"]}


# ------------------------------------------------------------- TwiML loop --

@router.post("/answer")
async def answer_call(To: str = Form(...)):
    phone = _normalize_phone(To)
    with session_manager.session_transaction(phone) as session:
        if session is None:
            return _no_session_response()
        vr = _run_turn(session, "", phone)
    return Response(content=str(vr), media_type="application/xml")


@router.post("/gather")
async def gather_speech(
    To: str = Form(...),
    SpeechResult: str = Form(""),
    Confidence: str = Form(""),
    Digits: str = Form(""),
):
    phone = _normalize_phone(To)
    with session_manager.session_transaction(phone) as session:
        if session is None:
            return _no_session_response()

        pre_state = session.get("current_state") or ""

        if Digits:
            # keypad press -> translate back into the word/phrase the state's
            # LLM classifier already knows how to handle (falls back to the
            # raw digit if this state has no menu, harmless either way).
            message = _DIGIT_MAP.get(pre_state, {}).get(Digits.strip(), Digits.strip())
        else:
            message = SpeechResult
            if message and _is_low_confidence(Confidence):
                vr = VoiceResponse()
                _say_and_gather(
                    vr,
                    "Maaf kijiye, sahi se sunayi nahi diya.",
                    pre_state,
                )
                return Response(content=str(vr), media_type="application/xml")

        vr = _run_turn(session, message, phone)
    return Response(content=str(vr), media_type="application/xml")