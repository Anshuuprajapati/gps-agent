"""
services/gps_service.py

This is the "real world" check — no AI involved.
In production, replace `_read_live_telemetry()` with an actual call to your
GPS vendor's API. Right now it reads straight from the CSV so you can test
end-to-end by editing mock_sessions.csv yourself (e.g. flip gpsStatus to 1
to simulate the device coming back online).
"""

BATTERY_VOLTAGE_THRESHOLD = 11.5  # below this = battery considered low


def _read_live_telemetry(session: dict) -> dict:
    """Stand-in for a real GPS vendor API call."""
    return {
        "voltage": float(session.get("main_powervoltage") or 0),
        "main_power_connected": str(session.get("ismainpoerconnected")) == "1",
        "gps_online": str(session.get("gpsStatus")) == "1",
    }


def analyze_root_cause(session: dict) -> str:
    """
    Runs once, right after outage detection (PRE_ANALYSIS).
    Returns one of: "BATTERY", "MAIN_POWER", "UNKNOWN"
    """
    telemetry = _read_live_telemetry(session)

    if telemetry["gps_online"]:
        return "UNKNOWN"  # shouldn't normally be called if GPS is fine

    if not telemetry["main_power_connected"]:
        return "MAIN_POWER"

    if telemetry["voltage"] < BATTERY_VOLTAGE_THRESHOLD:
        return "BATTERY"

    return "UNKNOWN"


def verify_gps(session: dict) -> bool:
    """
    Called after the owner/driver says "Done".
    Re-checks telemetry and returns True if the GPS is back online.
    """
    telemetry = _read_live_telemetry(session)
    return telemetry["gps_online"]


def is_power_issue_resolved(session: dict, issue_type: str) -> bool:
    """
    Used when GPS is still offline but we want to know if the SPECIFIC
    issue (battery / main power) has been fixed, even if GPS hasn't
    caught up yet.
    """
    telemetry = _read_live_telemetry(session)
    if issue_type == "BATTERY":
        return telemetry["voltage"] >= BATTERY_VOLTAGE_THRESHOLD
    if issue_type == "MAIN_POWER":
        return telemetry["main_power_connected"]
    return False
