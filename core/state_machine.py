"""
core/state_machine.py

This is the boss of the whole agent. The LLM never decides what happens
next — this file does, based on `current_state`. The LLM is only called
inside a handler to interpret free text (yes/no, a date, a name...).

Every handler has the same shape:
    handle_XXX(session: dict, message: str, sender_phone: str) -> (session, outbound)

outbound is a list of {"phone": ..., "text": ...} messages to send.
Usually it's just one message back to whoever texted — but for the
driver handoff we need to message TWO numbers at once, hence the list.
"""

import re
from datetime import datetime, timedelta
from core import llm_handler as llm
from services import gps_service, driver_service, ticket_service
from services.gps_service import BATTERY_ISSUE, MAIN_POWER_DISCONNECTED
from prompts.templates import render

PHONE_RE = re.compile(r"(\d{10,13})")


def _normalize_indian_phone(raw_digits: str) -> str:
    """
    Meta reports an incoming WhatsApp sender's number WITH the country
    code and no '+' (e.g. "919876543210"). A driver's number typed into
    chat by the owner is usually just the bare 10-digit mobile number.
    Without normalizing at the point of capture, the driver's own future
    messages would never match this session — find_session() does an
    exact string match against driver_phone, and "9876543210" !=
    "919876543210" — the driver would hit "no active case found."
    """
    digits = re.sub(r"\D", "", raw_digits or "")
    if len(digits) == 10:
        return "91" + digits
    return digits


def _msg(phone, text="", interactive: dict | None = None):
    message = {"phone": phone}
    if interactive is not None:
        message["interactive"] = interactive
    else:
        message["text"] = text
    return message


def _button_message(phone, body_text: str, buttons: list[tuple[str, str]]):
    return _msg(
        phone,
        interactive={
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": payload, "title": title}}
                    for payload, title in buttons
                ]
            },
        },
    )


def _vehicle_status_options_message(phone: str):
    return _button_message(
        phone,
        render("VEHICLE_STATUS_OPTIONS"),
        [
            ("PAYLOAD_WORKSHOP", "Workshop"),
            ("PAYLOAD_ACCIDENT", "Accident"),
            ("PAYLOAD_RUNNING", "Running"),
        ],
    )


def _normalize_payload(message: str) -> str:
    payload = message.strip().upper()
    if payload.startswith("PAYLOAD_"):
        return payload
    return ""


def _looks_like_gps_damage(message: str) -> bool:
    text = message.lower()
    has_gps_terms = bool(re.search(r"\b(gps|tracker|telematics|device|antenna)\b", text))
    has_damage_terms = bool(re.search(r"\b(kharab|khrab|kharaab|toot|broken|damaged|damage|repair|replace|fault)\b", text))
    return has_gps_terms and has_damage_terms


def _looks_like_vehicle_issue(message: str) -> bool:
    text = message.lower()
    has_vehicle_terms = bool(re.search(r"\b(gaadi|gadi|vehicle|car|truck|bus)\b", text))
    has_problem_terms = bool(re.search(r"\b(kharab|khrab|kharaab|toot|broken|problem|repair|replace|damage|damaged|fault)\b", text))
    has_gps_terms = bool(re.search(r"\b(gps|tracker|telematics|device|antenna)\b", text))
    return has_vehicle_terms and has_problem_terms and not has_gps_terms


def _is_driver_request(message: str) -> bool:
    text = message.strip().lower()
    return bool(re.search(
        r"\b(driver\s+se\s+baat\s+karao|driver\s+se\s+baat|driver\s+se\s+contact|driver\s+ko\s+bulao|driver\s+ko\s+bhejo|driver\s+ko\s+bhej|driver\s+ko\s+call|driver\s+ko\s+phone|driver\s+se)\b",
        text,
    ))


def _is_driver_change_request(message: str) -> bool:
    text = message.strip().lower()
    return bool(re.search(
        r"\b(driver\s+(change|badal|naya|new|alag)|naya\s+driver|change\s+driver|different\s+driver)\b",
        text,
    ))
    
def _is_direct_tech_request(message: str) -> bool:
    text = message.strip().lower()
    return bool(re.search(
        r"\b(send\s+(tech|technician|engineer)|send\s+engineer|send\s+technician|tech\s+bhejo|technician\s+bhejo|engineer\s+bhejo|front\s+se\s+(tech|technician|engineer)\s+bhejo|front\s+se\s+tech|front\s+se\s+technician|front\s+se\s+engineer)\b",
        text,
    ))

def _direct_tech_location_prompt() -> str:
    return "Technician kis location par bhejna hai?"


def get_service_date_prompt() -> str:
    text, _ = get_service_date_prompt_and_date()
    return text


def get_service_date_prompt_and_date() -> tuple[str, str]:
    """
    Returns both the question text AND the exact date it refers to.
    Needed because handle_ask_service_date's "yes" branch used to
    re-derive "aaj vs kal" from datetime.now() a second time, independent
    of what was actually shown to the customer — if the hour ticks past
    the 19:00 cutoff between question and reply, "yes" would silently
    get interpreted as confirming a different day than the one asked
    about. Resolving the date once, here, and reusing it removes that
    inconsistency.
    """
    now = datetime.now()
    if now.hour < 19:
        return "Kya aaj service book kar dein?", now.date().isoformat()
    return "Kya kal service book kar dein?", (now.date() + timedelta(days=1)).isoformat()


def get_service_date_options_prompt() -> str:
    return (
        "Please choose one option from below:\n"
        "1️⃣ Book service after 2 days\n"
        "2️⃣ Book service after 4 days\n"
        "3️⃣ Enter a specific date or tell after how many days..."
    )


def add_days_to_today(days: int) -> str:
    return (datetime.now().date() + timedelta(days=days)).isoformat()


_CONVERSATION_MEMORY_LIMIT = 5


def _compact_text(text: str, limit: int = 180) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _conversation_memory_lines(session: dict) -> list[str]:
    summary = str(session.get("conversation_summary", "") or "").strip()
    if not summary:
        return []
    lines = [line.strip() for line in summary.splitlines() if line.strip()]
    return lines[-_CONVERSATION_MEMORY_LIMIT:]
    
def _normalize_ticket_location(session: dict, slots: dict) -> str:
    return (
        (session.get("extracted_service_location") or "").strip()
        or (slots.get("service_location") or "").strip()
        or (session.get("destination_location") or "").strip()
        or (session.get("current_location") or "").strip()
        or (session.get("last_location") or "").strip()
        or ""
    )

