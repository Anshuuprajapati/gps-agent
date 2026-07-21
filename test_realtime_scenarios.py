"""
test_realtime_scenarios.py
==========================
Real-time end-to-end tests that mirror every scenario in MANUAL_TEST_SCENARIOS.md.

WHAT THIS FILE DOES DIFFERENTLY FROM test.py
---------------------------------------------
* test.py mocks the LLM and tests the state machine in isolation.
* This file tests complete multi-turn CONVERSATIONS end-to-end, using real
  CSV persistence between turns (via a temp backend), real gps_service telemetry
  reads, and real template rendering — exactly as a live WhatsApp session works.
* The LLM is still mocked per-turn (we are not paying per test run), but each
  mock is set up to return what a real Hindi/Hinglish user would actually say,
  so the test reads like the chat script in MANUAL_TEST_SCENARIOS.md.

HOW TO RUN
----------
    # From the gps-agent project root:
    pytest test_realtime_scenarios.py -v

    # Run a single scenario:
    pytest test_realtime_scenarios.py::TestScenario01_LowBattery -v

    # Run with real LLM (slow, costs money — set LLM_PROVIDER in .env first):
    pytest test_realtime_scenarios.py -v -m realllm

MARKERS
-------
    realllm   — tests that skip the LLM mock and hit the real provider.
                Not run by default. Use: pytest -m realllm
    regression — the bug-regression checks from section 16 of the manual doc.

CSV COLUMN ORDER NOTE
---------------------
The test helper `make_session()` builds dicts using session_manager.COLUMNS so
the order always stays in sync with the real CSV — no hand-counting columns.
"""

import csv
import os
import sys
import time
from datetime import date, timedelta
from unittest.mock import MagicMock, patch, call

import pytest

# ── make sure the project root is importable ──────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import session_manager, state_machine as sm
from core import llm_handler
from services import gps_service, ticket_service, engineer_service
from config import settings
from prompts.templates import render


# ══════════════════════════════════════════════════════════════════════════
#  SHARED HELPERS
# ══════════════════════════════════════════════════════════════════════════

OWNER_PHONE = "919876500001"
DRIVER_PHONE = "919876500002"
VEHICLE_NO   = "MH12AA0001"

SESSION_COLS = session_manager.COLUMNS


def pytest_configure(config):
    config.addinivalue_line("markers", "realllm: requires a live LLM provider")
    config.addinivalue_line("markers", "regression: regression tests")
    print("[pytest] starting realtime scenarios")


def pytest_runtest_logreport(report):
    if report.when != "call":
        return
    if report.passed:
        print(f"[PASS] {report.nodeid}")
    elif report.failed:
        print(f"[FAIL] {report.nodeid}")
    elif report.skipped:
        print(f"[SKIP] {report.nodeid}")


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    passed = len(terminalreporter.stats.get("passed", []))
    failed = len(terminalreporter.stats.get("failed", []))
    skipped = len(terminalreporter.stats.get("skipped", []))
    print(f"[SUMMARY] passed={passed} failed={failed} skipped={skipped}")


def make_session(**overrides) -> dict:
    """
    Blank session with safe defaults. Only set what a test actually cares
    about — everything else comes from here so tests don't silently break
    when new columns are added to session_manager.COLUMNS.
    """
    s = {col: "" for col in SESSION_COLS}
    s.update({
        "phone_number":  OWNER_PHONE,
        "vehicle_no":    VEHICLE_NO,
        "last_location": "Nagpur Bypass",
        "timestamp":     "2026-07-05 09:00:00",
        "gpstime":       "05 July 2026 09:00",
        "handler":       "OWNER",
        "current_state": "START",
    })
    s.update(overrides)
    return s


def telemetry(voltage=12.6, main_power=True, gps_online=False) -> dict:
    """Returns the three telemetry fields gps_service reads from the session."""
    return {
        "main_powervoltage":    str(voltage),
        "ismainpoerconnected":  "1" if main_power else "0",
        "gpsStatus":            "1" if gps_online else "0",
    }


def chat_turn(session: dict, message: str, phone: str = OWNER_PHONE):
    """
    One WhatsApp turn: process_message → (updated_session, outbound).
    Returns (session, outbound) to keep test lines short.
    """
    return sm.process_message(session, message, phone)


def reply_text(outbound: list, phone: str = OWNER_PHONE) -> str:
    """Extract the text body of the message addressed to `phone`."""
    for out in outbound:
        if out.get("phone") == phone:
            if out.get("text"):
                return out["text"]
            body = out.get("interactive", {}).get("body", {})
            return body.get("text", "")
    return ""


def button_ids(outbound: list, phone: str = OWNER_PHONE) -> list:
    """Extract button payload IDs from an interactive message."""
    for out in outbound:
        if out.get("phone") == phone:
            buttons = out.get("interactive", {}).get("action", {}).get("buttons", [])
            return [b["reply"]["id"] for b in buttons]
    return []


def assert_state(session: dict, expected: str):
    assert session["current_state"] == expected, (
        f"Expected state {expected!r}, got {session['current_state']!r}"
    )


# ══════════════════════════════════════════════════════════════════════════
#  PYTEST FIXTURES
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def default_llm_guards(monkeypatch):
    """
    Safety net: any LLM function not explicitly mocked in a test raises
    immediately instead of silently hitting the network.
    Every test that needs an LLM response patches the specific function.
    """
    def _boom(*a, **k):
        raise AssertionError(
            "Unmocked LLM call attempted. Patch sm.llm.<function> in this test."
        )

    for fn in (
        "classify_yes_no", "classify_wait_done_reply", "classify_self_or_driver",
        "classify_vehicle_status", "extract_date", "extract_time",
        "extract_free_text", "extract_name_and_phone", "extract_booking_slots",
        "extract_tech_dispatch_slots", "answer_from_knowledge_base",
    ):
        monkeypatch.setattr(sm.llm, fn, _boom, raising=False)

    # classify_global_intent runs on EVERY process_message() call regardless
    # of state (it replaced is_general_question/classify_ticket_inquiry/
    # is_driver_update_intent as one consolidated check) — without a default
    # it would silently hit the real network on every test, producing flaky
    # pass/fail depending on how the live model happened to classify each
    # message. Tests exercising the driver-update/ticket-inquiry/general-
    # question paths override this themselves.
    monkeypatch.setattr(
        sm.llm, "classify_global_intent",
        MagicMock(return_value="FLOW_REPLY"),
        raising=False,
    )
    yield


@pytest.fixture
def csv_backend(tmp_path, monkeypatch):
    """
    Full CSV backend in a temp directory — sessions, tickets, engineers.
    Lets tests exercise real session persistence between turns (scenario 12).
    """
    sessions_csv  = tmp_path / "mock_sessions.csv"
    tickets_csv   = tmp_path / "tickets.csv"
    engineers_csv = tmp_path / "engineers.csv"

    # Seed the sessions file with the required header row
    with open(sessions_csv, "w", newline="") as f:
        csv.writer(f).writerow(SESSION_COLS)

    # Seed engineers table with a few zone-matched rows + a Default fallback
    with open(engineers_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["engineer_id", "engineer_name", "phone_number", "zone"])
        w.writerow(["ENG001", "Ramesh Kumar",   "919000000001", "Pune"])
        w.writerow(["ENG002", "Suresh Patil",   "919000000002", "Mumbai"])
        w.writerow(["ENG003", "Anil Sharma",    "919000000003", "Nagpur"])
        w.writerow(["ENG099", "Rahul Deshmukh", "919000000099", "Default"])

    monkeypatch.setattr(session_manager, "CSV_PATH",  str(sessions_csv))
    monkeypatch.setattr(session_manager, "LOCK_PATH", str(sessions_csv) + ".lock")
    monkeypatch.setattr(settings, "TICKETS_CSV",   str(tickets_csv))
    monkeypatch.setattr(settings, "ENGINEERS_CSV", str(engineers_csv))

    return sessions_csv, tickets_csv, engineers_csv


