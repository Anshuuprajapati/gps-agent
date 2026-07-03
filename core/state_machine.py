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


def _normalize_phone_number(phone_str: str) -> str:
    """
    Normalize phone number: add country code +91 if only 10 digits provided.
    If already has country code (11+ digits), return as-is.
    """
    if not phone_str:
        return ""
    phone_str = phone_str.strip()
    # Extract only digits
    digits = re.sub(r"\D", "", phone_str)
    if len(digits) == 10:
        # 10-digit number - add India country code
        return "91" + digits
    elif len(digits) >= 11:
        # Already has country code
        return digits
    return phone_str  # Return as-is if doesn't match pattern


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


def _normalize_payload(message: str) -> str:
    payload = message.strip().upper()
    if payload.startswith("PAYLOAD_"):
        return payload
    return ""


def _is_driver_request(message: str) -> bool:
    text = message.strip().lower()
    return bool(re.search(r"\b(driver se baat karo|driver se|driver ko|driver ka|driver pe|driver\b|driver\s*baat)\b", text))


def get_service_date_prompt() -> str:
    now = datetime.now()
    if now.hour < 19:
        return "Kya aaj service book kar dein?"
    return "Kya kal service book kar dein?"


def get_service_date_options_prompt() -> str:
    return (
        "Please choose one option from below:\n"
        "1️⃣ Book service after 2 days\n"
        "2️⃣ Book service after 4 days\n"
        "3️⃣ Enter a specific date or tell after how many days..."
    )


def add_days_to_today(days: int) -> str:
    return (datetime.now().date() + timedelta(days=days)).isoformat()


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
        return session, [_button_message(sender_phone, text, [("PAYLOAD_YES", "YES"), ("PAYLOAD_NO", "NO")])]
    else:
        session["current_state"] = "ASK_NEW_DRIVER"
        return session, [_msg(sender_phone, "Driver ka naam aur mobile number bhejein.")]


# ------------------------------------------------------------ ASK_HANDLER --

def handle_ask_handler(session, message, sender_phone):
    payload = _normalize_payload(message)
    if payload == "PAYLOAD_SELF":
        choice = "SELF"
    elif payload == "PAYLOAD_DRIVER":
        choice = "DRIVER"
    else:
        choice = llm.classify_self_or_driver(session["current_state"], message)

    if choice == "SELF":
        session["handler"] = "OWNER"
        session["current_state"] = "WAIT_DONE"
        key = "ASK_CHECK_BATTERY" if session["root_cause"] == BATTERY_ISSUE else "ASK_CHECK_POWER"
        # Generate contextual response acknowledging their choice
        response = llm.generate_contextual_response(
            session, message, "ASK_HANDLER", 
            missing_fields=["battery_status" if session["root_cause"] == BATTERY_ISSUE else "power_status"],
            root_cause=session["root_cause"]
        )
        # If LLM response is too short or generic, append the actual instruction
        if len(response.split("\n")) < 2:
            response += "\n" + render(key)
        return session, [_msg(sender_phone, response)]

    if choice == "DRIVER":
        return _start_driver_handoff(session, sender_phone)

    # Unclear response - use LLM to generate natural nudge
    response = llm.generate_contextual_response(
        session, message, "ASK_HANDLER",
        missing_fields=["handler_choice"],
        root_cause=session["root_cause"]
    )
    return session, [_msg(sender_phone, response)]


# ----------------------------------------------------------- DRIVER_CONFIRM

def handle_driver_confirm(session, message, sender_phone):
    payload = _normalize_payload(message)
    if payload == "PAYLOAD_YES":
        answer = "YES"
    elif payload == "PAYLOAD_NO":
        answer = "NO"
    else:
        answer = llm.classify_yes_no(session["current_state"], message)

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
        # Generate contextual response for getting new driver info
        response = llm.generate_contextual_response(
            session, message, "DRIVER_CONFIRM",
            missing_fields=["new_driver_details"],
        )
        return session, [_msg(sender_phone, response)]

    text = render("SHOW_DRIVER_DETAILS", driver_name=session.get("driver_name", ""), driver_phone=session.get("driver_phone", ""))
    return session, [_button_message(sender_phone, text, [("PAYLOAD_YES", "YES"), ("PAYLOAD_NO", "NO")])]