def _apply_direct_tech_slots(session: dict, slots: dict) -> None:
    service_location = (slots.get("service_location") or "").strip()
    if service_location and not session.get("extracted_service_location"):
        session["extracted_service_location"] = service_location

    if slots.get("service_date") and not session.get("service_date"):
        session["service_date"] = slots["service_date"]

    if slots.get("service_time_window") and not session.get("service_time_window"):
        session["service_time_window"] = slots["service_time_window"]

    if slots.get("contact_person") and not session.get("contact_person"):
        session["contact_person"] = slots["contact_person"]

    phone_match = PHONE_RE.search(slots.get("contact_number") or "")
    if phone_match and not session.get("contact_number"):
        session["contact_number"] = phone_match.group(1)

    if not session.get("current_location"):
        inferred_location = session.get("last_location") or service_location
        if inferred_location:
            session["current_location"] = inferred_location

def _next_missing_direct_tech_state(session: dict) -> str:
    if not session.get("extracted_service_location"):
        return "ASK_DIRECT_TECH_LOCATION"
    if not session.get("service_date"):
        return "ASK_SERVICE_DATE"
    if not session.get("service_time_window"):
        return "ASK_SERVICE_TIME_WINDOW"
    if session.get("driver_name") and session.get("driver_phone"):
        return ""
    if not session.get("contact_person"):
        return "ASK_CONTACT_PERSON"
    if not session.get("contact_number"):
        return "ASK_CONTACT_NUMBER"
    return ""

def _prompt_for_direct_tech_state(session: dict, state: str) -> str:
    if state == "ASK_DIRECT_TECH_LOCATION":
        return _direct_tech_location_prompt()
    if state == "ASK_SERVICE_DATE":
        prompt_text, implied_date = get_service_date_prompt_and_date()
        session["pending_quick_date"] = implied_date
        return prompt_text
    if state == "ASK_SERVICE_TIME_WINDOW":
        return render("ASK_SERVICE_TIME_WINDOW")
    if state == "ASK_CONTACT_PERSON":
        return render("ASK_CONTACT_PERSON")
    if state == "ASK_CONTACT_NUMBER":
        return render("ASK_CONTACT_NUMBER")
    return render("ASK_CONTACT_PERSON")

def _handle_direct_tech_request(session: dict, message: str, sender_phone: str):
    conversation_context = build_conversation_context(session)
    slots = llm.extract_tech_dispatch_slots(message, conversation_context)
    _apply_direct_tech_slots(session, slots)

    next_state = _next_missing_direct_tech_state(session)
    if next_state:
        session["current_state"] = next_state
        return session, [_msg(sender_phone, _prompt_for_direct_tech_state(session, next_state))]

    if not session.get("current_location"):
        session["current_location"] = session.get("last_location") or session.get("extracted_service_location") or ""

    if not session.get("contact_person"):
        session["contact_person"] = session.get("driver_name") or "NOT_PROVIDED"
    if not session.get("contact_number"):
        session["contact_number"] = session.get("driver_phone") or "NOT_PROVIDED"

    session["current_state"] = "COMPLETED"
    ticket = ticket_service.create_ticket(session)
    outbound = _ticket_confirmation_messages(session, ticket, sender_phone)
    return session, outbound


def _handle_driver_change_request(session: dict, sender_phone: str):
    """When user says 'driver change hai', ask for new driver details."""
    session["current_state"] = "ASK_NEW_DRIVER"
    return session, [_msg(sender_phone, render("ASK_NEW_DRIVER"))]


def _handle_driver_update_message(session: dict, message: str, sender_phone: str):
    """
    When user provides driver info (via LLM intent detection),
    extract name+phone and update the session.
    Then continue with the current flow.
    """
    conversation_context = build_conversation_context(session)
    extracted = llm.extract_name_and_phone(session.get("current_state", "START"), message, conversation_context)
    phone_match = PHONE_RE.search(extracted.get("phone", "") or message)

    if extracted.get("name") and phone_match:
        driver_phone = _normalize_indian_phone(phone_match.group(1))
        session = driver_service.update_driver_details(session, extracted["name"], driver_phone)
        outbound = [_msg(sender_phone, render("SHOW_DRIVER_DETAILS", driver_name=session["driver_name"], driver_phone=session["driver_phone"]))]
        
        current_state = session.get("current_state")
        if current_state in {"ASK_VEHICLE_STATUS", "ASK_VEHICLE_STATUS_AFTER_CLOSE"}:
            outbound.append(_msg(sender_phone, render("ASK_VEHICLE_STATUS")))
            outbound.append(_vehicle_status_options_message(sender_phone))
        
        return session, outbound
    
    return session, [_msg(sender_phone, "Naam aur 10-digit mobile number dono bhejein, jaise: Ramesh 9876543210")]


def build_conversation_context(session: dict) -> str:
    snapshot_fields = [
        "vehicle_no",
        "current_state",
        "handler",
        "vehicle_state",
        "root_cause",
        "last_location",
        "current_location",
        "destination_location",
        "extracted_service_location",
        "service_date",
        "service_time_window",
        "contact_person",
        "contact_number",
        "ticket_id",
    ]

    snapshot_lines = []
    for field in snapshot_fields:
        value = str(session.get(field, "") or "").strip()
        if value:
            snapshot_lines.append(f"{field}: {value}")

    memory_lines = _conversation_memory_lines(session)

    parts = ["SESSION_SNAPSHOT:"]
    parts.extend(snapshot_lines or ["(empty)"])
    parts.append("")
    parts.append("RECENT_CONVERSATION:")
    parts.extend(memory_lines or ["(none)"])
    return "\n".join(parts)


def record_conversation_turn(session: dict, user_message: str, outbound_messages: list[dict] | None) -> dict:
    memory_lines = _conversation_memory_lines(session)

    user_text = _compact_text(user_message)
    if user_text:
        memory_lines.append(f"USER: {user_text}")

    for out in outbound_messages or []:
        text = out.get("text") or out.get("interactive", {}).get("body", {}).get("text", "")
        text = _compact_text(text)
        if text:
            memory_lines.append(f"BOT: {text}")

    session["conversation_summary"] = "\n".join(memory_lines[-_CONVERSATION_MEMORY_LIMIT:])
    return session


# ---------------------------------------------------------------- START ----

def handle_start(session, message, sender_phone):
    root_cause = gps_service.analyze_root_cause(session)
    session["root_cause"] = root_cause

    location = session.get("last_location") or session.get("current_location") or "N/A"
    last_update = session.get("gpstime") or session.get("timestamp") or "N/A"

    if root_cause == BATTERY_ISSUE:
        session["current_state"] = "ASK_HANDLER"
        text = render("BATTERY_ALERT", vehicle_no=session["vehicle_no"], location=location, last_update=last_update)
        return session, [_button_message(sender_phone, text, [("PAYLOAD_SELF", "Self"), ("PAYLOAD_DRIVER", "Driver")])]

    if root_cause == MAIN_POWER_DISCONNECTED:
        session["current_state"] = "ASK_HANDLER"
        text = render("MAIN_POWER_ALERT", vehicle_no=session["vehicle_no"], location=location, last_update=last_update)
        return session, [_button_message(sender_phone, text, [("PAYLOAD_SELF", "Self"), ("PAYLOAD_DRIVER", "Driver")])]

    # root cause unknown from telemetry alone -> ask vehicle status directly
    session["current_state"] = "ASK_VEHICLE_STATUS"
    text = render("OTHER_ALERT", vehicle_no=session["vehicle_no"], location=location, last_update=last_update)
    return session, [_msg(sender_phone, text)]


