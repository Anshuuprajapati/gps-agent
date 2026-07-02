"""
core/session_manager.py

Treats mock_sessions.csv as the database.
Every row = one active case, keyed by the OWNER's phone_number (this never
changes even when the conversation is handed off to the driver — we just
flip the `handler` column and use `driver_phone` for routing).

All reads/writes go through a file lock so two webhook calls arriving at
almost the same time don't corrupt the CSV.
"""

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
    "driver_name", "driver_phone", "current_location", "vehicle_state",
    "current_state", "handler", "extracted_appointment_date",
    "extracted_service_location", "root_cause", "physical_damage",
    "contact_person", "contact_number", "service_date", "service_time",
    "ticket_id", "engineer_id",
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