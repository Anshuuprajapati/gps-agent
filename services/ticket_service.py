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


def create_ticket(session: dict) -> dict:
    _ensure_file()

    engineer = assign_engineer(session.get("extracted_service_location", ""))

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
        df = pd.read_csv(settings.TICKETS_CSV, dtype=str).fillna("")
        df = pd.concat([df, pd.DataFrame([ticket])], ignore_index=True)
        df.to_csv(settings.TICKETS_CSV, index=False)

    return ticket