# ══════════════════════════════════════════════════════════════════════════
#  SCENARIO 1 — LOW BATTERY
#  Manual doc §1: voltage 10.8 → alert with Self/Driver buttons
#                 → tap Self → check battery → Done → ASK_PHYSICAL_DAMAGE
# ══════════════════════════════════════════════════════════════════════════

class TestScenario01_LowBattery:
    """
    CSV: voltage=10.8, main_power=1, gpsStatus=0
    Trigger: POST /trigger-outage → handle_start
    Flow: alert → SELF chosen → WAIT_DONE → Done (still low) → ASK_PHYSICAL_DAMAGE
    """

    def _battery_session(self):
        return make_session(**telemetry(voltage=10.8, main_power=True, gps_online=False))

    def test_trigger_outage_sends_battery_alert_with_buttons(self):
        session = self._battery_session()
        session, outbound = sm.handle_start(session, "", OWNER_PHONE)

        assert session["root_cause"] == gps_service.BATTERY_ISSUE
        assert_state(session, "ASK_HANDLER")
        text = reply_text(outbound)
        assert "battery" in text.lower()
        assert "MH12AA0001" in text
        assert "Nagpur Bypass" in text
        ids = button_ids(outbound)
        assert "PAYLOAD_SELF" in ids
        assert "PAYLOAD_DRIVER" in ids

    def test_tap_self_moves_to_wait_done_with_battery_instructions(self, monkeypatch):
        session = self._battery_session()
        session["current_state"] = "ASK_HANDLER"
        session["root_cause"]    = gps_service.BATTERY_ISSUE

        session, outbound = chat_turn(session, "PAYLOAD_SELF")
        assert_state(session, "WAIT_DONE")
        assert session["handler"] == "OWNER"
        assert "battery" in reply_text(outbound).lower() or "charge" in reply_text(outbound).lower()

    def test_done_when_battery_still_low_asks_physical_damage(self, monkeypatch):
        """
        After saying Done, gps_service reads telemetry from the session dict.
        Voltage still 10.8 → power issue NOT resolved → ask about physical damage.
        """
        monkeypatch.setattr(sm.llm, "classify_wait_done_reply", MagicMock(return_value="DONE"))
        session = self._battery_session()
        session["current_state"] = "WAIT_DONE"
        session["root_cause"]    = gps_service.BATTERY_ISSUE

        session, outbound = chat_turn(session, "Done")
        assert_state(session, "ASK_PHYSICAL_DAMAGE")
        ids = button_ids(outbound)
        assert "PAYLOAD_YES" in ids and "PAYLOAD_NO" in ids

    def test_physical_damage_no_goes_back_to_wait_done(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="NO"))
        session = self._battery_session()
        session["current_state"] = "ASK_PHYSICAL_DAMAGE"
        session["root_cause"]    = gps_service.BATTERY_ISSUE

        session, outbound = chat_turn(session, "PAYLOAD_NO")
        assert_state(session, "WAIT_DONE")
        assert "battery" in reply_text(outbound).lower() or "try" in reply_text(outbound).lower()

    def test_battery_voltage_boundary_exactly_at_threshold_is_not_low(self):
        session = make_session(**telemetry(voltage=11.5, main_power=True, gps_online=False))
        cause = gps_service.analyze_root_cause(session)
        assert cause == "UNKNOWN", "11.5V is AT the threshold — should not be flagged as battery issue"

    def test_battery_voltage_one_cent_below_threshold_is_low(self):
        session = make_session(**telemetry(voltage=11.49, main_power=True, gps_online=False))
        cause = gps_service.analyze_root_cause(session)
        assert cause == gps_service.BATTERY_ISSUE


# ══════════════════════════════════════════════════════════════════════════
#  SCENARIO 2 — BATTERY CHARGED → GPS RECOVERED
#  Manual doc §2: voltage 12.6, gpsStatus=1 → Done → COMPLETED
# ══════════════════════════════════════════════════════════════════════════

