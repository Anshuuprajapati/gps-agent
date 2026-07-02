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
from core import llm_handler as llm
from services import gps_service, driver_service, ticket_service
from prompts.templates import render

PHONE_RE = re.compile(r"(\d{10})")


def _msg(phone, text):
    return {"phone": phone, "text": text}


# ---------------------------------------------------------------- START ----

def handle_start(session, message, sender_phone):
    root_cause = gps_service.analyze_root_cause(session)
    session["root_cause"] = root_cause

    location = session.get("last_location") or session.get("current_location") or "N/A"
    last_update = session.get("gpstime") or session.get("timestamp") or "N/A"

    if root_cause == "BATTERY":
        session["current_state"] = "ASK_HANDLER"
        text = render("BATTERY_ALERT", vehicle_no=session["vehicle_no"], location=location, last_update=last_update)
        return session, [_msg(sender_phone, text)]

    if root_cause == "MAIN_POWER":
        session["current_state"] = "ASK_HANDLER"
        text = render("MAIN_POWER_ALERT", vehicle_no=session["vehicle_no"], location=location, last_update=last_update)
        return session, [_msg(sender_phone, text)]

    # root cause unknown from telemetry alone -> ask vehicle status directly
    session["current_state"] = "ASK_VEHICLE_STATUS"
    text = render("OTHER_ALERT", vehicle_no=session["vehicle_no"], location=location, last_update=last_update)
    text += "\n" + render("VEHICLE_STATUS_OPTIONS")
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
        return session, [_msg(sender_phone, text)]
    else:
        session["current_state"] = "ASK_NEW_DRIVER"
        return session, [_msg(sender_phone, "Driver ka naam aur mobile number bhejein.")]


# ------------------------------------------------------------ ASK_HANDLER --

def handle_ask_handler(session, message, sender_phone):
    choice = llm.classify_self_or_driver(session["current_state"], message)

    if choice == "SELF":
        session["handler"] = "OWNER"
        session["current_state"] = "WAIT_DONE"
        key = "ASK_CHECK_BATTERY" if session["root_cause"] == "BATTERY" else "ASK_CHECK_POWER"
        return session, [_msg(sender_phone, render(key))]

    if choice == "DRIVER":
        return _start_driver_handoff(session, sender_phone)

    # unclear -> repeat
    return session, [_msg(sender_phone, render("FALLBACK") + "\nReply: SELF ya DRIVER")]


# ----------------------------------------------------------- DRIVER_CONFIRM

def handle_driver_confirm(session, message, sender_phone):
    answer = llm.classify_yes_no(session["current_state"], message)

    if answer == "YES":
        session = driver_service.transfer_to_driver(session)
        session["current_state"] = "WAIT_DONE"
        owner_msg = _msg(session["phone_number"], render("TRANSFER_DONE_OWNER"))
        key = "ASK_CHECK_BATTERY" if session["root_cause"] == "BATTERY" else "ASK_CHECK_POWER"
        driver_intro = render("TRANSFER_DONE_DRIVER", driver_name=session["driver_name"], vehicle_no=session["vehicle_no"])
        driver_msg = _msg(session["driver_phone"], driver_intro + "\n" + render(key))
        return session, [owner_msg, driver_msg]

    if answer == "NO":
        session["current_state"] = "ASK_NEW_DRIVER"
        return session, [_msg(sender_phone, "Thik hai, naye driver ka naam aur mobile number bhejein.")]

    return session, [_msg(sender_phone, render("FALLBACK") + "\nReply YES ya naye driver ki detail bhejein.")]


# ------------------------------------------------------------ ASK_NEW_DRIVER

def handle_ask_new_driver(session, message, sender_phone):
    extracted = llm.extract_name_and_phone(session["current_state"], message)
    phone_match = PHONE_RE.search(extracted.get("phone", "") or message)

    if extracted.get("name") and phone_match:
        session = driver_service.update_driver_details(session, extracted["name"], phone_match.group(1))
        session = driver_service.transfer_to_driver(session)
        session["current_state"] = "WAIT_DONE"
        owner_msg = _msg(session["phone_number"], render("TRANSFER_DONE_OWNER"))
        key = "ASK_CHECK_BATTERY" if session["root_cause"] == "BATTERY" else "ASK_CHECK_POWER"
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
        return session, [_msg(sender_phone, render("ASK_PHYSICAL_DAMAGE"))]

    session["current_state"] = "ASK_VEHICLE_STATUS"
    return session, [_msg(sender_phone, render("ASK_VEHICLE_STATUS"))]