def _start_driver_handoff(session, sender_phone):
    """
    Shared by ASK_HANDLER (owner picks DRIVER upfront) and WAIT_DONE
    (owner changes their mind mid-troubleshooting and wants the driver
    involved instead). Same behavior either way: show driver details on
    file, or ask for new ones if none saved.
    """
    details = driver_service.get_driver_details(session)
    if details["phone"]:
        session["current_state"] = "DRIVER_CONFIRM"
        text = render("SHOW_DRIVER_DETAILS", driver_name=details["name"], driver_phone=details["phone"])
        return session, [_button_message(sender_phone, text, [("PAYLOAD_YES", "YES"), ("PAYLOAD_NO", "NO")])]
    else:
        session["current_state"] = "ASK_NEW_DRIVER"
        return session, [_msg(sender_phone, "Driver ka naam aur mobile number bhejein.")]


# ------------------------------------------------------------ ASK_HANDLER --

def handle_ask_handler(session, message, sender_phone):
    conversation_context = build_conversation_context(session)
    payload = _normalize_payload(message)
    if payload == "PAYLOAD_SELF":
        choice = "SELF"
    elif payload == "PAYLOAD_DRIVER":
        choice = "DRIVER"
    else:
        choice = llm.classify_self_or_driver(session["current_state"], message, conversation_context)

    if choice == "SELF":
        session["handler"] = "OWNER"
        session["current_state"] = "WAIT_DONE"
        key = "ASK_CHECK_BATTERY" if session["root_cause"] == BATTERY_ISSUE else "ASK_CHECK_POWER"
        return session, [_msg(sender_phone, render(key))]

    if choice == "DRIVER":
        return _start_driver_handoff(session, sender_phone)

    text = render("BATTERY_ALERT", vehicle_no=session["vehicle_no"], location=session.get("last_location") or session.get("current_location") or "N/A", last_update=session.get("gpstime") or session.get("timestamp") or "N/A") if session["root_cause"] == BATTERY_ISSUE else render("MAIN_POWER_ALERT", vehicle_no=session["vehicle_no"], location=session.get("last_location") or session.get("current_location") or "N/A", last_update=session.get("gpstime") or session.get("timestamp") or "N/A")
    return session, [_button_message(sender_phone, render("FALLBACK") + "\nReply: SELF ya DRIVER", [("PAYLOAD_SELF", "Self"), ("PAYLOAD_DRIVER", "Driver")])]


# ----------------------------------------------------------- DRIVER_CONFIRM

def handle_driver_confirm(session, message, sender_phone):
    conversation_context = build_conversation_context(session)
    payload = _normalize_payload(message)
    if payload == "PAYLOAD_YES":
        answer = "YES"
    elif payload == "PAYLOAD_NO":
        answer = "NO"
    else:
        answer = llm.classify_yes_no(session["current_state"], message, conversation_context)

    if answer == "YES":
        session = driver_service.transfer_to_driver(session)
        session["current_state"] = "WAIT_DONE"
        owner_msg = _msg(session["phone_number"], render("TRANSFER_DONE_OWNER"))
        key = "ASK_CHECK_BATTERY" if session["root_cause"] == BATTERY_ISSUE else "ASK_CHECK_POWER"
        driver_intro = render("TRANSFER_DONE_DRIVER", driver_name=session["driver_name"], vehicle_no=session["vehicle_no"])
        driver_msg = _msg(session["driver_phone"], driver_intro + "\n" + render(key))
        return session, [owner_msg, driver_msg]

    if answer == "NO":
        session["current_state"] = "ASK_NEW_DRIVER"
        return session, [_msg(sender_phone, "Thik hai, naye driver ka naam aur mobile number bhejein.")]

    text = render("SHOW_DRIVER_DETAILS", driver_name=session.get("driver_name", ""), driver_phone=session.get("driver_phone", ""))
    return session, [_button_message(sender_phone, text, [("PAYLOAD_YES", "YES"), ("PAYLOAD_NO", "NO")])]


# ------------------------------------------------------------ ASK_NEW_DRIVER

def handle_ask_new_driver(session, message, sender_phone):
    conversation_context = build_conversation_context(session)
    extracted = llm.extract_name_and_phone(session["current_state"], message, conversation_context)
    phone_match = PHONE_RE.search(extracted.get("phone", "") or message)

    if extracted.get("name") and phone_match:
        driver_phone = _normalize_indian_phone(phone_match.group(1))
        session = driver_service.update_driver_details(session, extracted["name"], driver_phone)
        session = driver_service.transfer_to_driver(session)
        session["current_state"] = "WAIT_DONE"
        owner_msg = _msg(session["phone_number"], render("TRANSFER_DONE_OWNER"))
        key = "ASK_CHECK_BATTERY" if session["root_cause"] == BATTERY_ISSUE else "ASK_CHECK_POWER"
        driver_intro = render("TRANSFER_DONE_DRIVER", driver_name=session["driver_name"], vehicle_no=session["vehicle_no"])
        driver_msg = _msg(session["driver_phone"], driver_intro + "\n" + render(key))
        return session, [owner_msg, driver_msg]

    return session, [_msg(sender_phone, "Naam aur 10-digit mobile number dono bhejein, jaise: Ramesh 9876543210")]


# --------------------------------------------------------------- WAIT_DONE --

def _finish_wait_done(session, sender_phone):
    """Runs the actual GPS re-check once someone confirms they're done."""
    if gps_service.verify_gps(session):
        session["current_state"] = "COMPLETED"
        return session, [_msg(sender_phone, render("GPS_FIXED_CLOSE"))]

    if not gps_service.is_power_issue_resolved(session, session["root_cause"]):
        session["current_state"] = "ASK_PHYSICAL_DAMAGE"
        damage_prompt = "ASK_PHYSICAL_DAMAGE_MAIN_POWER" if session["root_cause"] == MAIN_POWER_DISCONNECTED else "ASK_PHYSICAL_DAMAGE"
        return session, [_button_message(sender_phone, render(damage_prompt), [("PAYLOAD_YES", "YES"), ("PAYLOAD_NO", "NO")])]

    session["current_state"] = "ASK_VEHICLE_STATUS"
    return session, [_msg(sender_phone, render("ASK_VEHICLE_STATUS"))]


