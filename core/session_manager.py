"""
core/session_manager.py

Treats mock_sessions.csv as the database.
Every row = one active case, keyed by the OWNER's phone_number (this never
changes even when the conversation is handed off to the driver — we just
flip the `handler` column and use `driver_phone` for routing).

All reads/writes go through a file lock so two webhook calls arriving at
almost the same time don't corrupt the CSV.
"""

import os
from contextlib import contextmanager

import pandas as pd
from filelock import FileLock
from config import settings

CSV_PATH = settings.SESSIONS_CSV
LOCK_PATH = CSV_PATH + ".lock"

# Columns the CSV must always have. If you add a field to the flow,
# add it here too so create_session()/update_session() stay consistent.
COLUMNS = [
    "phone_number", "vehicle_no", "last_location", "timestamp", "gpstime",
    "main_powervoltage", "ismainpoerconnected", "gpsStatus",
    "driver_name", "driver_phone", "current_location", "destination_location",
    "vehicle_state", "current_state", "handler", "extracted_appointment_date",
    "extracted_service_location", "service_city_confirmed", "service_city_question_mode", "service_date_step",
    "service_date", "service_time", "service_time_window",
    "driver_contact_confirmed", "awaiting_alternate_contact",
    "root_cause", "physical_damage", "contact_person",
    "contact_number", "ticket_id", "engineer_id", "engineer_name", "engineer_phone", "last_prompt_text",
    "conversation_summary",
    "pending_quick_date",
]


def _load_df() -> pd.DataFrame:
    df = pd.read_csv(CSV_PATH, dtype=str).fillna("")
    # make sure every expected column exists even on an older CSV file
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df


def _save_df(df: pd.DataFrame) -> None:
    df.to_csv(CSV_PATH, index=False)


def find_session_by_vehicle(vehicle_no: str) -> dict | None:
    """
    Looks up an active session by vehicle number instead of phone number —
    useful for the outage-trigger job, which knows the vehicle, not the
    owner's phone number, until it loads the session.
    """
    with FileLock(LOCK_PATH):
        df = _load_df()

    vehicle_no = str(vehicle_no).strip().lower()
    match = df[df["vehicle_no"].str.strip().str.lower() == vehicle_no]

    if not match.empty:
        return match.iloc[0].to_dict()
    return None


def find_session(incoming_phone: str) -> dict | None:
    """
    Looks up an active session by whichever phone number just messaged us.
    Matches either:
      - phone_number == incoming_phone  (owner messaging)
      - driver_phone == incoming_phone AND handler == DRIVER (driver messaging)
    """
    with FileLock(LOCK_PATH):
        df = _load_df()

    incoming_phone = str(incoming_phone).strip()

    owner_match = df[df["phone_number"] == incoming_phone]
    if not owner_match.empty:
        return owner_match.iloc[0].to_dict()

    driver_match = df[(df["driver_phone"] == incoming_phone) & (df["handler"] == "DRIVER")]
    if not driver_match.empty:
        return driver_match.iloc[0].to_dict()

    return None


def update_session(session: dict) -> None:
    """
    Persists changes back to the CSV row identified by phone_number
    (the owner's number is the permanent primary key for the case).
    """
    with FileLock(LOCK_PATH):
        df = _load_df()
        key = session["phone_number"]
        idx = df.index[df["phone_number"] == key]

        if len(idx) == 0:
            # brand new session, append a row
            row = {col: str(session.get(col, "")) for col in COLUMNS}
            df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
        else:
            for col in COLUMNS:
                if col in session:
                    df.loc[idx, col] = str(session[col])

        _save_df(df)


def create_session(phone_number: str, vehicle_no: str, **extra) -> dict:
    """
    Used by the outage-trigger job (or manually) to open a brand-new case.
    """
    session = {col: "" for col in COLUMNS}
    session["phone_number"] = phone_number
    session["vehicle_no"] = vehicle_no
    session["current_state"] = "START"
    session["handler"] = "OWNER"
    session.update(extra)
    update_session(session)
    return session


def _lock_path_for(phone: str) -> str:
    safe = "".join(ch for ch in str(phone) if ch.isalnum()) or "unknown"
    return os.path.join(os.path.dirname(CSV_PATH) or ".", f".session_{safe}.lock")


@contextmanager
def session_transaction(incoming_phone: str):
    """
    Makes "find this session, let the caller process a turn, write it
    back" one atomic, cross-process-safe operation for THIS phone number.

    Why this exists: find_session() and a separate later update_session()
    call left a window open between the read and the write (state_machine
    processing, including LLM calls, happens in between). A WhatsApp
    webhook retry, a double-tapped button, or two rapid messages from the
    same person could both read the same starting state in that window —
    whichever call wrote back last would silently erase the other's
    update. Holding a lock for the whole turn closes that window.

    Scoped per-phone-number (its own lock file) rather than one shared
    lock, so different customers' conversations still process fully in
    parallel — only messages for the SAME session are serialized.

    Usage:
        with session_manager.session_transaction(sender_phone) as session:
            if session is None:
                ...  # no active case
                return
            updated_session, outbound = state_machine.process_message(...)
            # no need to call update_session() yourself — it happens
            # automatically here, using whatever `session` looks like
            # when the "with" block exits (handlers mutate it in place)

    Known limitation: if the owner and driver on the SAME case happen to
    message in at the exact same instant (one lock per phone number, and
    owner/driver are different numbers), that specific cross-number race
    isn't covered. Everything else is.
    """
    incoming_phone = str(incoming_phone).strip()
    with FileLock(_lock_path_for(incoming_phone)):
        session = find_session(incoming_phone)
        yield session
        if session is not None:
            update_session(session)