def handle_wait_done(session, message, sender_phone):
    # fast path — no LLM call needed for the exact expected reply
    if message.strip().lower() == "done":
        return _finish_wait_done(session, sender_phone)

    intent = llm.classify_wait_done_reply(session["current_state"], message)

    if intent == "DONE":
        return _finish_wait_done(session, sender_phone)

    if intent == "NEED_HELP":
        key = "BATTERY_HELP_STEPS" if session["root_cause"] == "BATTERY" else "MAIN_POWER_HELP_STEPS"
        return session, [_msg(sender_phone, render(key))]

    if intent == "WANT_DRIVER":
        return _start_driver_handoff(session, sender_phone)

    return session, [_msg(sender_phone, render("WAIT_DONE_NUDGE"))]


# --------------------------------------------------------- ASK_PHYSICAL_DAMAGE

def handle_ask_physical_damage(session, message, sender_phone):
    answer = llm.classify_yes_no(session["current_state"], message)

    if answer == "YES":
        session["physical_damage"] = "YES"
        session["current_state"] = "ASK_CURRENT_LOCATION"
        return session, [_msg(sender_phone, render("ASK_CURRENT_LOCATION"))]

    if answer == "NO":
        session["current_state"] = "WAIT_DONE"
        key = "ASK_CHECK_BATTERY" if session["root_cause"] == "BATTERY" else "ASK_CHECK_POWER"
        return session, [_msg(sender_phone, "Thik hai, ek baar aur try kijiye. " + render(key))]

    return session, [_msg(sender_phone, render("FALLBACK") + "\nReply YES ya NO.")]


# ---------------------------------------------------------- ASK_VEHICLE_STATUS

def handle_ask_vehicle_status(session, message, sender_phone):
    status = llm.classify_vehicle_status(session["current_state"], message)
    session["vehicle_state"] = status

    if status in ("WORKSHOP", "ACCIDENT"):
        session["current_state"] = "ASK_EXPECTED_DATE"
        return session, [_msg(sender_phone, render("ASK_EXPECTED_DATE"))]

    if status in ("RUNNING", "GPS_DAMAGED", "GPS_REMOVED"):
        session["current_state"] = "ASK_CURRENT_LOCATION"
        return session, [_msg(sender_phone, render("ASK_CURRENT_LOCATION"))]

    return session, [_msg(sender_phone, render("FALLBACK") + "\n" + render("ASK_VEHICLE_STATUS"))]


# ---------------------------------------------------------- ASK_EXPECTED_DATE

def handle_ask_expected_date(session, message, sender_phone):
    date_value = llm.extract_date(session["current_state"], message)
    if not date_value:
        return session, [_msg(sender_phone, "Date samajh nahi aayi, kripya dobara bhejein.")]

    session["extracted_appointment_date"] = date_value
    session["current_state"] = "COMPLETED"
    return session, [_msg(sender_phone, render("SAVE_DATE_CLOSE", date=date_value))]


# ---------------------------------------------------- SERVICE BOOKING STATES

def handle_ask_current_location(session, message, sender_phone):
    value = llm.extract_free_text(session["current_state"], message, "current location")
    session["current_location"] = value or message
    session["current_state"] = "ASK_SERVICE_LOCATION"
    return session, [_msg(sender_phone, render("ASK_SERVICE_LOCATION"))]


def handle_ask_service_location(session, message, sender_phone):
    value = llm.extract_free_text(session["current_state"], message, "service location")
    session["extracted_service_location"] = value or message
    session["current_state"] = "ASK_SERVICE_DATE"
    return session, [_msg(sender_phone, render("ASK_SERVICE_DATE"))]


