"""
reset_session.py

Quick CLI to reset a session back to current_state=START for testing,
so you don't have to hand-edit mock_sessions.csv every time.
Accepts either a phone number OR a vehicle number.

Usage:
    python reset_session.py 918882374849
    python reset_session.py MH12AB1234
    python reset_session.py MH12AB1234 --full     (also clears all extracted data)
"""

import sys
from core import session_manager

CLEAR_ON_FULL_RESET = [
    "root_cause", "physical_damage", "contact_person", "contact_number",
    "service_date", "service_time", "service_time_window",
    "extracted_appointment_date", "extracted_service_location",
    "service_city_confirmed", "service_date_step", "driver_contact_confirmed",
    "awaiting_alternate_contact", "destination_location",
    "ticket_id", "engineer_id", "current_location", "vehicle_state",
]


def reset(identifier: str, full: bool = False):
    session = session_manager.find_session(identifier) or session_manager.find_session_by_vehicle(identifier)

    if session is None:
        print(f"No session found for {identifier}")
        return

    session["current_state"] = "START"
    session["handler"] = "OWNER"

    if full:
        for field in CLEAR_ON_FULL_RESET:
            session[field] = ""
        print(f"Full reset: {session['vehicle_no']} ({session['phone_number']}) -> START (all extracted data cleared)")
    else:
        print(f"Reset: {session['vehicle_no']} ({session['phone_number']}) -> START")

    session_manager.update_session(session)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python reset_session.py <phone_number_or_vehicle_no> [--full]")
        sys.exit(1)

    identifier_arg = sys.argv[1]
    full_reset = "--full" in sys.argv
    reset(identifier_arg, full=full_reset)