"""
services/engineer_service.py

Simple zone-matching lookup — no AI needed. Matches the requested
service location text against each engineer's zone; falls back to the
"Default" zone engineer if nothing matches.
"""

import pandas as pd
from config import settings


def assign_engineer(service_location: str) -> dict:
    df = pd.read_csv(settings.ENGINEERS_CSV, dtype=str).fillna("")
    service_location = (service_location or "").strip().lower()

    for _, row in df.iterrows():
        if row["zone"].strip().lower() in service_location or service_location in row["zone"].strip().lower():
            return row.to_dict()

    default_row = df[df["zone"].str.lower() == "default"]
    if not default_row.empty:
        return default_row.iloc[0].to_dict()

    return df.iloc[0].to_dict()