# ------------------------------------------------------------ ASK_NEW_DRIVER

def handle_ask_new_driver(session, message, sender_phone):
    extracted = llm.extract_name_and_phone(session["current_state"], message)
    phone_match = PHONE_RE.search(extracted.get("phone", "") or message)

    if extracted.get("name") and phone_match:
        # Normalize phone number (add 91 if 10 digits)
        normalized_phone = _normalize_phone_number(phone_match.group(1))
        session = driver_service.update_driver_details(session, extracted["name"], normalized_phone)
        session = driver_service.transfer_to_driver(session)
        session["current_state"] = "WAIT_DONE"
        owner_msg = _msg(session["phone_number"], render("TRANSFER_DONE_OWNER"))
        key = "ASK_CHECK_BATTERY" if session["root_cause"] == BATTERY_ISSUE else "ASK_CHECK_POWER"
        driver_intro = render("TRANSFER_DONE_DRIVER", driver_name=session["driver_name"], vehicle_no=session["vehicle_no"])
        driver_msg = _msg(session["driver_phone"], driver_intro + "\n" + render(key))
        return session, [owner_msg, driver_msg]

    # Generate contextual nudge for valid driver details
    response = llm.generate_contextual_response(
        session, message, "ASK_NEW_DRIVER",
        missing_fields=["driver_name", "driver_phone"],
    )
    return session, [_msg(sender_phone, response)]


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
    # fast path — no LLM call needed for the exact expected reply
    if message.strip().lower() == "done":
        return _finish_wait_done(session, sender_phone)

    intent = llm.classify_wait_done_reply(session["current_state"], message)

    if intent == "DONE":
        return _finish_wait_done(session, sender_phone)

    if intent == "NEED_HELP":
        key = "BATTERY_HELP_STEPS" if session["root_cause"] == BATTERY_ISSUE else "MAIN_POWER_HELP_STEPS"
        # Generate contextual help response
        response = llm.generate_nudge_or_help_response(
            session, "WAIT_DONE",
            issue_type="battery" if session["root_cause"] == BATTERY_ISSUE else "power",
            context=f"User asked for help with {session['root_cause']}"
        )
        # Append the template help steps
        response += "\n\n" + render(key)
        return session, [_msg(sender_phone, response)]

    if intent == "WANT_DRIVER":
        return _start_driver_handoff(session, sender_phone)

    if re.search(r"\b(kharab|toot|broken|repair|replace|damage|damaged|fault)\b", message.lower()):
        session["current_state"] = "ASK_PHYSICAL_DAMAGE"
        damage_prompt = "ASK_PHYSICAL_DAMAGE_MAIN_POWER" if session["root_cause"] == MAIN_POWER_DISCONNECTED else "ASK_PHYSICAL_DAMAGE"
        # Generate contextual response acknowledging damage
        response = llm.generate_contextual_response(
            session, message, "WAIT_DONE",
            missing_fields=["physical_damage_confirmation"],
            root_cause=session["root_cause"]
        )
        return session, [_msg(sender_phone, response + "\n" + render(damage_prompt))]

    # Unclear - generate nudge to continue workflow
    response = llm.generate_contextual_response(
        session, message, "WAIT_DONE",
        missing_fields=["completion_status"],
        root_cause=session["root_cause"]
    )
    return session, [_msg(sender_phone, response)]


# --------------------------------------------------------- ASK_PHYSICAL_DAMAGE