def handle_wait_done(session, message, sender_phone):
    conversation_context = build_conversation_context(session)
    # fast path — no LLM call needed for the exact expected reply
    if message.strip().lower() == "done":
        return _finish_wait_done(session, sender_phone)

    intent = llm.classify_wait_done_reply(session["current_state"], message, conversation_context)

    if intent == "DONE":
        return _finish_wait_done(session, sender_phone)

    if intent == "NEED_HELP":
        key = "BATTERY_HELP_STEPS" if session["root_cause"] == BATTERY_ISSUE else "MAIN_POWER_HELP_STEPS"
        return session, [_msg(sender_phone, render(key))]

    if intent == "WANT_DRIVER":
        return _start_driver_handoff(session, sender_phone)

    if re.search(r"\b(kharab|toot|broken|repair|replace|damage|damaged|fault)\b", message.lower()):
        session["current_state"] = "ASK_PHYSICAL_DAMAGE"
        damage_prompt = "ASK_PHYSICAL_DAMAGE_MAIN_POWER" if session["root_cause"] == MAIN_POWER_DISCONNECTED else "ASK_PHYSICAL_DAMAGE"
        return session, [_button_message(sender_phone, render(damage_prompt), [("PAYLOAD_YES", "YES"), ("PAYLOAD_NO", "NO")])]

    return session, [_msg(sender_phone, render("WAIT_DONE_NUDGE"))]


# --------------------------------------------------------- ASK_PHYSICAL_DAMAGE

def handle_ask_physical_damage(session, message, sender_phone):
    conversation_context = build_conversation_context(session)
    payload = _normalize_payload(message)
    if payload == "PAYLOAD_YES":
        answer = "YES"
    elif payload == "PAYLOAD_NO":
        answer = "NO"
    else:
        answer = llm.classify_yes_no(session["current_state"], message, conversation_context)

    if answer == "YES":
        session["physical_damage"] = "YES"
        session["current_state"] = "ASK_CURRENT_LOCATION"
        return session, [_msg(sender_phone, render("ASK_CURRENT_LOCATION"))]

    if answer == "NO":
        session["current_state"] = "WAIT_DONE"
        key = "ASK_CHECK_BATTERY" if session["root_cause"] == BATTERY_ISSUE else "ASK_CHECK_POWER"
        return session, [_msg(sender_phone, "Thik hai, ek baar aur try kijiye. " + render(key))]

    damage_prompt = "ASK_PHYSICAL_DAMAGE_MAIN_POWER" if session["root_cause"] == MAIN_POWER_DISCONNECTED else "ASK_PHYSICAL_DAMAGE"
    return session, [_button_message(sender_phone, render(damage_prompt), [("PAYLOAD_YES", "YES"), ("PAYLOAD_NO", "NO")])]


# ---------------------------------------------------------- ASK_VEHICLE_STATUS

def _build_booking_summary(session: dict) -> str:
    return render(
        "BOOKING_SUMMARY",
        current_location=session.get("current_location", ""),
        service_location=session.get("extracted_service_location", ""),
        service_date=session.get("service_date", ""),
        service_time=session.get("service_time_window", session.get("service_time", "")),
        contact_person=session.get("contact_person", ""),
        contact_number=session.get("contact_number", ""),
    )


def _ticket_confirmation_messages(session: dict, ticket: dict, sender_phone: str) -> list[dict]:
    if ticket.get("existing_ticket"):
        session["ticket_id"] = ticket.get("ticket_id", session.get("ticket_id", ""))
        session["engineer_id"] = ticket.get("engineer_id", session.get("engineer_id", ""))
        session["current_state"] = "COMPLETED"
        return [_msg(sender_phone, render("PREVIOUS_TICKET_FOUND"))]

    session["ticket_id"] = ticket["ticket_id"]
    session["engineer_id"] = ticket["engineer_id"]
    session["current_state"] = "COMPLETED"

    summary_text = _build_booking_summary(session)
    ticket_text = render(
        "BOOKING_CONFIRMED",
        ticket_id=ticket["ticket_id"],
        engineer_name=ticket["engineer_name"],
        engineer_phone=ticket["engineer_phone"],
    )
    return [_msg(sender_phone, summary_text), _msg(sender_phone, ticket_text)]


def _create_and_confirm_ticket_directly(session: dict, sender_phone: str):
    """Create ticket directly without confirmation step. Shows summary then ticket."""
    ticket = ticket_service.create_ticket(session)
    outbound = _ticket_confirmation_messages(session, ticket, sender_phone)
    return session, outbound


def handle_ask_vehicle_status(session, message, sender_phone):
    conversation_context = build_conversation_context(session)
    payload = _normalize_payload(message)
    if payload == "PAYLOAD_WORKSHOP":
        status = "WORKSHOP"
    elif payload == "PAYLOAD_ACCIDENT":
        status = "ACCIDENT"
    elif payload == "PAYLOAD_RUNNING":
        status = "RUNNING"
    elif payload == "PAYLOAD_GPS":
        status = "GPS_DAMAGED"
    elif payload == "PAYLOAD_VEHICLE":
        status = "WORKSHOP"
    elif _looks_like_gps_damage(message):
        status = "GPS_DAMAGED"
    elif _looks_like_vehicle_issue(message):
        session["vehicle_state"] = "UNCLEAR"
        session["current_state"] = "ASK_VEHICLE_STATUS"
        return session, [_button_message(sender_phone, render("ASK_GPS_OR_VEHICLE"), [("PAYLOAD_GPS", "GPS"), ("PAYLOAD_VEHICLE", "Vehicle")])]
    else:
        status = llm.classify_vehicle_status(session["current_state"], message, conversation_context)
    session["vehicle_state"] = status

    if status == "WORKSHOP":
        # Try to extract date from the message to skip ASK_EXPECTED_DATE if available
        extract_date = getattr(llm, "extract_date", None)
        date_value = extract_date(session["current_state"], message, conversation_context) if extract_date else ""
        if date_value:
            session["extracted_appointment_date"] = date_value
            session["current_state"] = "COMPLETED"
            return session, [_msg(sender_phone, render("SAVE_DATE_CLOSE", date=date_value))]
        session["current_state"] = "ASK_EXPECTED_DATE"
        return session, [_msg(sender_phone, render("ASK_EXPECTED_DATE_WORKSHOP"))]

    if status == "ACCIDENT":
        session["current_state"] = "ASK_CURRENT_LOCATION"
        return session, [_msg(sender_phone, render("ASK_CURRENT_LOCATION"))]

    if status == "GPS_DAMAGED":
        session["vehicle_state"] = status
        session["current_state"] = "ASK_GPS_REPAIR_CONFIRMATION"
        return session, [_button_message(sender_phone, render("ASK_GPS_REPAIR_CONFIRMATION"), [("PAYLOAD_YES", "Haan"), ("PAYLOAD_NO", "Nahi")])]

    if status in ("RUNNING", "GPS_REMOVED"):
        fast_result = _handle_booking_bulk_extraction(session, message, sender_phone)
        if fast_result is not None:
            return fast_result

        session["current_state"] = "ASK_DESTINATION_LOCATION"
        return session, [_msg(sender_phone, render("ASK_DESTINATION_LOCATION"))]

    return session, [_msg(sender_phone, render("FALLBACK") + "\n" + render("ASK_VEHICLE_STATUS"))]