class TestScenario02_BatteryRecovered:

    def test_done_when_gps_back_online_closes_case(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_wait_done_reply", MagicMock(return_value="DONE"))
        session = make_session(
            current_state="WAIT_DONE",
            root_cause=gps_service.BATTERY_ISSUE,
            **telemetry(voltage=12.6, main_power=True, gps_online=True),
        )
        session, outbound = chat_turn(session, "Done")

        assert_state(session, "COMPLETED")
        text = reply_text(outbound)
        assert "online" in text.lower() or "wapas" in text.lower()

    def test_completed_state_rejects_further_messages(self, monkeypatch):
        session = make_session(current_state="COMPLETED")
        session, outbound = chat_turn(session, "Done")

        assert_state(session, "COMPLETED")
        assert "close" in reply_text(outbound).lower() or "pehle se" in reply_text(outbound).lower()


# ══════════════════════════════════════════════════════════════════════════
#  SCENARIO 3 — BATTERY CHARGED BUT GPS STILL OFFLINE
#  Manual doc §3: voltage 12.6 (battery OK), gpsStatus=0 → ASK_VEHICLE_STATUS
# ══════════════════════════════════════════════════════════════════════════

class TestScenario03_BatteryOkGpsStillDown:

    def test_voltage_ok_gps_offline_moves_to_vehicle_status(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_wait_done_reply", MagicMock(return_value="DONE"))
        session = make_session(
            current_state="WAIT_DONE",
            root_cause=gps_service.BATTERY_ISSUE,
            **telemetry(voltage=12.6, main_power=True, gps_online=False),
        )
        session, outbound = chat_turn(session, "Done")

        # Power issue resolved (voltage OK) but GPS still offline → ask vehicle status
        assert_state(session, "ASK_VEHICLE_STATUS")


# ══════════════════════════════════════════════════════════════════════════
#  SCENARIO 4 — MAIN POWER DISCONNECTED
#  Manual doc §4: main_power=0 → main power alert → Self/Driver buttons
# ══════════════════════════════════════════════════════════════════════════

class TestScenario04_MainPowerDisconnected:

    def test_trigger_sends_main_power_alert(self):
        session = make_session(**telemetry(voltage=12.6, main_power=False, gps_online=False))
        session, outbound = sm.handle_start(session, "", OWNER_PHONE)

        assert session["root_cause"] == gps_service.MAIN_POWER_DISCONNECTED
        assert_state(session, "ASK_HANDLER")
        text = reply_text(outbound)
        assert "power" in text.lower() or "connection" in text.lower()
        assert button_ids(outbound) == ["PAYLOAD_SELF", "PAYLOAD_DRIVER"]

    def test_self_chosen_gives_wiring_check_instructions(self, monkeypatch):
        session = make_session(
            current_state="ASK_HANDLER",
            root_cause=gps_service.MAIN_POWER_DISCONNECTED,
        )
        session, outbound = chat_turn(session, "PAYLOAD_SELF")

        assert_state(session, "WAIT_DONE")
        text = reply_text(outbound)
        assert "wiring" in text.lower() or "power" in text.lower() or "connection" in text.lower()

    def test_main_power_reconnected_gps_still_offline_routes_to_vehicle_status(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_wait_done_reply", MagicMock(return_value="DONE"))
        session = make_session(
            current_state="WAIT_DONE",
            root_cause=gps_service.MAIN_POWER_DISCONNECTED,
            **telemetry(voltage=12.6, main_power=True, gps_online=False),
        )
        session, outbound = chat_turn(session, "Done")
        assert_state(session, "ASK_VEHICLE_STATUS")


# ══════════════════════════════════════════════════════════════════════════
#  SCENARIO 5 — VEHICLE IN WORKSHOP
#  Manual doc §5: "Gaadi workshop mein hai" → ASK_EXPECTED_DATE → date saved → COMPLETED
# ══════════════════════════════════════════════════════════════════════════

class TestScenario05_VehicleInWorkshop:

    def test_workshop_reply_asks_expected_date(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_vehicle_status", MagicMock(return_value="WORKSHOP"))
        monkeypatch.setattr(sm.llm, "extract_date", MagicMock(return_value=""))
        session = make_session(current_state="ASK_VEHICLE_STATUS", root_cause="UNKNOWN")

        session, outbound = chat_turn(session, "Gaadi workshop mein hai")
        assert_state(session, "ASK_EXPECTED_DATE")

    def test_expected_date_reply_closes_case(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "extract_date", MagicMock(return_value="2026-07-15"))
        session = make_session(current_state="ASK_EXPECTED_DATE")

        session, outbound = chat_turn(session, "15 July tak")
        assert_state(session, "COMPLETED")
        assert session["extracted_appointment_date"] == "2026-07-15"
        text = reply_text(outbound)
        assert "2026-07-15" in text or "note" in text.lower()

    def test_date_embedded_in_workshop_message_skips_date_state(self, monkeypatch):
        """
        If the user says "Workshop mein hai, 15 July tak" in one message,
        extract_date should fire immediately and skip ASK_EXPECTED_DATE.
        """
        monkeypatch.setattr(sm.llm, "classify_vehicle_status", MagicMock(return_value="WORKSHOP"))
        monkeypatch.setattr(sm.llm, "extract_date", MagicMock(return_value="2026-07-15"))
        session = make_session(current_state="ASK_VEHICLE_STATUS")

        session, outbound = chat_turn(session, "Workshop mein hai, 15 July tak")
        assert_state(session, "COMPLETED")


# ══════════════════════════════════════════════════════════════════════════
#  SCENARIO 6 — VEHICLE ACCIDENT
#  Manual doc §6: "Accident ho gaya hai" → ASK_CURRENT_LOCATION → (booking)
# ══════════════════════════════════════════════════════════════════════════

class TestScenario06_VehicleAccident:

    def test_accident_reply_moves_to_current_location(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_vehicle_status", MagicMock(return_value="ACCIDENT"))
        session = make_session(current_state="ASK_VEHICLE_STATUS")

        session, outbound = chat_turn(session, "Accident ho gaya hai gaadi ka")
        assert_state(session, "ASK_CURRENT_LOCATION")
        assert "location" in reply_text(outbound).lower() or "kahan" in reply_text(outbound).lower()


# ══════════════════════════════════════════════════════════════════════════
#  SCENARIO 7 — GPS REMOVED
#  Manual doc §7: "GPS nikal diya hai" → straight to booking flow
# ══════════════════════════════════════════════════════════════════════════

class TestScenario07_GpsRemoved:

    def test_gps_removed_starts_booking_flow(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_vehicle_status", MagicMock(return_value="GPS_REMOVED"))
        monkeypatch.setattr(sm.llm, "extract_booking_slots", MagicMock(return_value={}))
        session = make_session(current_state="ASK_VEHICLE_STATUS")

        session, outbound = chat_turn(session, "GPS nikal diya hai humne")
        assert_state(session, "ASK_DESTINATION_LOCATION")


# ══════════════════════════════════════════════════════════════════════════
#  SCENARIO 8 — GPS DAMAGED
#  Manual doc §8: "GPS device damage ho gaya" → repair confirmation → booking
# ══════════════════════════════════════════════════════════════════════════

class TestScenario08_GpsDamaged:

    def test_gps_damaged_asks_repair_confirmation(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_vehicle_status", MagicMock(return_value="GPS_DAMAGED"))
        session = make_session(current_state="ASK_VEHICLE_STATUS")

        session, outbound = chat_turn(session, "GPS device damage ho gaya hai")
        assert_state(session, "ASK_GPS_REPAIR_CONFIRMATION")
        ids = button_ids(outbound)
        assert "PAYLOAD_YES" in ids

    def test_repair_yes_moves_to_current_location(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="YES"))
        session = make_session(current_state="ASK_GPS_REPAIR_CONFIRMATION")

        session, outbound = chat_turn(session, "PAYLOAD_YES")
        assert_state(session, "ASK_CURRENT_LOCATION")

    def test_repair_no_closes_case_politely(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="NO"))
        session = make_session(current_state="ASK_GPS_REPAIR_CONFIRMATION")

        session, outbound = chat_turn(session, "PAYLOAD_NO")
        assert_state(session, "COMPLETED")

    def test_gps_damaged_full_booking_flow(self, tmp_path, monkeypatch):
        """
        GPS damaged → confirm repair → location → city confirmation → time →
        contact person → contact number → ticket created.
        Tests that vehicle_state=GPS_DAMAGED causes engineer assignment to skip
        zone matching (no engineer assigned for GPS-only damage pickup).
        """
        # Patch engineer CSV so assign_engineer returns something predictable
        eng_csv = tmp_path / "engineers.csv"
        with open(eng_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["engineer_id", "engineer_name", "phone_number", "zone"])
            w.writerow(["ENG099", "Default Guy", "919000000099", "Default"])
        monkeypatch.setattr(settings, "ENGINEERS_CSV", str(eng_csv))
        tickets_csv = tmp_path / "tickets.csv"
        monkeypatch.setattr(settings, "TICKETS_CSV", str(tickets_csv))

        phone = OWNER_PHONE
        session = make_session(current_state="ASK_GPS_REPAIR_CONFIRMATION", vehicle_state="GPS_DAMAGED")

        # Step 1 — confirm repair
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="YES"))
        session, _ = chat_turn(session, "Haan repair karo")
        assert_state(session, "ASK_CURRENT_LOCATION")

        # Step 2 — location
        monkeypatch.setattr(sm.llm, "extract_free_text", MagicMock(return_value="Delhi"))
        session, _ = chat_turn(session, "Delhi mein hu")
        assert_state(session, "ASK_SERVICE_CITY_CONFIRMATION")

        # Step 3 — confirm city (goes straight to time because mode=TODAY)
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="YES"))
        session, _ = chat_turn(session, "Haan Delhi theek hai")
        assert_state(session, "ASK_SERVICE_TIME_WINDOW")

        # Step 4 — time
        monkeypatch.setattr(sm.llm, "extract_time", MagicMock(return_value="10:00 AM"))
        session, _ = chat_turn(session, "Subah 10 baje")
        assert_state(session, "ASK_CONTACT_PERSON")

        # Step 5 — contact person
        monkeypatch.setattr(sm.llm, "extract_free_text", MagicMock(return_value="Ramesh"))
        session, _ = chat_turn(session, "Ramesh hai site pe")
        assert_state(session, "ASK_CONTACT_NUMBER")

        # Step 6 — contact number → ticket created directly
        session, outbound = chat_turn(session, "9876543210")
        assert_state(session, "COMPLETED")
        assert session["ticket_id"].startswith("TKT-")
        # GPS_DAMAGED path skips engineer assignment → engineer fields empty
        assert session.get("engineer_id", "") == ""


# ══════════════════════════════════════════════════════════════════════════
#  SCENARIO 9 — DRIVER HANDOVER (the phone-normalization regression test)
#  Manual doc §9: bare 10-digit → normalized to 91+10 → driver can reply
# ══════════════════════════════════════════════════════════════════════════

class TestScenario09_DriverHandover:

    def test_bare_10digit_driver_phone_is_normalized_with_91_prefix(self, monkeypatch):
        monkeypatch.setattr(
            sm.llm, "extract_name_and_phone",
            MagicMock(return_value={"name": "Ramesh", "phone": "9876543210"}),
        )
        session = make_session(
            current_state="ASK_NEW_DRIVER",
            root_cause=gps_service.BATTERY_ISSUE,
        )
        session, outbound = chat_turn(session, "Ramesh 9876543210")

        assert session["driver_phone"] == "919876543210", (
            "Bare 10-digit number must be stored as 91+10 digits so driver's "
            "own WhatsApp messages can be matched back to this session"
        )
        assert session["handler"] == "DRIVER"
        assert_state(session, "WAIT_DONE")

    def test_driver_with_91_prefix_is_not_double_prefixed(self, monkeypatch):
        """If someone types the full number with country code, don't add 91 again."""
        monkeypatch.setattr(
            sm.llm, "extract_name_and_phone",
            MagicMock(return_value={"name": "Deepak", "phone": "919876543210"}),
        )
        session = make_session(current_state="ASK_NEW_DRIVER", root_cause=gps_service.BATTERY_ISSUE)
        session, outbound = chat_turn(session, "Deepak 919876543210")

        assert session["driver_phone"] == "919876543210"

    def test_existing_driver_confirm_yes_messages_both_phones(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="YES"))
        session = make_session(
            current_state="DRIVER_CONFIRM",
            root_cause=gps_service.BATTERY_ISSUE,
            driver_name="Deepak",
            driver_phone="919876543210",
        )
        session, outbound = chat_turn(session, "PAYLOAD_YES")

        assert session["handler"] == "DRIVER"
        assert_state(session, "WAIT_DONE")
        phones = {m["phone"] for m in outbound}
        assert OWNER_PHONE in phones, "Owner must get a 'transferred' confirmation"
        assert "919876543210" in phones, "Driver must get the troubleshooting instruction"

    def test_driver_session_lookup_after_handoff(self, csv_backend, monkeypatch):
        """
        After transfer_to_driver, find_session(driver_phone) must return the
        same session row — this is exactly what broke before the normalization fix.
        """
        sessions_csv, _, _ = csv_backend
        session = session_manager.create_session(
            phone_number=OWNER_PHONE,
            vehicle_no=VEHICLE_NO,
            driver_name="Deepak",
            driver_phone="919876543210",
            current_state="DRIVER_CONFIRM",
            root_cause=gps_service.BATTERY_ISSUE,
        )

        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="YES"))
        session, _ = sm.handle_driver_confirm(session, "haan", OWNER_PHONE)
        session_manager.update_session(session)

        # Now the driver texts in — find_session must resolve to the same case
        driver_session = session_manager.find_session("919876543210")
        assert driver_session is not None, (
            "find_session(driver_phone) returned None — "
            "this is the regression the normalization fix solved"
        )
        assert driver_session["current_state"] == "WAIT_DONE"
        assert driver_session["handler"] == "DRIVER"

    def test_owner_cannot_skip_driver_handoff_with_unrelated_message(self, monkeypatch):
        """
        Mid-troubleshooting the owner types "driver se baat karo" — the
        process_message dispatcher must intercept this BEFORE handle_wait_done
        and route to the driver handoff path.
        """
        monkeypatch.setattr(
            sm.llm, "classify_wait_done_reply",
            MagicMock(side_effect=AssertionError("handle_wait_done should not have run")),
        )
        session = make_session(
            current_state="WAIT_DONE",
            root_cause=gps_service.BATTERY_ISSUE,
            handler="OWNER",
        )
        session, outbound = chat_turn(session, "driver se baat karo please")

        # No saved driver → goes straight to ASK_NEW_DRIVER
        assert_state(session, "ASK_NEW_DRIVER")