def handle_ask_physical_damage(session, message, sender_phone):
    payload = _normalize_payload(message)
    if payload == "PAYLOAD_YES":
        answer = "YES"
    elif payload == "PAYLOAD_NO":
        answer = "NO"
    else:
        answer = llm.classify_yes_no(session["current_state"], message)

    if answer == "YES":
        session["physical_damage"] = "YES"
        session["current_state"] = "ASK_CURRENT_LOCATION"
        # Generate contextual response acknowledging damage
        response = llm.generate_contextual_response(
            session, message, "ASK_PHYSICAL_DAMAGE",
            missing_fields=["current_location"],
        )
        if "location" not in response.lower():
            response += "\n" + render("ASK_CURRENT_LOCATION")
        return session, [_msg(sender_phone, response)]

    if answer == "NO":
        session["current_state"] = "WAIT_DONE"
        key = "ASK_CHECK_BATTERY" if session["root_cause"] == BATTERY_ISSUE else "ASK_CHECK_POWER"
        # Generate contextual response to retry
        response = llm.generate_contextual_response(
            session, message, "ASK_PHYSICAL_DAMAGE",
            missing_fields=["completion_status"],
        )
        response += "\n" + render(key)
        return session, [_msg(sender_phone, response)]

    damage_prompt = "ASK_PHYSICAL_DAMAGE_MAIN_POWER" if session["root_cause"] == MAIN_POWER_DISCONNECTED else "ASK_PHYSICAL_DAMAGE"
    return session, [_button_message(sender_phone, render(damage_prompt), [("PAYLOAD_YES", "YES"), ("PAYLOAD_NO", "NO")])]


# ---------------------------------------------------------- ASK_VEHICLE_STATUS

def handle_ask_vehicle_status(session, message, sender_phone):
    status = llm.classify_vehicle_status(session["current_state"], message)
    session["vehicle_state"] = status

    if status == "WORKSHOP":
        session["current_state"] = "ASK_EXPECTED_DATE"
        # Generate contextual response acknowledging workshop status
        response = llm.generate_contextual_response(
            session, message, "ASK_VEHICLE_STATUS",
            missing_fields=["expected_workshop_date"],
        )
        if "date" not in response.lower():
            response += "\n" + render("ASK_EXPECTED_DATE_WORKSHOP")
        return session, [_msg(sender_phone, response)]

    if status == "ACCIDENT":
        session["current_state"] = "ASK_EXPECTED_DATE"
        # Generate contextual response acknowledging accident
        response = llm.generate_contextual_response(
            session, message, "ASK_VEHICLE_STATUS",
            missing_fields=["accident_recovery_date"],
        )
        if "date" not in response.lower():
            response += "\n" + render("ASK_EXPECTED_DATE_ACCIDENT")
        return session, [_msg(sender_phone, response)]

    if status in ("RUNNING", "GPS_DAMAGED", "GPS_REMOVED"):
        session["current_state"] = "ASK_CURRENT_LOCATION"
        # Generate contextual response and ask for location
        response = llm.generate_contextual_response(
            session, message, "ASK_VEHICLE_STATUS",
            missing_fields=["current_location"],
        )
        if "location" not in response.lower():
            response += "\n" + render("ASK_CURRENT_LOCATION")
        return session, [_msg(sender_phone, response)]

    # Unclear - generate contextual nudge
    response = llm.generate_contextual_response(
        session, message, "ASK_VEHICLE_STATUS",
        missing_fields=["vehicle_status"],
    )
    return session, [_msg(sender_phone, response)]


# ---------------------------------------------------------- ASK_EXPECTED_DATE

def handle_ask_expected_date(session, message, sender_phone):
    date_value = llm.extract_date(session["current_state"], message)
    if not date_value:
        # Generate contextual nudge for valid date
        response = llm.generate_contextual_response(
            session, message, "ASK_EXPECTED_DATE",
            missing_fields=["valid_date"],
        )
        return session, [_msg(sender_phone, response)]

    session["extracted_appointment_date"] = date_value
    session["current_state"] = "COMPLETED"
    # Generate contextual acknowledgment before closing
    response = llm.generate_contextual_response(
        session, message, "ASK_EXPECTED_DATE",
        missing_fields=[],
    )
    response += "\n" + render("SAVE_DATE_CLOSE", date=date_value)
    return session, [_msg(sender_phone, response)]


