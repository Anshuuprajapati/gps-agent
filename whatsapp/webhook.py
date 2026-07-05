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

Reliability notes (see bug audit):
  - Meta WILL redeliver the same webhook payload on retries/timeouts —
    this is normal, not rare. _already_processed() deduplicates by the
    WhatsApp message ID so a retried delivery doesn't get processed twice
    (double ticket, driver messaged twice, etc). This is an in-memory,
    per-process cache — fine for the retry-window timescale Meta actually
    uses, but won't cover a multi-worker deployment sharing no state; a
    persisted/shared dedup store would be the next step if you scale to
    multiple workers.
  - session_manager.session_transaction() makes "find this session, run
    the state machine, write it back" one atomic per-phone operation,
    closing the race window a plain find_session()+update_session() pair
    left open between concurrent messages for the same conversation.
  - The actual processing work (LLM calls, session I/O, sending replies)
    is synchronous/blocking code. Run_in_threadpool offloads it so one
    slow request doesn't stall the event loop for every OTHER customer's
    conversation at the same time.
"""

from collections import OrderedDict

from fastapi import APIRouter, Request, Response, HTTPException
from starlette.concurrency import run_in_threadpool

from config import settings
from core import session_manager
from core import state_machine
from whatsapp.sender import send_message

router = APIRouter()

# ------------------------------------------------- webhook-retry dedup --

_MAX_SEEN_MESSAGE_IDS = 2000
_seen_message_ids: "OrderedDict[str, None]" = OrderedDict()


def _already_processed(message_id: str) -> bool:
    """LRU-ish in-memory dedup cache keyed by WhatsApp's message id."""
    if not message_id:
        return False
    if message_id in _seen_message_ids:
        _seen_message_ids.move_to_end(message_id)
        return True
    _seen_message_ids[message_id] = None
    if len(_seen_message_ids) > _MAX_SEEN_MESSAGE_IDS:
        _seen_message_ids.popitem(last=False)
    return False


async def _verify_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == settings.META_VERIFY_TOKEN:
        return Response(content=challenge, status_code=200)

    return Response(content="Verification failed", status_code=403)


def _process_incoming_message(payload: dict) -> dict:
    """
    All the actual (blocking) work for one webhook delivery. Runs inside
    a thread-pool worker (see _receive_message below) so it doesn't block
    the event loop for other requests while it's running.
    """
    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]["value"]

        if "messages" not in change:
            # delivery/read receipts etc. — nothing to do
            return {"status": "ignored"}

        message_data = change["messages"][0]
        message_id = message_data.get("id", "")
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

    if _already_processed(message_id):
        return {"status": "duplicate_ignored"}

    outbound_messages = []
    with session_manager.session_transaction(sender_phone) as session:
        if session is None:
            send_message(sender_phone, "Aapka koi active case nahi mila. Kripya support se contact karein.")
            return {"status": "no_session"}

        updated_session, outbound_messages = state_machine.process_message(session, text, sender_phone)
        if updated_session is not session:
            session.clear()
            session.update(updated_session)

    for out in outbound_messages:
        send_message(out["phone"], out.get("text", ""), interactive=out.get("interactive"))

    return {"status": "ok"}


async def _receive_message(request: Request):
    payload = await request.json()
    return await run_in_threadpool(_process_incoming_message, payload)


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