def handle_ask_service_date(session, message, sender_phone):
    value = llm.extract_date(session["current_state"], message)
    session["service_date"] = value or message
    session["current_state"] = "ASK_SERVICE_TIME"
    return session, [_msg(sender_phone, render("ASK_SERVICE_TIME"))]


def handle_ask_service_time(session, message, sender_phone):
    value = llm.extract_free_text(session["current_state"], message, "preferred time")
    session["service_time"] = value or message
    session["current_state"] = "ASK_CONTACT_PERSON"
    return session, [_msg(sender_phone, render("ASK_CONTACT_PERSON"))]


def handle_ask_contact_person(session, message, sender_phone):
    value = llm.extract_free_text(session["current_state"], message, "contact person name")
    session["contact_person"] = value or message
    session["current_state"] = "ASK_CONTACT_NUMBER"
    return session, [_msg(sender_phone, render("ASK_CONTACT_NUMBER"))]


def handle_ask_contact_number(session, message, sender_phone):
    match = PHONE_RE.search(message)
    if not match:
        return session, [_msg(sender_phone, render("INVALID_NUMBER"))]

    session["contact_number"] = match.group(1)
    session["current_state"] = "CONFIRM_SUMMARY"
    text = render(
        "BOOKING_SUMMARY",
        current_location=session["current_location"],
        service_location=session["extracted_service_location"],
        service_date=session["service_date"],
        service_time=session["service_time"],
        contact_person=session["contact_person"],
        contact_number=session["contact_number"],
    )
    return session, [_msg(sender_phone, text)]


def handle_confirm_summary(session, message, sender_phone):
    answer = llm.classify_yes_no(session["current_state"], message)

    if answer == "YES":
        ticket = ticket_service.create_ticket(session)
        session["ticket_id"] = ticket["ticket_id"]
        session["engineer_id"] = ticket["engineer_id"]
        session["current_state"] = "COMPLETED"
        text = render(
            "BOOKING_CONFIRMED",
            ticket_id=ticket["ticket_id"],
            engineer_name=ticket["engineer_name"],
            engineer_phone=ticket["engineer_phone"],
        )
        return session, [_msg(sender_phone, text)]

    if answer == "NO":
        session["current_state"] = "ASK_CURRENT_LOCATION"
        return session, [_msg(sender_phone, render("BOOKING_REDO"))]

    return session, [_msg(sender_phone, render("FALLBACK") + "\nReply YES ya NO.")]


def handle_completed(session, message, sender_phone):
    return session, [_msg(sender_phone, "Yeh case pehle se close ho chuka hai. Naye issue ke liye support se contact karein.")]


# --------------------------------------------------------------- DISPATCH --

HANDLERS = {
    "START": handle_start,
    "ASK_HANDLER": handle_ask_handler,
    "DRIVER_CONFIRM": handle_driver_confirm,
    "ASK_NEW_DRIVER": handle_ask_new_driver,
    "WAIT_DONE": handle_wait_done,
    "ASK_PHYSICAL_DAMAGE": handle_ask_physical_damage,
    "ASK_VEHICLE_STATUS": handle_ask_vehicle_status,
    "ASK_EXPECTED_DATE": handle_ask_expected_date,
    "ASK_CURRENT_LOCATION": handle_ask_current_location,
    "ASK_SERVICE_LOCATION": handle_ask_service_location,
    "ASK_SERVICE_DATE": handle_ask_service_date,
    "ASK_SERVICE_TIME": handle_ask_service_time,
    "ASK_CONTACT_PERSON": handle_ask_contact_person,
    "ASK_CONTACT_NUMBER": handle_ask_contact_number,
    "CONFIRM_SUMMARY": handle_confirm_summary,
    "COMPLETED": handle_completed,
}


def process_message(session: dict, message: str, sender_phone: str):
    """
    Entry point called by the webhook.
    Looks up the right handler for session['current_state'] and runs it.
    Returns (updated_session, outbound_messages).
    """
    state = session.get("current_state") or "START"
    handler = HANDLERS.get(state, handle_start)
    return handler(session, message, sender_phone)