"""
services/ticket_service.py

Creates a service ticket once the booking summary is confirmed.
Writes to a separate tickets.csv (acts like a "tickets" table).
"""

import os
import uuid
import pandas as pd
from filelock import FileLock
from config import settings
from services.engineer_service import assign_engineer

TICKET_COLUMNS = [
    "ticket_id", "vehicle_no", "issue_type", "current_location",
    "service_location", "service_date", "service_time",
    "contact_person", "contact_number", "engineer_id",
    "engineer_name", "engineer_phone", "status",
]


def _ensure_file():
    if not os.path.exists(settings.TICKETS_CSV):
        pd.DataFrame(columns=TICKET_COLUMNS).to_csv(settings.TICKETS_CSV, index=False)


def _load_tickets_df() -> pd.DataFrame:
    _ensure_file()
    return pd.read_csv(settings.TICKETS_CSV, dtype=str).fillna("")


def _find_existing_ticket(vehicle_no: str) -> dict | None:
    vehicle_no = str(vehicle_no or "").strip().lower()
    if not vehicle_no:
        return None

    with FileLock(settings.TICKETS_CSV + ".lock"):
        df = _load_tickets_df()

    match = df[df["vehicle_no"].str.strip().str.lower() == vehicle_no]
    if match.empty:
        return None

    ticket = match.iloc[-1].to_dict()
    ticket["existing_ticket"] = True
    return ticket


def get_ticket_by_id(ticket_id: str) -> dict | None:
    ticket_id = str(ticket_id or "").strip().upper()
    if not ticket_id:
        return None

    with FileLock(settings.TICKETS_CSV + ".lock"):
        df = _load_tickets_df()

    match = df[df["ticket_id"].str.strip().str.upper() == ticket_id]
    if match.empty:
        return None

    return match.iloc[-1].to_dict()


def create_ticket(session: dict) -> dict:
    _ensure_file()

    existing_ticket = _find_existing_ticket(session.get("vehicle_no", ""))
    if existing_ticket is not None and not session.get("force_new_ticket"):
        return existing_ticket

    engineer = assign_engineer(session.get("extracted_service_location", "")) if session.get("vehicle_state") != "GPS_DAMAGED" else {"engineer_id": "", "engineer_name": "", "engineer_phone": ""}

    ticket = {
        "ticket_id": "TKT-" + uuid.uuid4().hex[:8].upper(),
        "vehicle_no": session.get("vehicle_no", ""),
        "issue_type": session.get("root_cause", ""),
        "current_location": session.get("current_location", ""),
        "service_location": session.get("extracted_service_location", ""),
        "service_date": session.get("service_date", ""),
        "service_time": session.get("service_time_window", session.get("service_time", "")),
        "contact_person": session.get("contact_person", ""),
        "contact_number": session.get("contact_number", ""),
        "engineer_id": engineer.get("engineer_id", ""),
        "engineer_name": engineer.get("engineer_name", ""),
        "engineer_phone": engineer.get("phone_number", ""),
        "status": "ASSIGNED",
    }

    # Without this lock, two tickets created at nearly the same moment
    # (two different bookings, or a duplicate webhook for the same one)
    # could both read the same starting file and each write back their
    # own +1 row — the second write wins and the FIRST ticket silently
    # vanishes from tickets.csv, even though that customer was already
    # told "Ticket TKT-XXXX confirmed."
    with FileLock(settings.TICKETS_CSV + ".lock"):
        df = _load_tickets_df()
        df = pd.concat([df, pd.DataFrame([ticket])], ignore_index=True)
        df.to_csv(settings.TICKETS_CSV, index=False)

    ticket["existing_ticket"] = False
    return ticket