# ---------------------------------------------------- SERVICE BOOKING STATES

def handle_ask_current_location(session, message, sender_phone):
    value = llm.extract_free_text(session["current_state"], message, "current location")
    session["current_location"] = value or message
    session["current_state"] = "ASK_DESTINATION_LOCATION"
    # Generate contextual response acknowledging the location
    response = llm.generate_contextual_response(
        session, message, "ASK_CURRENT_LOCATION",
        missing_fields=["destination_location"],
    )
    if "destination" not in response.lower() and "kahan" not in response.lower():
        response += "\n" + render("ASK_DESTINATION_LOCATION")
    return session, [_msg(sender_phone, response)]


def handle_ask_destination_location(session, message, sender_phone):
    value = llm.extract_free_text(session["current_state"], message, "destination location")
    session["destination_location"] = value or message
    session["service_city_confirmed"] = ""
    session["current_state"] = "ASK_SERVICE_CITY_CONFIRMATION"
    suggested_city = session["destination_location"] or "Delhi"
    # Generate contextual response with suggestion
    response = llm.generate_contextual_response(
        session, message, "ASK_DESTINATION_LOCATION",
        missing_fields=["service_city_confirmation"],
    )
    response += "\n" + render("ASK_SERVICE_CITY_SUGGESTION", suggested_city=suggested_city)
    return session, [_msg(sender_phone, response)]


def handle_ask_service_city_confirmation(session, message, sender_phone):
    answer = llm.classify_yes_no(session["current_state"], message)
    if answer == "YES":
        session["service_city_confirmed"] = "TRUE"
        session["extracted_service_location"] = session.get("destination_location", "Delhi") or "Delhi"
        session["current_state"] = "ASK_SERVICE_DATE"
        session["service_date_step"] = 0
        # Generate contextual response confirming the city
        response = llm.generate_contextual_response(
            session, message, "ASK_SERVICE_CITY_CONFIRMATION",
            missing_fields=["service_date"],
        )
        response += "\n" + get_service_date_prompt()
        return session, [_msg(sender_phone, response)]

    if answer == "NO":
        session["service_city_confirmed"] = "FALSE"
        session["current_state"] = "ASK_SERVICE_CITY_PREFERENCE"
        # Generate contextual response asking for preference
        response = llm.generate_contextual_response(
            session, message, "ASK_SERVICE_CITY_CONFIRMATION",
            missing_fields=["preferred_service_city"],
        )
        response += "\n" + render("ASK_PREFERRED_SERVICE_CITY")
        return session, [_msg(sender_phone, response)]

    # Unclear - resend suggestion
    response = llm.generate_contextual_response(
        session, message, "ASK_SERVICE_CITY_CONFIRMATION",
        missing_fields=["service_city_confirmation"],
    )
    return session, [_msg(sender_phone, response)]


def handle_ask_service_city_preference(session, message, sender_phone):
    value = llm.extract_free_text(session["current_state"], message, "preferred service city")
    session["extracted_service_location"] = value or message
    session["service_city_confirmed"] = "FALSE"
    session["current_state"] = "ASK_SERVICE_DATE"
    session["service_date_step"] = 0
    # Generate contextual response acknowledging the city preference
    response = llm.generate_contextual_response(
        session, message, "ASK_SERVICE_CITY_PREFERENCE",
        missing_fields=["service_date"],
    )
    response += "\n" + get_service_date_prompt()
    return session, [_msg(sender_phone, response)]


