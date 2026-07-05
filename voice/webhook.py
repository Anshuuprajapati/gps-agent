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
                            after every turn.
  Both return TwiML (XML), not JSON — that's how Twilio Voice works.
"""

from fastapi import APIRouter, Request, HTTPException, Form
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Gather

from core import session_manager, state_machine
from voice.caller import place_call
from voice.speech_format import to_speech

router = APIRouter(prefix="/voice")


# --------------------------------------------------------------- helpers --

def _normalize_phone(raw: str) -> str:
    """
    Session rows store phone numbers as plain digits (e.g. "918882374849"),
    but Twilio always sends To/From in E.164 with a leading '+'
    (e.g. "+918882374849"). Strip it so session lookups actually match —
    this is exactly why calls were landing on "no active case found."
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


def _say_and_gather(vr: VoiceResponse, prompt: str) -> None:
    gather = Gather(
        input="speech",
        action="/voice/gather",
        method="POST",
        speech_timeout="auto",
        language="hi-IN",
    )
    gather.say(prompt or "Kripya jawab dein.", language="hi-IN")
    vr.append(gather)
    # nothing heard at all -> ask again instead of silently hanging up
    vr.say("Mujhe kuch sunayi nahi diya.", language="hi-IN")
    vr.redirect("/voice/gather", method="POST")


def _run_turn(session: dict, message: str, calling_phone: str) -> VoiceResponse:
    updated_session, outbound_messages = state_machine.process_message(session, message, calling_phone)
    session_manager.update_session(updated_session)

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
        vr.say(prompt or "Dhanyawaad.", language="hi-IN")
        vr.hangup()
    else:
        _say_and_gather(vr, prompt)

    return vr


def _no_session_response() -> Response:
    vr = VoiceResponse()
    vr.say("Aapka koi active case nahi mila. Dhanyawaad.", language="hi-IN")
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
    session = session_manager.find_session(phone)
    if session is None:
        return _no_session_response()

    vr = _run_turn(session, "", phone)
    return Response(content=str(vr), media_type="application/xml")


@router.post("/gather")
async def gather_speech(To: str = Form(...), SpeechResult: str = Form("")):
    phone = _normalize_phone(To)
    session = session_manager.find_session(phone)
    if session is None:
        return _no_session_response()

    vr = _run_turn(session, SpeechResult, phone)
    return Response(content=str(vr), media_type="application/xml")