# ---------------------------------------------------------- ASK_EXPECTED_DATE

def handle_ask_expected_date(session, message, sender_phone):
    conversation_context = build_conversation_context(session)
    date_value = llm.extract_date(session["current_state"], message, conversation_context)
    if not date_value:
        return session, [_msg(sender_phone, "Date samajh nahi aayi, kripya dobara bhejein.")]

    session["extracted_appointment_date"] = date_value
    session["current_state"] = "COMPLETED"
    return session, [_msg(sender_phone, render("SAVE_DATE_CLOSE", date=date_value))]


# ---------------------------------------------------- SERVICE BOOKING STATES

def handle_ask_gps_repair_confirmation(session, message, sender_phone):
    conversation_context = build_conversation_context(session)
    payload = _normalize_payload(message)
    if payload == "PAYLOAD_YES":
        answer = "YES"
    elif payload == "PAYLOAD_NO":
        answer = "NO"
    else:
        answer = llm.classify_yes_no(session["current_state"], message, conversation_context)

    if answer == "YES":
        session["current_state"] = "ASK_CURRENT_LOCATION"
        return session, [_msg(sender_phone, render("ASK_CURRENT_LOCATION"))]

    if answer == "NO":
        session["current_state"] = "COMPLETED"
        return session, [_msg(sender_phone, "Thik hai. Agar baad mein service chahiye ho, hume message kar dijiye. Dhanyavaad!")]

    return session, [_button_message(sender_phone, render("ASK_GPS_REPAIR_CONFIRMATION"), [("PAYLOAD_YES", "Haan"), ("PAYLOAD_NO", "Nahi")])]


def handle_ask_current_location(session, message, sender_phone):
    conversation_context = build_conversation_context(session)
    value = llm.extract_free_text(session["current_state"], message, "current location", conversation_context)
    session["current_location"] = value or message

    if session.get("vehicle_state") != "RUNNING":
        session["destination_location"] = session.get("current_location", "")
        session["service_city_confirmed"] = ""
        session["service_city_question_mode"] = "TODAY"
        session["current_state"] = "ASK_SERVICE_CITY_CONFIRMATION"
        suggested_city = session.get("destination_location") or "Delhi"
        return session, [_msg(sender_phone, render("ASK_SERVICE_CITY_SUGGESTION", suggested_city=suggested_city))]

    session["current_state"] = "ASK_DESTINATION_LOCATION"
    return session, [_msg(sender_phone, render("ASK_DESTINATION_LOCATION"))]


def handle_ask_destination_location(session, message, sender_phone):
    conversation_context = build_conversation_context(session)
    value = llm.extract_free_text(session["current_state"], message, "destination location", conversation_context)
    session["destination_location"] = value or message
    session["service_city_confirmed"] = ""
    session["service_city_question_mode"] = "TODAY"
    session["current_state"] = "ASK_SERVICE_CITY_CONFIRMATION"
    suggested_city = session["destination_location"] or "Delhi"
    return session, [_msg(sender_phone, render("ASK_SERVICE_CITY_SUGGESTION", suggested_city=suggested_city))]


def handle_ask_service_city_confirmation(session, message, sender_phone):
    conversation_context = build_conversation_context(session)
    answer = llm.classify_yes_no(session["current_state"], message, conversation_context)
    if answer == "YES":
        session["service_city_confirmed"] = "TRUE"
        session["extracted_service_location"] = session.get("destination_location", "Delhi") or "Delhi"
        if session.get("service_city_question_mode") == "TODAY":
            session["service_date"] = datetime.now().date().isoformat()
            session["current_state"] = "ASK_SERVICE_TIME_WINDOW"
            return session, [_msg(sender_phone, render("ASK_SERVICE_TIME_WINDOW"))]

        session["current_state"] = "ASK_SERVICE_DATE"
        session["service_date_step"] = 0
        prompt_text, implied_date = get_service_date_prompt_and_date()
        session["pending_quick_date"] = implied_date
        return session, [_msg(sender_phone, prompt_text)]

    if answer == "NO":
        session["service_city_confirmed"] = "FALSE"
        session["current_state"] = "ASK_SERVICE_CITY_PREFERENCE"
        return session, [_msg(sender_phone, render("ASK_PREFERRED_SERVICE_CITY"))]

    return session, [_msg(sender_phone, render("ASK_SERVICE_CITY_SUGGESTION", suggested_city=session.get("destination_location", "Delhi") or "Delhi"))]


def handle_ask_service_city_preference(session, message, sender_phone):
    conversation_context = build_conversation_context(session)
    value = llm.extract_free_text(session["current_state"], message, "preferred service city", conversation_context)
    session["extracted_service_location"] = value or message
    session["service_city_confirmed"] = "FALSE"
    session["current_state"] = "ASK_SERVICE_DATE"
    session["service_date_step"] = 0
    prompt_text, implied_date = get_service_date_prompt_and_date()
    session["pending_quick_date"] = implied_date
    return session, [_msg(sender_phone, prompt_text)]


def handle_ask_service_date(session, message, sender_phone):
    conversation_context = build_conversation_context(session)
    value = llm.extract_date(session["current_state"], message, conversation_context)
    if value:
        session["service_date"] = value
        session["current_state"] = "ASK_SERVICE_TIME_WINDOW"
        return session, [_msg(sender_phone, render("ASK_SERVICE_TIME_WINDOW"))]

    answer = llm.classify_yes_no(session["current_state"], message, conversation_context)
    if answer == "YES":
        session["service_date"] = session.get("pending_quick_date") or datetime.now().date().isoformat()
        session["current_state"] = "ASK_SERVICE_TIME_WINDOW"
        return session, [_msg(sender_phone, render("ASK_SERVICE_TIME_WINDOW"))]

    if answer == "NO":
        session["service_date_step"] = 1
        session["current_state"] = "ASK_SERVICE_DATE_OPTIONS"
        return session, [_msg(sender_phone, get_service_date_options_prompt())]

    session["service_date_step"] = 1
    session["current_state"] = "ASK_SERVICE_DATE_OPTIONS"
    return session, [_msg(sender_phone, get_service_date_options_prompt())]


def handle_ask_service_date_options(session, message, sender_phone):
    conversation_context = build_conversation_context(session)
    raw = message.strip().lower()
    date_value = None

    if raw in ("1", "1️⃣"):
        date_value = add_days_to_today(2)
    elif raw in ("2", "2️⃣"):
        date_value = add_days_to_today(4)
    elif raw in ("3", "3️⃣"):
        return session, [_msg(sender_phone, render("ASK_SERVICE_DATE_CUSTOM"))]
    else:
        date_value = llm.extract_date(session["current_state"], message, conversation_context)

    if date_value:
        session["service_date"] = date_value
        session["current_state"] = "ASK_SERVICE_TIME_WINDOW"
        return session, [_msg(sender_phone, render("ASK_SERVICE_TIME_WINDOW"))]

    return session, [_msg(sender_phone, get_service_date_options_prompt())]