def handle_ask_service_date(session, message, sender_phone):
    value = llm.extract_date(session["current_state"], message)
    if value:
        session["service_date"] = value
        session["current_state"] = "ASK_SERVICE_TIME_WINDOW"
        # Generate contextual response acknowledging the date
        response = llm.generate_contextual_response(
            session, message, "ASK_SERVICE_DATE",
            missing_fields=["service_time_window"],
        )
        if "time" not in response.lower() and "baje" not in response.lower():
            response += "\n" + render("ASK_SERVICE_TIME_WINDOW")
        return session, [_msg(sender_phone, response)]

    answer = llm.classify_yes_no(session["current_state"], message)
    if answer == "YES":
        prompt = get_service_date_prompt()
        if "aaj" in prompt:
            session["service_date"] = datetime.now().date().isoformat()
        else:
            session["service_date"] = (datetime.now().date() + timedelta(days=1)).isoformat()
        session["current_state"] = "ASK_SERVICE_TIME_WINDOW"
        # Generate contextual response
        response = llm.generate_contextual_response(
            session, message, "ASK_SERVICE_DATE",
            missing_fields=["service_time_window"],
        )
        if "time" not in response.lower() and "baje" not in response.lower():
            response += "\n" + render("ASK_SERVICE_TIME_WINDOW")
        return session, [_msg(sender_phone, response)]

    if answer == "NO":
        session["service_date_step"] = 1
        session["current_state"] = "ASK_SERVICE_DATE_OPTIONS"
        # Generate contextual response offering options
        response = llm.generate_contextual_response(
            session, message, "ASK_SERVICE_DATE",
            missing_fields=["specific_service_date"],
        )
        response += "\n" + get_service_date_options_prompt()
        return session, [_msg(sender_phone, response)]

    # Unclear - generate contextual nudge
    response = llm.generate_contextual_response(
        session, message, "ASK_SERVICE_DATE",
        missing_fields=["service_date"],
    )
    return session, [_msg(sender_phone, response)]


def handle_ask_service_date_options(session, message, sender_phone):
    raw = message.strip().lower()
    date_value = None

    if raw in ("1", "1️⃣"):
        date_value = add_days_to_today(2)
    elif raw in ("2", "2️⃣"):
        date_value = add_days_to_today(4)
    elif raw in ("3", "3️⃣"):
        # Generate contextual response asking for custom date
        response = llm.generate_contextual_response(
            session, message, "ASK_SERVICE_DATE_OPTIONS",
            missing_fields=["specific_custom_date"],
        )
        return session, [_msg(sender_phone, response)]
    else:
        date_value = llm.extract_date(session["current_state"], message)

    if date_value:
        session["service_date"] = date_value
        session["current_state"] = "ASK_SERVICE_TIME_WINDOW"
        # Generate contextual response acknowledging the selected date
        response = llm.generate_contextual_response(
            session, message, "ASK_SERVICE_DATE_OPTIONS",
            missing_fields=["service_time_window"],
        )
        if "time" not in response.lower() and "baje" not in response.lower():
            response += "\n" + render("ASK_SERVICE_TIME_WINDOW")
        return session, [_msg(sender_phone, response)]

    # Generate contextual nudge to try again
    response = llm.generate_contextual_response(
        session, message, "ASK_SERVICE_DATE_OPTIONS",
        missing_fields=["valid_service_date"],
    )
    return session, [_msg(sender_phone, response)]


def handle_ask_service_time_window(session, message, sender_phone):
    value = llm.extract_time(session["current_state"], message)
    if not value:
        # Generate contextual nudge for valid time
        response = llm.generate_contextual_response(
            session, message, "ASK_SERVICE_TIME_WINDOW",
            missing_fields=["valid_time_window"],
        )
        return session, [_msg(sender_phone, response)]

    session["service_time_window"] = value
    if (session.get("driver_name") or session.get("driver_phone")) and not session.get("driver_contact_confirmed"):
        session["current_state"] = "ASK_DRIVER_CONTACT_CONFIRMATION"
        # Generate contextual response acknowledging the time and asking for driver confirmation
        response = llm.generate_contextual_response(
            session, message, "ASK_SERVICE_TIME_WINDOW",
            missing_fields=["driver_contact_confirmation"],
        )
        response += "\n" + render(
            "ASK_DRIVER_CONTACT_CONFIRMATION",
            driver_name=session.get("driver_name", ""),
            driver_phone=session.get("driver_phone", ""),
        )
        return session, [_msg(sender_phone, response)]

    session["current_state"] = "ASK_CONTACT_PERSON"
    # Generate contextual response acknowledging the time
    response = llm.generate_contextual_response(
        session, message, "ASK_SERVICE_TIME_WINDOW",
        missing_fields=["contact_person"],
    )
    response += "\n" + render("ASK_CONTACT_PERSON")
    return session, [_msg(sender_phone, response)]