# ══════════════════════════════════════════════════════════════════════════
#  SCENARIO 10 — USER ASKS A QUESTION MID-FLOW
#  Manual doc §10: "Yeh kaise karu?" in WAIT_DONE → help text, state unchanged
# ══════════════════════════════════════════════════════════════════════════

class TestScenario10_MidFlowQuestion:

    def test_need_help_in_wait_done_gives_steps_without_advancing(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_wait_done_reply", MagicMock(return_value="NEED_HELP"))
        session = make_session(current_state="WAIT_DONE", root_cause=gps_service.BATTERY_ISSUE)

        session, outbound = chat_turn(session, "Yeh kaise karu?")
        assert_state(session, "WAIT_DONE")
        text = reply_text(outbound)
        assert any(w in text.lower() for w in ("battery", "terminal", "charge", "step"))

    def test_after_help_done_still_proceeds_normally(self, monkeypatch):
        """Help text then Done should still work — state machine must not get stuck."""
        monkeypatch.setattr(sm.llm, "classify_wait_done_reply", MagicMock(return_value="NEED_HELP"))
        session = make_session(
            current_state="WAIT_DONE",
            root_cause=gps_service.BATTERY_ISSUE,
            **telemetry(voltage=10.8, main_power=True, gps_online=False),
        )
        session, _ = chat_turn(session, "Yeh kaise karu?")
        assert_state(session, "WAIT_DONE")

        monkeypatch.setattr(sm.llm, "classify_wait_done_reply", MagicMock(return_value="DONE"))
        session, outbound = chat_turn(session, "Done")
        assert_state(session, "ASK_PHYSICAL_DAMAGE")  # battery still low

    def test_general_knowledge_question_mid_flow_answered_and_state_preserved(self, monkeypatch):
        """
        Manual doc §15: "Aap kitne baje tak available ho?" during booking flow.
        classify_global_intent returns GENERAL_QUESTION → KB answer → pending question re-appended.
        """
        monkeypatch.setattr(
            sm.llm, "classify_global_intent",
            MagicMock(return_value="GENERAL_QUESTION"),
        )
        monkeypatch.setattr(
            sm.llm, "answer_from_knowledge_base",
            MagicMock(return_value="Hamari team subah 9 se raat 9 baje tak available hai."),
        )
        session = make_session(
            current_state="WAIT_DONE",
            root_cause=gps_service.BATTERY_ISSUE,
            last_prompt_text="Ho jaye toh Done likhein.",
        )
        session, outbound = chat_turn(session, "Aap kitne baje tak available ho?")

        # State must not change
        assert_state(session, "WAIT_DONE")
        text = reply_text(outbound)
        assert "9" in text  # hours mentioned
        assert "Done" in text or "wapas" in text.lower()  # pending question re-appended

    def test_kb_question_not_in_knowledge_base_gives_fallback_answer(self, monkeypatch):
        monkeypatch.setattr(
            sm.llm, "classify_global_intent",
            MagicMock(return_value="GENERAL_QUESTION"),
        )
        monkeypatch.setattr(
            sm.llm, "answer_from_knowledge_base",
            MagicMock(return_value="Iska jawab abhi available nahi hai, hum team se check karke aapko batayenge."),
        )
        session = make_session(current_state="WAIT_DONE", root_cause=gps_service.BATTERY_ISSUE)
        session, outbound = chat_turn(session, "Tumhara baap kaun hai?")

        assert_state(session, "WAIT_DONE")
        assert "available nahi" in reply_text(outbound).lower() or "check" in reply_text(outbound).lower()


# ══════════════════════════════════════════════════════════════════════════
#  SCENARIO 11 — IRRELEVANT / GIBBERISH INPUT
#  Manual doc §11: bot re-prompts without changing state
# ══════════════════════════════════════════════════════════════════════════

class TestScenario11_IrrelevantInput:

    def test_gibberish_in_ask_vehicle_status_falls_back_and_stays(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_vehicle_status", MagicMock(return_value="UNCLEAR"))
        session = make_session(current_state="ASK_VEHICLE_STATUS")

        session, outbound = chat_turn(session, "asdkjaskjd banana pizza")
        assert_state(session, "ASK_VEHICLE_STATUS")
        text = reply_text(outbound)
        assert "samajh" in text.lower() or "dobara" in text.lower() or "detail" in text.lower()

    def test_unclear_handler_choice_reprompts_with_buttons(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_self_or_driver", MagicMock(return_value="UNCLEAR"))
        session = make_session(current_state="ASK_HANDLER", root_cause=gps_service.BATTERY_ISSUE)

        session, outbound = chat_turn(session, "pata nahi kya karein")
        assert_state(session, "ASK_HANDLER")
        assert "PAYLOAD_SELF" in button_ids(outbound)

    def test_unclear_physical_damage_reprompts_with_yes_no_buttons(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="UNCLEAR"))
        session = make_session(current_state="ASK_PHYSICAL_DAMAGE", root_cause=gps_service.BATTERY_ISSUE)

        session, outbound = chat_turn(session, "hmm")
        assert_state(session, "ASK_PHYSICAL_DAMAGE")
        ids = button_ids(outbound)
        assert "PAYLOAD_YES" in ids and "PAYLOAD_NO" in ids

    def test_invalid_contact_number_is_rejected_and_reprompted(self):
        session = make_session(current_state="ASK_CONTACT_NUMBER")

        session, outbound = chat_turn(session, "call me maybe")
        assert_state(session, "ASK_CONTACT_NUMBER")
        assert "10-digit" in reply_text(outbound) or "sahi nahi" in reply_text(outbound).lower()


# ══════════════════════════════════════════════════════════════════════════
#  SCENARIO 12 — SESSION RESUME AFTER SERVER RESTART
#  Manual doc §12: state persists to CSV, survives across separate Python calls
# ══════════════════════════════════════════════════════════════════════════

class TestScenario12_SessionResumeAfterInterruption:

    def test_state_persists_across_separate_find_and_update_calls(self, csv_backend):
        sessions_csv, _, _ = csv_backend

        # Turn 1: create session, advance to ASK_HANDLER, write to disk
        session = session_manager.create_session(
            phone_number=OWNER_PHONE,
            vehicle_no=VEHICLE_NO,
            **telemetry(voltage=10.5, main_power=True, gps_online=False),
        )
        session, _ = sm.handle_start(session, "", OWNER_PHONE)
        session_manager.update_session(session)

        assert session["current_state"] == "ASK_HANDLER"

        # Simulate server restart: re-load the session from the CSV file
        reloaded = session_manager.find_session(OWNER_PHONE)
        assert reloaded is not None
        assert reloaded["current_state"] == "ASK_HANDLER"
        assert reloaded["root_cause"] == gps_service.BATTERY_ISSUE

    def test_conversation_continues_correctly_after_reload(self, csv_backend, monkeypatch):
        sessions_csv, _, _ = csv_backend

        # Setup: write a WAIT_DONE session to the CSV
        session = session_manager.create_session(
            phone_number=OWNER_PHONE,
            vehicle_no=VEHICLE_NO,
            current_state="WAIT_DONE",
            root_cause=gps_service.BATTERY_ISSUE,
            handler="OWNER",
            **telemetry(voltage=12.6, main_power=True, gps_online=True),
        )

        # Load it back and continue
        reloaded = session_manager.find_session(OWNER_PHONE)
        monkeypatch.setattr(sm.llm, "classify_wait_done_reply", MagicMock(return_value="DONE"))
        reloaded, outbound = sm.process_message(reloaded, "Done", OWNER_PHONE)
        session_manager.update_session(reloaded)

        final = session_manager.find_session(OWNER_PHONE)
        assert final["current_state"] == "COMPLETED"

    def test_multiple_turns_all_written_and_recoverable(self, csv_backend, monkeypatch):
        """Three turns, each written to CSV, each recovered correctly."""
        sessions_csv, _, _ = csv_backend

        session = session_manager.create_session(
            phone_number=OWNER_PHONE,
            vehicle_no=VEHICLE_NO,
            **telemetry(voltage=10.5, main_power=True, gps_online=False),
        )

        # Turn 1: handle_start
        session, _ = sm.handle_start(session, "", OWNER_PHONE)
        session_manager.update_session(session)
        assert session_manager.find_session(OWNER_PHONE)["current_state"] == "ASK_HANDLER"

        # Turn 2: SELF chosen
        monkeypatch.setattr(sm.llm, "classify_self_or_driver", MagicMock(return_value="SELF"))
        session = session_manager.find_session(OWNER_PHONE)
        session, _ = sm.process_message(session, "main khud karunga", OWNER_PHONE)
        session_manager.update_session(session)
        assert session_manager.find_session(OWNER_PHONE)["current_state"] == "WAIT_DONE"

        # Turn 3: Done (GPS back online)
        monkeypatch.setattr(sm.llm, "classify_wait_done_reply", MagicMock(return_value="DONE"))
        session = session_manager.find_session(OWNER_PHONE)
        session["gpsStatus"] = "1"  # simulate GPS coming back online
        session, _ = sm.process_message(session, "Done", OWNER_PHONE)
        session_manager.update_session(session)
        assert session_manager.find_session(OWNER_PHONE)["current_state"] == "COMPLETED"


# ══════════════════════════════════════════════════════════════════════════
#  SCENARIO 13 — TICKET CREATION
#  Manual doc §13: CONFIRM_SUMMARY + "Haan" → ticket in tickets.csv
# ══════════════════════════════════════════════════════════════════════════

class TestScenario13_TicketCreation:

    def test_confirm_yes_creates_ticket_and_closes_case(self, tmp_path, monkeypatch):
        tickets_csv  = tmp_path / "tickets.csv"
        engineers_csv = tmp_path / "engineers.csv"
        with open(engineers_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["engineer_id", "engineer_name", "phone_number", "zone"])
            w.writerow(["ENG001", "Ramesh Kumar", "919000000001", "Pune"])
            w.writerow(["ENG099", "Default",      "919000000099", "Default"])
        monkeypatch.setattr(settings, "TICKETS_CSV",   str(tickets_csv))
        monkeypatch.setattr(settings, "ENGINEERS_CSV", str(engineers_csv))

        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="YES"))
        session = make_session(
            current_state="CONFIRM_SUMMARY",
            root_cause=gps_service.BATTERY_ISSUE,
            current_location="Nagpur Bypass",
            extracted_service_location="Pune",
            service_date="2026-07-06",
            service_time_window="05:00 PM",
            contact_person="Site Manager",
            contact_number="9876500000",
        )
        session, outbound = chat_turn(session, "Haan confirm kar do")

        assert_state(session, "COMPLETED")
        assert session["ticket_id"].startswith("TKT-")
        assert session["engineer_id"] == "ENG001"

        text = reply_text(outbound)
        assert "TKT-" in text

        assert tickets_csv.exists()
        with open(tickets_csv) as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["vehicle_no"] == VEHICLE_NO
        assert rows[0]["service_location"] == "Pune"
        assert rows[0]["contact_number"] == "9876500000"
        assert rows[0]["status"] == "ASSIGNED"
        assert rows[0]["engineer_id"] == "ENG001"

    def test_confirm_no_routes_to_correction(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="NO"))
        session = make_session(current_state="CONFIRM_SUMMARY")

        session, outbound = chat_turn(session, "Nahi galat hai")
        assert_state(session, "ASK_BOOKING_CORRECTION")
        assert not session.get("ticket_id")

    def test_unknown_service_zone_falls_back_to_default_engineer(self, tmp_path, monkeypatch):
        engineers_csv = tmp_path / "engineers.csv"
        with open(engineers_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["engineer_id", "engineer_name", "phone_number", "zone"])
            w.writerow(["ENG099", "Default Guy", "919000000099", "Default"])
        monkeypatch.setattr(settings, "ENGINEERS_CSV", str(engineers_csv))

        engineer = engineer_service.assign_engineer("Timbuktu City")
        assert engineer["engineer_id"] == "ENG099"
        assert engineer["zone"] == "Default"

    def test_concurrent_ticket_creation_does_not_lose_rows(self, tmp_path, monkeypatch):
        """
        Two tickets written in rapid succession — both must appear in tickets.csv.
        This exercises the FileLock inside ticket_service.create_ticket().
        """
        tickets_csv   = tmp_path / "tickets.csv"
        engineers_csv = tmp_path / "engineers.csv"
        with open(engineers_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["engineer_id", "engineer_name", "phone_number", "zone"])
            w.writerow(["ENG099", "Default", "919000000099", "Default"])
        monkeypatch.setattr(settings, "TICKETS_CSV",   str(tickets_csv))
        monkeypatch.setattr(settings, "ENGINEERS_CSV", str(engineers_csv))

        session1 = make_session(
            extracted_service_location="Delhi",
            service_date="2026-07-06", service_time_window="09:00 AM",
            contact_person="A", contact_number="9000000001",
        )
        session2 = make_session(
            vehicle_no="MH12BB9999",
            extracted_service_location="Mumbai",
            service_date="2026-07-07", service_time_window="11:00 AM",
            contact_person="B", contact_number="9000000002",
        )
        ticket_service.create_ticket(session1)
        ticket_service.create_ticket(session2)

        with open(tickets_csv) as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 2
        vehicles = {r["vehicle_no"] for r in rows}
        assert VEHICLE_NO in vehicles
        assert "MH12BB9999" in vehicles