def handle_ask_service_time_window(session, message, sender_phone):
    conversation_context = build_conversation_context(session)
    value = llm.extract_time(session["current_state"], message, conversation_context)
    if not value:
        return session, [_msg(sender_phone, render("ASK_SERVICE_TIME_WINDOW"))]

    session["service_time_window"] = value
    if (session.get("driver_name") or session.get("driver_phone")) and not session.get("driver_contact_confirmed"):
        session["current_state"] = "ASK_DRIVER_CONTACT_CONFIRMATION"
        return session, [_msg(sender_phone, render(
            "ASK_DRIVER_CONTACT_CONFIRMATION",
            driver_name=session.get("driver_name", ""),
            driver_phone=session.get("driver_phone", ""),
        ))]

    session["current_state"] = "ASK_CONTACT_PERSON"
    return session, [_msg(sender_phone, render("ASK_CONTACT_PERSON"))]


def handle_driver_contact_confirmation(session, message, sender_phone):
    conversation_context = build_conversation_context(session)
    payload = _normalize_payload(message)
    if payload == "PAYLOAD_YES":
        answer = "YES"
    elif payload == "PAYLOAD_NO":
        answer = "NO"
    else:
        answer = llm.classify_yes_no(session["current_state"], message, conversation_context)

    if answer == "YES":
        session["driver_contact_confirmed"] = "TRUE"
        session["contact_person"] = session.get("driver_name", "Driver")
        session["contact_number"] = session.get("driver_phone", "NOT_PROVIDED")
        return _create_and_confirm_ticket_directly(session, sender_phone)

    if answer == "NO":
        session["driver_contact_confirmed"] = "FALSE"
        session["awaiting_alternate_contact"] = "TRUE"
        session["current_state"] = "ASK_ALTERNATE_CONTACT"
        return session, [_msg(sender_phone, render("ASK_ALTERNATE_CONTACT"))]

    text = render(
        "ASK_DRIVER_CONTACT_CONFIRMATION",
        driver_name=session.get("driver_name", ""),
        driver_phone=session.get("driver_phone", ""),
    )
    return session, [_button_message(sender_phone, text, [("PAYLOAD_YES", "YES"), ("PAYLOAD_NO", "NO")])]


def handle_ask_alternate_contact(session, message, sender_phone):
    conversation_context = build_conversation_context(session)
    raw = message.strip().lower()

    # Fixed two bugs here:
    #   1. missing parens meant "phone" alone (via "no...number") wasn't
    #      properly grouped with "nahi"/"not provided" before being AND'd.
    #   2. a message containing "nahi" ANYWHERE used to be treated as "no
    #      contact provided" even if it also contained a valid phone
    #      number (e.g. "Nahi, driver ka number hi sahi hai, 9876543210"
    #      would previously throw the number away).
    says_not_provided = "nahi" in raw or ("no" in raw and "number" in raw) or "not provided" in raw
    has_phone_number = bool(PHONE_RE.search(message))

    if says_not_provided and not has_phone_number:
        session["contact_person"] = "NOT_PROVIDED"
        session["contact_number"] = "NOT_PROVIDED"
        return _create_and_confirm_ticket_directly(session, sender_phone)

    extracted = llm.extract_name_and_phone(session["current_state"], message, conversation_context)
    phone_match = PHONE_RE.search(extracted.get("phone", "") or message)
    if phone_match:
        session["contact_number"] = phone_match.group(1)
        session["contact_person"] = extracted.get("name") or message
        return _create_and_confirm_ticket_directly(session, sender_phone)

    return session, [_msg(sender_phone, render("INVALID_NUMBER") + "\n" + render("ASK_ALTERNATE_CONTACT"))]


def handle_ask_contact_person(session, message, sender_phone):
    conversation_context = build_conversation_context(session)
    raw = message.strip().lower()
    value = llm.extract_free_text(session["current_state"], message, "contact person name", conversation_context)

    if "driver" in raw and session.get("driver_name"):
        value = f"Driver ({session['driver_name']})"
        session["contact_person"] = value
        session["contact_number"] = session.get("driver_phone") or session.get("contact_number") or "NOT_PROVIDED"
        return _create_and_confirm_ticket_directly(session, sender_phone)

    if not value and raw in {"driver", "driver se", "driver ko", "driver hi"}:
        session["contact_person"] = f"Driver ({session.get('driver_name') or 'Driver'})"
        session["contact_number"] = session.get("driver_phone") or "NOT_PROVIDED"
        return _create_and_confirm_ticket_directly(session, sender_phone)

    session["contact_person"] = value or message
    session["current_state"] = "ASK_CONTACT_NUMBER"
    return session, [_msg(sender_phone, render("ASK_CONTACT_NUMBER"))]


def handle_ask_booking_correction(session, message, sender_phone):
    conversation_context = build_conversation_context(session)
    updated = False
    raw = message.strip().lower()

    if PHONE_RE.search(message):
        session["contact_number"] = PHONE_RE.search(message).group(1)
        updated = True

    if "service location" in raw or ("service" in raw and "location" in raw):
        value = llm.extract_free_text(session["current_state"], message, "service location", conversation_context)
        if value:
            session["extracted_service_location"] = value
            updated = True

    if "destination" in raw or "kahan" in raw or "ja rahe" in raw:
        value = llm.extract_free_text(session["current_state"], message, "destination location", conversation_context)
        if value:
            session["destination_location"] = value
            updated = True

    if "vehicle location" in raw or "current location" in raw or ("location" in raw and "service" not in raw):
        value = llm.extract_free_text(session["current_state"], message, "current location", conversation_context)
        if value:
            session["current_location"] = value
            updated = True

    if "time" in raw or "baje" in raw or "+" in raw or "pm" in raw or "am" in raw:
        value = llm.extract_time(session["current_state"], message, conversation_context)
        if value:
            session["service_time_window"] = value
            updated = True

    if "date" in raw or "kal" in raw or "parso" in raw or "aaj" in raw or "july" in raw or "aug" in raw or "september" in raw or "oct" in raw:
        value = llm.extract_date(session["current_state"], message, conversation_context)
        if value:
            session["service_date"] = value
            updated = True

    if ("city" in raw or "service city" in raw or "preferred city" in raw) and not updated:
        value = llm.extract_free_text(session["current_state"], message, "preferred service city", conversation_context)
        if value:
            session["extracted_service_location"] = value
            updated = True

    if ("contact person" in raw or "site" in raw or "phone" in raw or "number" in raw) and not updated:
        value = llm.extract_free_text(session["current_state"], message, "contact person name", conversation_context)
        if value:
            session["contact_person"] = value
            updated = True

    if not updated:
        session["current_state"] = "ASK_BOOKING_CORRECTION"
        return session, [_msg(sender_phone, "Koi sahi detail nahi mili. Kripya sirf woh detail bhejein jo aap update karna chahte hain, jaise 'Service city Delhi' ya 'Time 5 baje'.")]

    return _create_and_confirm_ticket_directly(session, sender_phone)