def handle_driver_contact_confirmation(session, message, sender_phone):
    payload = _normalize_payload(message)
    if payload == "PAYLOAD_YES":
        answer = "YES"
    elif payload == "PAYLOAD_NO":
        answer = "NO"
    else:
        answer = llm.classify_yes_no(session["current_state"], message)

    if answer == "YES":
        session["driver_contact_confirmed"] = "TRUE"
        session["contact_person"] = session.get("driver_name", "Driver")
        session["contact_number"] = session.get("driver_phone", "NOT_PROVIDED")
        session["current_state"] = "CONFIRM_SUMMARY"
        # Generate contextual response confirming driver details
        response = llm.generate_contextual_response(
            session, message, "ASK_DRIVER_CONTACT_CONFIRMATION",
            missing_fields=[],
        )
        text = render(
            "BOOKING_SUMMARY",
            current_location=session.get("current_location", ""),
            service_location=session.get("extracted_service_location", ""),
            service_date=session.get("service_date", ""),
            service_time=session.get("service_time_window", session.get("service_time", "")),
            contact_person=session.get("contact_person", ""),
            contact_number=session.get("contact_number", ""),
        )
        response += "\n\n" + text
        return session, [_msg(sender_phone, response)]

    if answer == "NO":
        session["driver_contact_confirmed"] = "FALSE"
        session["awaiting_alternate_contact"] = "TRUE"
        session["current_state"] = "ASK_ALTERNATE_CONTACT"
        # Generate contextual response for alternate contact
        response = llm.generate_contextual_response(
            session, message, "ASK_DRIVER_CONTACT_CONFIRMATION",
            missing_fields=["alternate_contact_details"],
        )
        return session, [_msg(sender_phone, response)]

    text = render(
        "ASK_DRIVER_CONTACT_CONFIRMATION",
        driver_name=session.get("driver_name", ""),
        driver_phone=session.get("driver_phone", ""),
    )
    return session, [_button_message(sender_phone, text, [("PAYLOAD_YES", "YES"), ("PAYLOAD_NO", "NO")])]