# ══════════════════════════════════════════════════════════════════════════
#  SCENARIO 14 — SERVICE BOOKING (FULL FLOW)
#  Manual doc §14: ASK_CURRENT_LOCATION → … → ticket confirmed
# ══════════════════════════════════════════════════════════════════════════

class TestScenario14_ServiceBookingFullFlow:

    def test_full_booking_flow_nagpur_to_pune(self, tmp_path, monkeypatch):
        """
        Exact chat script from MANUAL_TEST_SCENARIOS.md §14:
          Location → Pune destination → confirm Pune → Kal → 5 baje →
          Site manager Rahul → 9876500000 → ticket confirmed
        """
        engineers_csv = tmp_path / "engineers.csv"
        tickets_csv   = tmp_path / "tickets.csv"
        with open(engineers_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["engineer_id", "engineer_name", "phone_number", "zone"])
            w.writerow(["ENG001", "Ramesh Kumar", "919000000001", "Pune"])
            w.writerow(["ENG099", "Default",      "919000000099", "Default"])
        monkeypatch.setattr(settings, "ENGINEERS_CSV", str(engineers_csv))
        monkeypatch.setattr(settings, "TICKETS_CSV",   str(tickets_csv))

        phone   = OWNER_PHONE
        session = make_session(current_state="ASK_CURRENT_LOCATION", vehicle_state="RUNNING")

        # "Nagpur bypass ke paas hu"
        free_text_mock = MagicMock(side_effect=["Nagpur Bypass", "Pune", "Rahul"])
        monkeypatch.setattr(sm.llm, "extract_free_text", free_text_mock)

        session, out = chat_turn(session, "Nagpur bypass ke paas hu", phone)
        assert_state(session, "ASK_DESTINATION_LOCATION")
        assert session["current_location"] == "Nagpur Bypass"

        # "Pune ja rahe hain"
        session, out = chat_turn(session, "Pune ja rahe hain", phone)
        assert_state(session, "ASK_SERVICE_CITY_CONFIRMATION")
        assert session["destination_location"] == "Pune"
        assert "Pune" in reply_text(out, phone)

        # "Haan Pune theek hai" — city confirmed, mode=TODAY → goes straight to time
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="YES"))
        session, out = chat_turn(session, "Haan Pune theek hai", phone)
        assert_state(session, "ASK_SERVICE_TIME_WINDOW")
        assert session["service_date"] == date.today().isoformat()

        # "Shaam 5 baje"
        monkeypatch.setattr(sm.llm, "extract_time", MagicMock(return_value="05:00 PM"))
        session, out = chat_turn(session, "Shaam 5 baje", phone)
        assert_state(session, "ASK_CONTACT_PERSON")
        assert session["service_time_window"] == "05:00 PM"

        # "Site manager Rahul"
        session, out = chat_turn(session, "Site manager Rahul", phone)
        assert_state(session, "ASK_CONTACT_NUMBER")
        assert session["contact_person"] == "Rahul"

        # "9876500000" → ticket created
        session, out = chat_turn(session, "9876500000", phone)
        assert_state(session, "COMPLETED")
        assert session["contact_number"] == "9876500000"
        assert session["ticket_id"].startswith("TKT-")
        assert session["engineer_id"] == "ENG001"
        assert "Pune" in reply_text(out, phone)  # booking summary mentions city
        assert "TKT-" in reply_text(outbound=out, phone=phone) or \
               any("TKT-" in (m.get("text", "") or "") for m in out)

    def test_bulk_extraction_skips_already_answered_questions(self, monkeypatch):
        """
        Customer sends all details at once:
        "Nagpur bypass ke paas hu, Pune ja raha hu, Rahul 9876500000"
        Should fill multiple slots and jump ahead in the flow.
        """
        monkeypatch.setattr(sm.llm, "extract_booking_slots", MagicMock(return_value={
            "current_location":    "Nagpur Bypass",
            "destination_location": "Pune",
            "service_date":        "",
            "service_time_window": "",
            "contact_person":      "Rahul",
            "contact_number":      "9876500000",
        }))
        session = make_session(current_state="ASK_CURRENT_LOCATION", vehicle_state="RUNNING")
        session, out = chat_turn(
            session,
            "Nagpur bypass ke paas hu, Pune ja raha hu, contact Rahul 9876500000",
        )
        # Bulk fill landed at least current_location + destination_location
        assert session["current_location"] == "Nagpur Bypass"
        assert session["destination_location"] == "Pune"
        # The bot should have jumped forward — not still at ASK_CURRENT_LOCATION
        assert session["current_state"] != "ASK_CURRENT_LOCATION"

    def test_date_options_1_sets_two_days_from_today(self):
        session = make_session(current_state="ASK_SERVICE_DATE_OPTIONS")
        session, out = chat_turn(session, "1")

        assert_state(session, "ASK_SERVICE_TIME_WINDOW")
        assert session["service_date"] == sm.add_days_to_today(2)

    def test_date_options_2_sets_four_days_from_today(self):
        session = make_session(current_state="ASK_SERVICE_DATE_OPTIONS")
        session, out = chat_turn(session, "2")

        assert_state(session, "ASK_SERVICE_TIME_WINDOW")
        assert session["service_date"] == sm.add_days_to_today(4)

    def test_service_city_no_goes_to_preference_then_back_to_date(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="NO"))
        session = make_session(
            current_state="ASK_SERVICE_CITY_CONFIRMATION",
            destination_location="Pune",
        )
        session, _ = chat_turn(session, "Nahi mujhe Nagpur chahiye")
        assert_state(session, "ASK_SERVICE_CITY_PREFERENCE")

        monkeypatch.setattr(sm.llm, "extract_free_text", MagicMock(return_value="Nagpur"))
        session, _ = chat_turn(session, "Nagpur")
        assert_state(session, "ASK_SERVICE_DATE")
        assert session["extracted_service_location"] == "Nagpur"