def handle_ask_contact_number(session, message, sender_phone):
    match = PHONE_RE.search(message)
    if not match:
        return session, [_msg(sender_phone, render("INVALID_NUMBER"))]

    session["contact_number"] = match.group(1)
    return _create_and_confirm_ticket_directly(session, sender_phone)


def handle_confirm_summary(session, message, sender_phone):
    conversation_context = build_conversation_context(session)
    answer = llm.classify_yes_no(session["current_state"], message, conversation_context)

    if answer == "YES":
        ticket = ticket_service.create_ticket(session)
        outbound = _ticket_confirmation_messages(session, ticket, sender_phone)
        return session, outbound

    if answer == "NO":
        session["current_state"] = "ASK_BOOKING_CORRECTION"
        return session, [_msg(sender_phone, render(
            "BOOKING_CORRECTION",
            current_location=session.get("current_location", ""),
            service_location=session.get("extracted_service_location", ""),
            service_date=session.get("service_date", ""),
            service_time=session.get("service_time_window", session.get("service_time", "")),
            contact_person=session.get("contact_person", ""),
            contact_number=session.get("contact_number", ""),
        ))]

    return session, [_msg(sender_phone, render("FALLBACK"))]


def handle_completed(session, message, sender_phone):
    if gps_service.verify_gps(session):
        return session, [_msg(sender_phone, render("GPS_FIXED_CLOSE"))]

    session["current_state"] = "ASK_VEHICLE_STATUS"
    return session, [
        _msg(sender_phone, render("ASK_VEHICLE_STATUS_AFTER_CLOSE")),
        _vehicle_status_options_message(sender_phone),
    ]
    
def handle_direct_tech_request(session, message, sender_phone):
    return _handle_direct_tech_request(session, message, sender_phone)


# --------------------------------------------------------------- DISPATCH --

HANDLERS = {
    "START": handle_start,
    "DIRECT_TECH_REQUEST": handle_direct_tech_request,
    "ASK_HANDLER": handle_ask_handler,
    "DRIVER_CONFIRM": handle_driver_confirm,
    "ASK_NEW_DRIVER": handle_ask_new_driver,
    "WAIT_DONE": handle_wait_done,
    "ASK_PHYSICAL_DAMAGE": handle_ask_physical_damage,
    "ASK_VEHICLE_STATUS": handle_ask_vehicle_status,
    "ASK_EXPECTED_DATE": handle_ask_expected_date,
    "ASK_GPS_REPAIR_CONFIRMATION": handle_ask_gps_repair_confirmation,
    "ASK_CURRENT_LOCATION": handle_ask_current_location,
    "ASK_DESTINATION_LOCATION": handle_ask_destination_location,
    "ASK_SERVICE_CITY_CONFIRMATION": handle_ask_service_city_confirmation,
    "ASK_SERVICE_CITY_PREFERENCE": handle_ask_service_city_preference,
    "ASK_SERVICE_DATE": handle_ask_service_date,
    "ASK_SERVICE_DATE_OPTIONS": handle_ask_service_date_options,
    "ASK_SERVICE_TIME_WINDOW": handle_ask_service_time_window,
    "ASK_DRIVER_CONTACT_CONFIRMATION": handle_driver_contact_confirmation,
    "ASK_ALTERNATE_CONTACT": handle_ask_alternate_contact,
    "ASK_CONTACT_PERSON": handle_ask_contact_person,
    "ASK_CONTACT_NUMBER": handle_ask_contact_number,
    "ASK_BOOKING_CORRECTION": handle_ask_booking_correction,
    "CONFIRM_SUMMARY": handle_confirm_summary,
    "COMPLETED": handle_completed,
}


def _reply_text_for(outbound: list[dict], sender_phone: str) -> str:
    for out in outbound:
        if out.get("phone") == sender_phone:
            if out.get("text"):
                return out["text"]
            interactive = out.get("interactive") or {}
            return interactive.get("body", {}).get("text", "")
    return ""


def _handle_general_question(session, message, sender_phone):
    """
    Answers an off-topic question (working hours, how GPS tracking works,
    pricing, etc.) from the knowledge base, then hands the conversation
    back to wherever it left off — current_state is never changed here,
    and we replay the pending question so the flow isn't lost.
    """
    answer = llm.answer_from_knowledge_base(message)
    pending = session.get("last_prompt_text", "")

    if pending:
        reply = f"{answer}\n\nChaliye, wapas apne case par aate hain — {pending}"
    else:
        reply = answer

    return session, [_msg(sender_phone, reply)]


# ------------------------------------------ bulk booking-slot extraction --
# "Give everything in one line" — if a customer volunteers several
# booking answers at once instead of one field at a time, extract all of
# them and skip straight to whatever's still missing, instead of asking
# questions that were already answered. Saves the customer real time on
# both WhatsApp and the voice agent (both go through process_message()).

# Only these states are open free-text answers where bulk-extraction makes
# sense. Yes/no confirmations and numbered-menu states are deliberately
# excluded — extracting "booking slots" out of "haan" or "2" doesn't mean
# anything, and the safety confirmation step is never skipped (see
# _next_missing_booking_state).
_BOOKING_FLOW_STATES = {
    "ASK_CURRENT_LOCATION", "ASK_DESTINATION_LOCATION",
    "ASK_SERVICE_DATE", "ASK_SERVICE_TIME_WINDOW",
    "ASK_CONTACT_PERSON", "ASK_CONTACT_NUMBER",
}


def _looks_information_dense(message: str) -> bool:
    """
    Cheap pre-filter so we don't spend an extra LLM call on every single
    ordinary one-word answer ("Nagpur", "Kal", "9876543210") — only
    messages that are actually long enough to plausibly contain several
    answers at once trigger the bulk-extraction attempt.
    """
    return len(message.split()) >= 6


def _apply_booking_slots(session: dict, message: str, slots: dict) -> bool:
    """
    Fills in whatever booking fields were found (without ever overwriting
    something already captured earlier). Returns True only if at least
    TWO fields got filled — one field is just an ordinary answer to
    whatever was already being asked, and should go through that state's
    normal handler as usual; two or more is the actual "gave everything
    at once" case worth fast-forwarding for.
    """
    filled = []

    if slots.get("current_location") and not session.get("current_location"):
        session["current_location"] = slots["current_location"]
        filled.append("current_location")

    if slots.get("destination_location") and not session.get("destination_location"):
        session["destination_location"] = slots["destination_location"]
        filled.append("destination_location")

    if slots.get("service_date") and not session.get("service_date"):
        session["service_date"] = slots["service_date"]
        filled.append("service_date")

    if slots.get("service_time_window") and not session.get("service_time_window"):
        session["service_time_window"] = slots["service_time_window"]
        filled.append("service_time_window")

    if slots.get("contact_person") and not session.get("contact_person"):
        session["contact_person"] = slots["contact_person"]
        filled.append("contact_person")

    phone_match = PHONE_RE.search(slots.get("contact_number") or "") or PHONE_RE.search(message)
    if phone_match and not session.get("contact_number"):
        session["contact_number"] = phone_match.group(1)
        filled.append("contact_number")

    return len(filled) >= 2