def handle_ask_alternate_contact(session, message, sender_phone):
    raw = message.strip().lower()
    if "nahi" in raw or "no" in raw and "number" in raw or "not provided" in raw or "not provided" in message.lower():
        session["contact_person"] = "NOT_PROVIDED"
        session["contact_number"] = "NOT_PROVIDED"
        session["current_state"] = "CONFIRM_SUMMARY"
        # Generate contextual response when no alternate contact
        response = llm.generate_contextual_response(
            session, message, "ASK_ALTERNATE_CONTACT",
            missing_fields=[],
        )
        text = render(
            "BOOKING_SUMMARY",
            current_location=session.get("current_location", ""),
            service_location=session.get("extracted_service_location", ""),
            service_date=session.get("service_date", ""),
            service_time=session.get("service_time_window", session.get("service_time", "")),
            contact_person=session.get("contact_person", "NOT_PROVIDED"),
            contact_number=session.get("contact_number", "NOT_PROVIDED"),
        )
        response += "\n\n" + text
        return session, [_msg(sender_phone, response)]

    extracted = llm.extract_name_and_phone(session["current_state"], message)
    phone_match = PHONE_RE.search(extracted.get("phone", "") or message)
    if phone_match:
        # Normalize phone number (add 91 if 10 digits)
        normalized_phone = _normalize_phone_number(phone_match.group(1))
        session["contact_number"] = normalized_phone
        session["contact_person"] = extracted.get("name") or message
        session["current_state"] = "CONFIRM_SUMMARY"
        # Generate contextual response confirming alternate contact
        response = llm.generate_contextual_response(
            session, message, "ASK_ALTERNATE_CONTACT",
            missing_fields=[],
        )
        text = render(
            "BOOKING_SUMMARY",
            current_location=session.get("current_location", ""),
            service_location=session.get("extracted_service_location", ""),
            service_date=session.get("service_date", ""),
            service_time=session.get("service_time_window", session.get("service_time", "")),
            contact_person=session.get("contact_person", ""),
            contact_number=session.get("contact_number", ""),
        )
        response += "\n\n" + text
        return session, [_msg(sender_phone, response)]

    # Generate contextual nudge for valid contact details
    response = llm.generate_contextual_response(
        session, message, "ASK_ALTERNATE_CONTACT",
        missing_fields=["valid_contact_details"],
    )
    return session, [_msg(sender_phone, response)]


def handle_ask_contact_person(session, message, sender_phone):
    value = llm.extract_free_text(session["current_state"], message, "contact person name")
    raw = message.strip().lower()
    if "driver" in raw and session.get("driver_name"):
        value = f"Driver ({session['driver_name']})"
    session["contact_person"] = value or message
    session["current_state"] = "ASK_CONTACT_NUMBER"
    # Generate contextual response acknowledging the contact person
    response = llm.generate_contextual_response(
        session, message, "ASK_CONTACT_PERSON",
        missing_fields=["contact_number"],
    )
    if "number" not in response.lower() and "phone" not in response.lower():
        response += "\n" + render("ASK_CONTACT_NUMBER")
    return session, [_msg(sender_phone, response)]


def handle_ask_booking_correction(session, message, sender_phone):
    updated = False
    raw = message.strip().lower()

    if PHONE_RE.search(message):
        # Normalize phone number (add 91 if 10 digits)
        normalized_phone = _normalize_phone_number(PHONE_RE.search(message).group(1))
        session["contact_number"] = normalized_phone
        updated = True

    if "service location" in raw or ("service" in raw and "location" in raw):
        value = llm.extract_free_text(session["current_state"], message, "service location")
        if value:
            session["extracted_service_location"] = value
            updated = True

    if "destination" in raw or "kahan" in raw or "ja rahe" in raw:
        value = llm.extract_free_text(session["current_state"], message, "destination location")
        if value:
            session["destination_location"] = value
            updated = True

    if "vehicle location" in raw or "current location" in raw or ("location" in raw and "service" not in raw):
        value = llm.extract_free_text(session["current_state"], message, "current location")
        if value:
            session["current_location"] = value
            updated = True

    if "time" in raw or "baje" in raw or "+" in raw or "pm" in raw or "am" in raw:
        value = llm.extract_time(session["current_state"], message)
        if value:
            session["service_time_window"] = value
            updated = True

    if "date" in raw or "kal" in raw or "parso" in raw or "aaj" in raw or "july" in raw or "aug" in raw or "september" in raw or "oct" in raw:
        value = llm.extract_date(session["current_state"], message)
        if value:
            session["service_date"] = value
            updated = True

    if ("city" in raw or "service city" in raw or "preferred city" in raw) and not updated:
        value = llm.extract_free_text(session["current_state"], message, "preferred service city")
        if value:
            session["extracted_service_location"] = value
            updated = True

    if "contact person" in raw or "site" in raw or "phone" in raw or "number" in raw and not updated:
        value = llm.extract_free_text(session["current_state"], message, "contact person name")
        if value:
            session["contact_person"] = value
            updated = True

    if not updated:
        session["current_state"] = "ASK_BOOKING_CORRECTION"
        # Generate contextual nudge for valid corrections
        response = llm.generate_contextual_response(
            session, message, "ASK_BOOKING_CORRECTION",
            missing_fields=["valid_correction_details"],
        )
        return session, [_msg(sender_phone, response)]

    session["current_state"] = "CONFIRM_SUMMARY"
    text = render(
        "BOOKING_SUMMARY",
        current_location=session["current_location"],
        service_location=session.get("extracted_service_location", ""),
        service_date=session.get("service_date", ""),
        service_time=session.get("service_time_window", session.get("service_time", "")),
        contact_person=session.get("contact_person", ""),
        contact_number=session.get("contact_number", ""),
    )
    return session, [_msg(sender_phone, text)]