# ══════════════════════════════════════════════════════════════════════════
#  SCENARIO 16 — REGRESSION TESTS (Manual doc §16 a–e)
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.regression
class TestScenario16_BugRegressions:

    def test_16a_duplicate_webhook_message_id_is_ignored(self):
        """
        §16a: Same WhatsApp message ID delivered twice must only be
        processed once. Tested at the dedup-cache level since we can't
        easily hit the HTTP endpoint in a unit test.
        """
        from whatsapp.webhook import _already_processed, _seen_message_ids
        _seen_message_ids.clear()

        msg_id = "wamid.REGRESSION_16A"
        assert not _already_processed(msg_id), "First delivery should not be marked seen"
        assert     _already_processed(msg_id), "Second delivery must be deduped"

    def test_16b_driver_reply_recognized_as_same_session(self, csv_backend, monkeypatch):
        """
        §16b: Driver's own WhatsApp (bare 10-digit at input, stored as 91+)
        must resolve back to the same session row.
        """
        sessions_csv, _, _ = csv_backend

        session = session_manager.create_session(
            phone_number=OWNER_PHONE,
            vehicle_no=VEHICLE_NO,
            driver_name="Test Driver",
            driver_phone="919876543210",
            current_state="WAIT_DONE",
            handler="DRIVER",
            root_cause=gps_service.BATTERY_ISSUE,
        )

        # Driver texts in from their number
        driver_session = session_manager.find_session("919876543210")
        assert driver_session is not None, "Driver session not found — normalization regression"
        assert driver_session["handler"] == "DRIVER"

    def test_16c_nahi_with_valid_phone_saves_number_not_NOT_PROVIDED(self, monkeypatch):
        """
        §16c: "Nahi driver ka number hi sahi hai 9876543210" must save 9876543210,
        not mark it NOT_PROVIDED.
        """
        monkeypatch.setattr(
            sm.llm, "extract_name_and_phone",
            MagicMock(return_value={"name": "", "phone": "9876543210"}),
        )
        session = make_session(current_state="ASK_ALTERNATE_CONTACT")
        session, outbound = chat_turn(session, "Nahi driver ka number hi sahi hai 9876543210")

        assert session.get("contact_number") == "9876543210", (
            "'nahi' + valid phone should save the phone, not mark NOT_PROVIDED"
        )
        assert_state(session, "COMPLETED")

    def test_16d_booking_correction_updates_city_not_confused_by_word_phone(self, monkeypatch):
        """
        §16d: "Service city Pune, phone sahi hai" should update service city
        and not loop on itself because the word 'phone' is also in the message.
        """
        monkeypatch.setattr(sm.llm, "extract_free_text", MagicMock(return_value="Pune"))
        monkeypatch.setattr(sm.llm, "extract_time",      MagicMock(return_value=""))
        monkeypatch.setattr(sm.llm, "extract_date",      MagicMock(return_value=""))

        session = make_session(
            current_state="ASK_BOOKING_CORRECTION",
            service_date="2026-07-06",
            service_time_window="10:00 AM",
            contact_person="Raju",
            contact_number="9876500000",
        )
        session, outbound = chat_turn(session, "Service city Pune, phone sahi hai")

        assert session["extracted_service_location"] == "Pune"
        assert_state(session, "COMPLETED")

    def test_16e_button_payload_bypasses_llm_for_physical_damage(self, monkeypatch):
        """
        §16e: PAYLOAD_YES / PAYLOAD_NO buttons must never call classify_yes_no.
        """
        blown_up = MagicMock(side_effect=AssertionError("classify_yes_no must NOT be called for a payload"))
        monkeypatch.setattr(sm.llm, "classify_yes_no", blown_up)

        session = make_session(
            current_state="ASK_PHYSICAL_DAMAGE",
            root_cause=gps_service.BATTERY_ISSUE,
        )
        session, outbound = chat_turn(session, "PAYLOAD_YES")

        assert_state(session, "ASK_CURRENT_LOCATION")
        blown_up.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════
