"""
services/driver_service.py

Handles the "talk to my driver instead" handoff.
Key idea: it's the SAME session row (same phone_number primary key).
We just flip `handler` to DRIVER and point routing at `driver_phone`.
The owner's number goes quiet; the driver's number now drives the
conversation, but shares the same context/history.
"""


def get_driver_details(session: dict) -> dict:
    return {
        "name": session.get("driver_name", ""),
        "phone": session.get("driver_phone", ""),
    }


def update_driver_details(session: dict, name: str, phone: str) -> dict:
    session["driver_name"] = name
    session["driver_phone"] = phone
    return session


def transfer_to_driver(session: dict) -> dict:
    """
    Switches the active handler to DRIVER.
    The owner's session effectively closes (no more replies expected from
    them); the driver becomes the one the state machine listens to next.
    """
    session["handler"] = "DRIVER"
    return session