def _next_missing_booking_state(session: dict) -> str:
    """
    Given whatever booking fields are already filled in, returns the
    state for the next thing still needed — this is what lets a customer
    skip straight past several already-answered questions at once.

    The city-confirmation yes/no is deliberately NEVER skipped even if a
    destination was already given — booking a service call to the wrong
    city is costly enough that it always gets an explicit confirmation.
    """
    if not session.get("current_location"):
        return "ASK_CURRENT_LOCATION"
    if not session.get("destination_location"):
        return "ASK_DESTINATION_LOCATION"
    if not session.get("service_city_confirmed"):
        return "ASK_SERVICE_CITY_CONFIRMATION"
    if not session.get("service_date"):
        return "ASK_SERVICE_DATE"
    if not session.get("service_time_window"):
        return "ASK_SERVICE_TIME_WINDOW"

    has_driver = bool(session.get("driver_name") or session.get("driver_phone"))
    gave_explicit_contact = bool(session.get("contact_person")) and bool(session.get("contact_number"))
    if has_driver and not session.get("driver_contact_confirmed") and not gave_explicit_contact:
        return "ASK_DRIVER_CONTACT_CONFIRMATION"

    if not session.get("contact_person"):
        return "ASK_CONTACT_PERSON"
    if not session.get("contact_number"):
        return "ASK_CONTACT_NUMBER"
    return "CONFIRM_SUMMARY"


def _prompt_for_booking_state(session: dict, state: str) -> str:
    """
    Builds the exact prompt for `state`, reusing the same
    template/kwargs each per-field handler already uses for that same
    state — kept in one place so the bulk-extraction fast path and the
    normal step-by-step handlers can never drift apart.
    """
    if state == "ASK_CURRENT_LOCATION":
        return render("ASK_CURRENT_LOCATION")
    if state == "ASK_DESTINATION_LOCATION":
        return render("ASK_DESTINATION_LOCATION")
    if state == "ASK_SERVICE_CITY_CONFIRMATION":
        suggested_city = session.get("destination_location") or "Delhi"
        return render("ASK_SERVICE_CITY_SUGGESTION", suggested_city=suggested_city)
    if state == "ASK_SERVICE_DATE":
        prompt_text, implied_date = get_service_date_prompt_and_date()
        session["pending_quick_date"] = implied_date
        return prompt_text
    if state == "ASK_SERVICE_TIME_WINDOW":
        return render("ASK_SERVICE_TIME_WINDOW")
    if state == "ASK_DRIVER_CONTACT_CONFIRMATION":
        return render(
            "ASK_DRIVER_CONTACT_CONFIRMATION",
            driver_name=session.get("driver_name", ""),
            driver_phone=session.get("driver_phone", ""),
        )
    if state == "ASK_CONTACT_PERSON":
        return render("ASK_CONTACT_PERSON")
    if state == "ASK_CONTACT_NUMBER":
        return render("ASK_CONTACT_NUMBER")
    if state == "CONFIRM_SUMMARY":
        return render(
            "BOOKING_SUMMARY",
            current_location=session.get("current_location", ""),
            service_location=session.get("extracted_service_location", ""),
            service_date=session.get("service_date", ""),
            service_time=session.get("service_time_window", session.get("service_time", "")),
            contact_person=session.get("contact_person", ""),
            contact_number=session.get("contact_number", ""),
        )
    return render("ASK_CURRENT_LOCATION")


def _handle_booking_bulk_extraction(session: dict, message: str, sender_phone: str):
    conversation_context = build_conversation_context(session)
    slots = llm.extract_booking_slots(message, conversation_context)
    if not _apply_booking_slots(session, message, slots):
        return None  # nothing extra found — let the normal handler run

    next_state = _next_missing_booking_state(session)
    session["current_state"] = next_state
    prompt = _prompt_for_booking_state(session, next_state)
    return session, [_msg(sender_phone, prompt)]


def process_message(session: dict, message: str, sender_phone: str):
    """
    Entry point called by the webhook.
    Looks up the right handler for session['current_state'] and runs it.
    Returns (updated_session, outbound_messages).
    """
    state = session.get("current_state") or "START"

    if _is_driver_change_request(message):
        return _handle_driver_change_request(session, sender_phone)

    if llm.is_driver_update_intent(message, build_conversation_context(session)):
        return _handle_driver_update_message(session, message, sender_phone)

    if state in {"ASK_DIRECT_TECH_LOCATION", "ASK_SERVICE_DATE", "ASK_SERVICE_TIME_WINDOW", "ASK_CONTACT_PERSON", "ASK_CONTACT_NUMBER"}:
        if state == "ASK_CONTACT_PERSON" and ("driver" in message.strip().lower() and session.get("driver_name")):
            return handle_ask_contact_person(session, message, sender_phone)
        if state in {"ASK_DIRECT_TECH_LOCATION", "ASK_SERVICE_DATE", "ASK_SERVICE_TIME_WINDOW", "ASK_CONTACT_PERSON", "ASK_CONTACT_NUMBER"}:
            return _handle_direct_tech_request(session, message, sender_phone)
    
    if state == "ASK_DIRECT_TECH_LOCATION" or _is_direct_tech_request(message):
        return _handle_direct_tech_request(session, message, sender_phone)

    if (
        session.get("handler", "OWNER") == "OWNER"
        and state not in ("COMPLETED", "DRIVER_CONFIRM", "ASK_NEW_DRIVER", "ASK_HANDLER")
        and _is_driver_request(message)
    ):
        return _start_driver_handoff(session, sender_phone)

    is_payload = bool(_normalize_payload(message))
    if (
        message.strip()
        and not is_payload
        and llm.is_general_question(state, message, build_conversation_context(session)) == "GENERAL_QUESTION"
    ):
        return _handle_general_question(session, message, sender_phone)

    if (
        state in _BOOKING_FLOW_STATES
        and not is_payload
        and _looks_information_dense(message)
    ):
        bulk_result = _handle_booking_bulk_extraction(session, message, sender_phone)
        if bulk_result is not None:
            updated_session, outbound = bulk_result
            reply_text = _reply_text_for(outbound, sender_phone)
            if reply_text:
                updated_session["last_prompt_text"] = reply_text
            return updated_session, outbound

    handler = HANDLERS.get(state, handle_start)
    updated_session, outbound = handler(session, message, sender_phone)

    reply_text = _reply_text_for(outbound, sender_phone)
    if reply_text:
        updated_session["last_prompt_text"] = reply_text

    return updated_session, outbound