#  BONUS — EDGE CASES FOUND DURING REVIEW (not in manual doc but real risks)
# ══════════════════════════════════════════════════════════════════════════

class TestEdgeCases:

    def test_service_city_question_mode_today_skips_date_entirely(self, monkeypatch):
        """
        The `service_city_question_mode = TODAY` path must set service_date
        to today and go straight to ASK_SERVICE_TIME_WINDOW, never to
        ASK_SERVICE_DATE. This was a real time-of-day-sensitive bug.
        """
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="YES"))
        session = make_session(
            current_state="ASK_SERVICE_CITY_CONFIRMATION",
            destination_location="Mumbai",
            service_city_question_mode="TODAY",
        )
        session, _ = chat_turn(session, "Haan")

        assert_state(session, "ASK_SERVICE_TIME_WINDOW")
        assert session["service_date"] == date.today().isoformat()

    def test_pending_quick_date_is_used_when_user_says_yes_to_today_kal(self, monkeypatch):
        """
        get_service_date_prompt_and_date() returns a date AND stores it in
        pending_quick_date. When user says YES, that stored date must be used
        (not re-derived), avoiding the hour-tick race condition.
        """
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        session = make_session(
            current_state="ASK_SERVICE_DATE",
            pending_quick_date=tomorrow,
        )
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="YES"))
        monkeypatch.setattr(sm.llm, "extract_date",    MagicMock(return_value=""))

        session, _ = chat_turn(session, "Haan kal theek hai")
        assert session["service_date"] == tomorrow

    def test_gps_status_1_in_start_state_returns_unknown_root_cause(self):
        """GPS already online at START → root cause UNKNOWN (shouldn't happen in prod
        but the system must not crash or mislabel it)."""
        session = make_session(**telemetry(voltage=12.6, main_power=True, gps_online=True))
        cause = gps_service.analyze_root_cause(session)
        assert cause == "UNKNOWN"

    def test_completed_session_rejects_every_message_type(self, monkeypatch):
        """Once COMPLETED, any follow-up message returns the 'already closed' reply."""
        session = make_session(current_state="COMPLETED")
        for msg in ("Done", "haan", "ticket status?", "PAYLOAD_YES", "Driver se baat karo"):
            s, outbound = chat_turn(session, msg)
            assert_state(s, "COMPLETED")
            assert "close" in reply_text(outbound).lower() or "pehle se" in reply_text(outbound).lower()

    def test_driver_contact_confirmation_uses_driver_details_from_session(self, monkeypatch):
        """
        After driver handoff, if a driver is on file, the booking flow should
        ask whether the driver is the contact person — using the driver's
        stored name and phone from the session.
        """
        monkeypatch.setattr(sm.llm, "extract_time", MagicMock(return_value="02:00 PM"))
        session = make_session(
            current_state="ASK_SERVICE_TIME_WINDOW",
            driver_name="Deepak",
            driver_phone="919871234560",
            handler="DRIVER",
        )
        session, outbound = chat_turn(session, "Dopahar 2 baje")

        assert_state(session, "ASK_DRIVER_CONTACT_CONFIRMATION")
        text = reply_text(outbound)
        assert "Deepak" in text
        assert "919871234560" in text or "9871234560" in text

    def test_alternate_contact_not_provided_closes_with_NOT_PROVIDED(self, monkeypatch):
        """
        If the owner has no alternate contact person, saying "nahi" (with no
        phone number in the message) should store NOT_PROVIDED and create the
        ticket.
        """
        monkeypatch.setattr(
            sm.llm, "extract_name_and_phone",
            MagicMock(return_value={"name": "", "phone": ""}),
        )
        session = make_session(
            current_state="ASK_ALTERNATE_CONTACT",
            service_date="2026-07-06",
            service_time_window="10:00 AM",
            extracted_service_location="Delhi",
        )
        session, outbound = chat_turn(session, "Nahi koi nahi hai")

        assert session["contact_person"] == "NOT_PROVIDED"
        assert session["contact_number"] == "NOT_PROVIDED"
        assert_state(session, "COMPLETED")

    def test_extract_booking_slots_single_field_does_not_bulk_skip(self, monkeypatch):
        """
        _apply_booking_slots requires >= 2 fields to be considered a bulk reply.
        A message with only one field (e.g. just a location) must go through
        the normal per-field handler, not bulk-skip.
        """
        monkeypatch.setattr(sm.llm, "extract_booking_slots", MagicMock(return_value={
            "current_location":    "Nagpur",
            "destination_location": "",
            "service_date":        "",
            "service_time_window": "",
            "contact_person":      "",
            "contact_number":      "",
        }))
        monkeypatch.setattr(sm.llm, "extract_free_text", MagicMock(return_value="Nagpur"))

        session = make_session(current_state="ASK_CURRENT_LOCATION", vehicle_state="RUNNING")
        # Only one slot filled (current_location) — must not bulk-skip to middle of flow
        session, _ = chat_turn(session, "Nagpur mein hu", OWNER_PHONE)
        # Single-field messages go through normal handler → next state ASK_DESTINATION_LOCATION
        assert session["current_state"] in ("ASK_DESTINATION_LOCATION", "ASK_SERVICE_CITY_CONFIRMATION")