def handle_ask_contact_number(session, message, sender_phone):
    match = PHONE_RE.search(message)
    if not match:
        # Generate contextual nudge for valid phone number
        response = llm.generate_contextual_response(
            session, message, "ASK_CONTACT_NUMBER",
            missing_fields=["valid_contact_number"],
        )
        return session, [_msg(sender_phone, response)]

    # Normalize phone number (add 91 if 10 digits)
    normalized_phone = _normalize_phone_number(match.group(1))
    session["contact_number"] = normalized_phone
    session["current_state"] = "CONFIRM_SUMMARY"
    text = render(
        "BOOKING_SUMMARY",
        current_location=session["current_location"],
        service_location=session.get("extracted_service_location", ""),
        service_date=session.get("service_date", ""),
        service_time=session.get("service_time_window", session.get("service_time", "")),
        contact_person=session.get("contact_person", ""),
        contact_number=session.get("contact_number", ""),
    )
    # Generate contextual acknowledgment before showing summary
    response = llm.generate_contextual_response(
        session, message, "ASK_CONTACT_NUMBER",
        missing_fields=[],
    )
    response += "\n\n" + text
    return session, [_msg(sender_phone, response)]


def handle_confirm_summary(session, message, sender_phone):
    answer = llm.classify_yes_no(session["current_state"], message)

    if answer == "YES":
        ticket = ticket_service.create_ticket(session)
        session["ticket_id"] = ticket["ticket_id"]
        session["engineer_id"] = ticket["engineer_id"]
        session["current_state"] = "COMPLETED"
        # Generate contextual confirmation
        response = llm.generate_contextual_response(
            session, message, "CONFIRM_SUMMARY",
            missing_fields=[],
        )
        text = render(
            "BOOKING_CONFIRMED",
            ticket_id=ticket["ticket_id"],
            engineer_name=ticket["engineer_name"],
            engineer_phone=ticket["engineer_phone"],
        )
        response += "\n\n" + text
        return session, [_msg(sender_phone, response)]

    if answer == "NO":
        session["current_state"] = "ASK_BOOKING_CORRECTION"
        # Generate contextual response asking for corrections
        response = llm.generate_contextual_response(
            session, message, "CONFIRM_SUMMARY",
            missing_fields=["booking_details_to_correct"],
        )
        return session, [_msg(sender_phone, response)]

    # Unclear - ask to confirm yes or no
    response = llm.generate_contextual_response(
        session, message, "CONFIRM_SUMMARY",
        missing_fields=["booking_confirmation"],
    )
    return session, [_msg(sender_phone, response)]


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


def process_message(session: dict, message: str, sender_phone: str):
    """
    Entry point called by the webhook.
    Looks up the right handler for session['current_state'] and runs it.
    Returns (updated_session, outbound_messages).
    """
    state = session.get("current_state") or "START"

    if session.get("handler", "OWNER") == "OWNER" and state not in ("DRIVER_CONFIRM", "ASK_NEW_DRIVER", "ASK_HANDLER") and _is_driver_request(message):
        return _start_driver_handoff(session, sender_phone)

    handler = HANDLERS.get(state, handle_start)
    return handler(session, message, sender_phone)