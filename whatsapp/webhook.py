"""
whatsapp/webhook.py

Routes:
  GET  /webhook, /webhook/   -> Meta's one-time verification handshake
  POST /webhook, /webhook/   -> incoming messages land here
  POST /trigger-outage/{phone_number} -> manually fire the FIRST proactive
      alert message for a session sitting in current_state=START
      (this is what your scheduler/cron/outage-detection job should call
      the moment a vehicle crosses the 24h-offline threshold)

Your webhook URL (already set in Meta App dashboard):
  https://eupotamic-bryce-oversensibly.ngrok-free.dev/webhook

Note: routes are registered for BOTH "/webhook" and "/webhook/" so Meta
posting with or without a trailing slash never triggers a 307 redirect.
"""

from fastapi import APIRouter, Request, Response, HTTPException
from config import settings
from core import session_manager
from core import state_machine
from whatsapp.sender import send_message

router = APIRouter()


async def _verify_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == settings.META_VERIFY_TOKEN:
        return Response(content=challenge, status_code=200)

    return Response(content="Verification failed", status_code=403)


async def _receive_message(request: Request):
    payload = await request.json()

    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]["value"]

        if "messages" not in change:
            # delivery/read receipts etc. — nothing to do
            return {"status": "ignored"}

        message_data = change["messages"][0]
        sender_phone = message_data["from"]
        text = ""
        if "text" in message_data:
            text = message_data.get("text", {}).get("body", "").strip()
        elif "button" in message_data:
            text = message_data.get("button", {}).get("payload", "").strip() or message_data.get("button", {}).get("text", "").strip()
        elif "interactive" in message_data:
            interactive = message_data["interactive"]
            text = (
                interactive.get("button_reply", {}).get("id", "").strip()
                or interactive.get("button_reply", {}).get("title", "").strip()
                or interactive.get("list_reply", {}).get("id", "").strip()
                or interactive.get("list_reply", {}).get("title", "").strip()
            )

    except (KeyError, IndexError):
        return {"status": "ignored"}

    session = session_manager.find_session(sender_phone)

    if session is None:
        send_message(sender_phone, "Aapka koi active case nahi mila. Kripya support se contact karein.")
        return {"status": "no_session"}

    updated_session, outbound_messages = state_machine.process_message(session, text, sender_phone)
    session_manager.update_session(updated_session)

    for out in outbound_messages:
        send_message(out["phone"], out.get("text", ""), interactive=out.get("interactive"))

    return {"status": "ok"}


# registered twice (with/without trailing slash) so Meta never gets a 307
router.add_api_route("/webhook", _verify_webhook, methods=["GET"])
router.add_api_route("/webhook/", _verify_webhook, methods=["GET"])
router.add_api_route("/webhook", _receive_message, methods=["POST"])
router.add_api_route("/webhook/", _receive_message, methods=["POST"])


@router.post("/trigger-outage/{vehicle_no}")
async def trigger_outage(vehicle_no: str):
    """
    Call this to send the FIRST proactive message for a case sitting at
    current_state=START (battery/main-power/other alert with location +
    last update time). This is what a real 24h-outage scheduler would call
    automatically — it knows the VEHICLE, not the owner's phone number,
    so lookup is by vehicle_no.

        curl -X POST http://127.0.0.1:8000/trigger-outage/MH12AB1234
    """
    session = session_manager.find_session_by_vehicle(vehicle_no)
    if session is None:
        raise HTTPException(status_code=404, detail=f"No session found for vehicle {vehicle_no}")

    if session.get("current_state") != "START":
        raise HTTPException(status_code=400, detail=f"Session is already at state {session.get('current_state')}, not START")

    owner_phone = session["phone_number"]
    updated_session, outbound_messages = state_machine.process_message(session, "", owner_phone)
    session_manager.update_session(updated_session)

    for out in outbound_messages:
        send_message(out["phone"], out.get("text", ""), interactive=out.get("interactive"))

    return {"status": "sent", "vehicle_no": vehicle_no, "phone_number": owner_phone, "new_state": updated_session["current_state"]}