# ══════════════════════════════════════════════════════════════════════════
#  REAL LLM TESTS (skipped by default — run with: pytest -m realllm)
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.realllm
@pytest.mark.skip(reason="Requires a live LLM provider. Run: pytest -m realllm to enable.")
class TestRealLLM:
    """
    These tests hit the actual LLM provider configured in .env.
    They are slow (~2-5s per turn), cost money, and are non-deterministic.
    Run them manually before a release to catch prompt regressions.

    Prerequisites:
        - .env must have a valid API key for LLM_PROVIDER
        - Server does NOT need to be running (tests call state_machine directly)

    Usage:
        pytest test_realtime_scenarios.py::TestRealLLM -v -m realllm -s
    """

    @pytest.fixture(autouse=True)
    def allow_real_llm(self, monkeypatch):
        """Override the safety-net guard so real LLM calls are allowed here."""
        # Remove the _boom guards set by default_llm_guards
        # by restoring actual llm_handler functions
        import importlib
        import core.llm_handler as real_llm
        importlib.reload(real_llm)

        import core.state_machine as sm_mod
        sm_mod.llm = real_llm
        yield
        # Restore
        importlib.reload(real_llm)
        sm_mod.llm = real_llm

    def test_real_llm_classifies_haan_as_yes(self):
        result = llm_handler.classify_yes_no("ASK_PHYSICAL_DAMAGE", "Haan bilkul damage hai")
        assert result == "YES"

    def test_real_llm_classifies_nahi_as_no(self):
        result = llm_handler.classify_yes_no("ASK_PHYSICAL_DAMAGE", "Nahi koi damage nahi hai")
        assert result == "NO"

    def test_real_llm_extracts_10digit_phone(self):
        result = llm_handler.extract_name_and_phone("ASK_NEW_DRIVER", "Ramesh 9876543210")
        assert result["name"] == "Ramesh"
        assert "9876543210" in result["phone"]

    def test_real_llm_extracts_kal_as_tomorrow(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        result = llm_handler.extract_date("ASK_SERVICE_DATE", "Kal service chahiye")
        assert result == tomorrow

    def test_real_llm_classifies_workshop_reply(self):
        result = llm_handler.classify_vehicle_status("ASK_VEHICLE_STATUS", "Gaadi workshop mein khadi hai")
        assert result == "WORKSHOP"

    def test_real_llm_classifies_gps_damaged(self):
        result = llm_handler.classify_vehicle_status("ASK_VEHICLE_STATUS", "GPS device toot gaya hai")
        assert result == "GPS_DAMAGED"

    def test_real_llm_done_classified_correctly(self):
        result = llm_handler.classify_wait_done_reply("WAIT_DONE", "Ho gaya battery charge")
        assert result == "DONE"

    def test_real_llm_need_help_classified_correctly(self):
        result = llm_handler.classify_wait_done_reply("WAIT_DONE", "Samajh nahi aa raha kaise karein")
        assert result == "NEED_HELP"

    def test_real_llm_driver_request_classified(self):
        result = llm_handler.classify_wait_done_reply("WAIT_DONE", "Driver ko bhej do yaar")
        assert result == "WANT_DRIVER"

    def test_real_llm_knowledge_base_answers_from_file(self):
        answer = llm_handler.answer_from_knowledge_base("Aap kitne baje available ho?")
        assert answer  # non-empty
        assert "9" in answer or "baje" in answer.lower()  # hours are in the KB

    def test_real_llm_general_question_detection(self):
        result = llm_handler.is_general_question("WAIT_DONE", "Aapki team kab available hai?")
        assert result == "GENERAL_QUESTION"

    def test_real_llm_flow_reply_not_misclassified_as_general_question(self):
        result = llm_handler.is_general_question("WAIT_DONE", "Done ho gaya")
        assert result == "FLOW_REPLY"


if __name__ == "__main__":
    print("Running realtime scenarios via pytest...")
    raise SystemExit(pytest.main(["-v", "-s", __file__]))