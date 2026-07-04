"""
test.py — end-to-end scenario tests for the GPS AI Support Agent.

Covers every item in the test-case checklist:
  Low Battery | Battery charged -> GPS recovered | Battery charged -> still no GPS
  | Main Power disconnected | Vehicle in workshop | Vehicle accident | GPS removed
  | GPS damaged | Driver handover | User asks a question mid-flow
  | User gives irrelevant input | Session resume after interruption
  | Complaint creation | Service booking | LLM context retention
  | Entity extraction | Confirmation detection
  | API verification after every troubleshooting step

Design notes
------------
* The LLM (`core.llm_handler`) is mocked everywhere. These are deterministic
  unit/integration tests for the STATE MACHINE — they must not depend on a
  live Groq/Gemini/Ollama/Anthropic key or the network.
* `gps_service` is NOT mocked. It reads telemetry straight off the session
  dict (this is the "mock vendor API" — see services/gps_service.py), so we
  test it for real by setting main_powervoltage / ismainpoerconnected /
  gpsStatus on the session, exactly like production would.
* CSV-backed services (session_manager, ticket_service, engineer_service)
  are pointed at temp files per test so nothing touches the real data/ dir.

Run with:
    pytest test.py -v
"""

import os
import sys
import csv
import importlib
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import state_machine as sm          # noqa: E402
from core import session_manager               # noqa: E402
from services import gps_service, ticket_service, engineer_service  # noqa: E402
from config import settings                    # noqa: E402


# ============================================================== fixtures ==

SESSION_COLUMNS = session_manager.COLUMNS


def base_session(**overrides) -> dict:
    """A blank session row (mirrors session_manager.COLUMNS) with sane
    defaults, so every test only has to override what it cares about."""
    session = {col: "" for col in SESSION_COLUMNS}
    session.update({
        "phone_number": "919999900001",
        "vehicle_no": "MH12AB1234",
        "last_location": "Pune Bypass",
        "timestamp": "2026-07-01 10:00:00",
        "gpstime": "01 July 2026 10:00",
        "handler": "OWNER",
        "current_state": "START",
    })
    session.update(overrides)
    return session


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """
    Safety net: if any test forgets to mock an llm_handler function, fail
    loudly instead of silently trying to hit a real provider / network.
    """
    def _boom(*a, **k):
        raise AssertionError(
            "A real LLM call was attempted — mock core.state_machine.llm.* "
            "in this test instead of relying on the network."
        )
    for name in (
        "classify_yes_no", "classify_wait_done_reply", "classify_self_or_driver",
        "classify_vehicle_status", "extract_date", "extract_time",
        "extract_free_text", "extract_name_and_phone",
    ):
        monkeypatch.setattr(sm.llm, name, _boom, raising=True)
    yield


@pytest.fixture
def tmp_csv_backend(tmp_path, monkeypatch):
    """Points ticket_service / engineer_service at throwaway CSV files."""
    tickets_csv = tmp_path / "tickets.csv"
    engineers_csv = tmp_path / "engineers.csv"

    with open(engineers_csv, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["engineer_id", "engineer_name", "phone_number", "zone"])
        writer.writerow(["ENG001", "Ramesh Kumar", "919000000001", "Pune"])
        writer.writerow(["ENG002", "Suresh Patil", "919000000002", "Mumbai"])
        writer.writerow(["ENG005", "Rahul Deshmukh", "919000000005", "Default"])

    monkeypatch.setattr(settings, "TICKETS_CSV", str(tickets_csv))
    monkeypatch.setattr(settings, "ENGINEERS_CSV", str(engineers_csv))
    return tickets_csv, engineers_csv


@pytest.fixture
def tmp_sessions_backend(tmp_path, monkeypatch):
    """Points session_manager at a throwaway sessions CSV (module-level
    constants, so patch them directly rather than via `settings`)."""
    sessions_csv = tmp_path / "mock_sessions.csv"
    with open(sessions_csv, "w", newline="") as f:
        csv.writer(f).writerow(SESSION_COLUMNS)
    monkeypatch.setattr(session_manager, "CSV_PATH", str(sessions_csv))
    monkeypatch.setattr(session_manager, "LOCK_PATH", str(sessions_csv) + ".lock")
    return sessions_csv


def telemetry(voltage=12.6, main_power_connected=True, gps_online=False):
    return {
        "main_powervoltage": str(voltage),
        "ismainpoerconnected": "1" if main_power_connected else "0",
        "gpsStatus": "1" if gps_online else "0",
    }


# ===================================================== 1. LOW BATTERY =====

class TestLowBattery:
    def test_low_battery_triggers_battery_alert(self):
        session = base_session(**telemetry(voltage=10.8, main_power_connected=True, gps_online=False))
        session, outbound = sm.handle_start(session, "", "919999900001")

        assert session["root_cause"] == gps_service.BATTERY_ISSUE
        assert session["current_state"] == "ASK_HANDLER"
        assert "battery" in outbound[0]["interactive"]["body"]["text"].lower()
        button_ids = [b["reply"]["id"] for b in outbound[0]["interactive"]["action"]["buttons"]]
        assert button_ids == ["PAYLOAD_SELF", "PAYLOAD_DRIVER"]

    def test_battery_voltage_exactly_at_threshold_is_not_low(self):
        # boundary check on BATTERY_VOLTAGE_THRESHOLD (11.5)
        session = base_session(**telemetry(voltage=11.5, main_power_connected=True, gps_online=False))
        cause = gps_service.analyze_root_cause(session)
        assert cause == "UNKNOWN"


# ============================== 2 & 3. BATTERY CHARGED (recovered / not) ==

class TestBatteryChargedOutcomes:
    def test_battery_charged_gps_recovered(self, monkeypatch):
        session = base_session(
            current_state="WAIT_DONE", root_cause=gps_service.BATTERY_ISSUE,
            **telemetry(voltage=12.6, main_power_connected=True, gps_online=True),
        )
        session, outbound = sm.handle_wait_done(session, "Done", "919999900001")

        assert session["current_state"] == "COMPLETED"
        assert "online" in outbound[0]["text"].lower() or "wapas" in outbound[0]["text"].lower()

    def test_battery_charged_still_no_gps_moves_to_vehicle_status(self):
        # voltage now healthy (issue itself resolved) but gpsStatus still 0
        session = base_session(
            current_state="WAIT_DONE", root_cause=gps_service.BATTERY_ISSUE,
            **telemetry(voltage=12.6, main_power_connected=True, gps_online=False),
        )
        session, outbound = sm.handle_wait_done(session, "Done", "919999900001")

        assert session["current_state"] == "ASK_VEHICLE_STATUS"

    def test_battery_still_low_after_done_asks_about_physical_damage(self):
        # neither the GPS nor the underlying battery issue is fixed
        session = base_session(
            current_state="WAIT_DONE", root_cause=gps_service.BATTERY_ISSUE,
            **telemetry(voltage=10.9, main_power_connected=True, gps_online=False),
        )
        session, outbound = sm.handle_wait_done(session, "Done", "919999900001")

        assert session["current_state"] == "ASK_PHYSICAL_DAMAGE"


# ========================================== 4. MAIN POWER DISCONNECTED ====

class TestMainPowerDisconnected:
    def test_main_power_disconnected_triggers_alert(self):
        session = base_session(**telemetry(voltage=12.6, main_power_connected=False, gps_online=False))
        session, outbound = sm.handle_start(session, "", "919999900001")

        assert session["root_cause"] == gps_service.MAIN_POWER_DISCONNECTED
        assert session["current_state"] == "ASK_HANDLER"
        assert "power" in outbound[0]["interactive"]["body"]["text"].lower()

    def test_main_power_reconnected_but_gps_still_offline(self):
        session = base_session(
            current_state="WAIT_DONE", root_cause=gps_service.MAIN_POWER_DISCONNECTED,
            **telemetry(voltage=12.6, main_power_connected=True, gps_online=False),
        )
        session, outbound = sm.handle_wait_done(session, "Done", "919999900001")
        assert session["current_state"] == "ASK_VEHICLE_STATUS"


# ============ 5, 6, 7, 8. VEHICLE STATUS: workshop / accident / gps issues =

class TestVehicleStatusScenarios:
    @pytest.mark.parametrize("llm_value,expected_state,expected_template_key", [
        ("WORKSHOP", "ASK_EXPECTED_DATE", "ASK_EXPECTED_DATE_WORKSHOP"),
        ("ACCIDENT", "ASK_EXPECTED_DATE", "ASK_EXPECTED_DATE_ACCIDENT"),
        ("GPS_REMOVED", "ASK_CURRENT_LOCATION", "ASK_CURRENT_LOCATION"),
        ("GPS_DAMAGED", "ASK_CURRENT_LOCATION", "ASK_CURRENT_LOCATION"),
        ("RUNNING", "ASK_CURRENT_LOCATION", "ASK_CURRENT_LOCATION"),
    ])
    def test_vehicle_status_routes_correctly(self, monkeypatch, llm_value, expected_state, expected_template_key):
        monkeypatch.setattr(sm.llm, "classify_vehicle_status", MagicMock(return_value=llm_value))
        session = base_session(current_state="ASK_VEHICLE_STATUS")

        session, outbound = sm.handle_ask_vehicle_status(session, "some free text reply", "919999900001")

        assert session["vehicle_state"] == llm_value
        assert session["current_state"] == expected_state
        from prompts.templates import render
        assert outbound[0]["text"] == render(expected_template_key) if expected_template_key != "ASK_CURRENT_LOCATION" or True else True

    def test_vehicle_status_unclear_falls_back_and_stays_in_state(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_vehicle_status", MagicMock(return_value="UNCLEAR"))
        session = base_session(current_state="ASK_VEHICLE_STATUS")

        session, outbound = sm.handle_ask_vehicle_status(session, "asdf gibberish", "919999900001")

        assert session["current_state"] == "ASK_VEHICLE_STATUS"
        assert "samajh" in outbound[0]["text"].lower()  # FALLBACK template


# ================================================== 9. DRIVER HANDOVER ====

class TestDriverHandover:
    def test_ask_handler_driver_payload_with_saved_driver_shows_confirm(self):
        session = base_session(
            current_state="ASK_HANDLER", root_cause=gps_service.BATTERY_ISSUE,
            driver_name="Deepak Singh", driver_phone="9871234560",
        )
        session, outbound = sm.handle_ask_handler(session, "PAYLOAD_DRIVER", "919999900001")

        assert session["current_state"] == "DRIVER_CONFIRM"
        assert "Deepak Singh" in outbound[0]["interactive"]["body"]["text"]

    def test_ask_handler_driver_payload_no_saved_driver_asks_for_details(self):
        session = base_session(current_state="ASK_HANDLER", root_cause=gps_service.BATTERY_ISSUE)
        session, outbound = sm.handle_ask_handler(session, "PAYLOAD_DRIVER", "919999900001")

        assert session["current_state"] == "ASK_NEW_DRIVER"

    def test_driver_confirm_yes_transfers_handler_and_messages_both_numbers(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="YES"))
        session = base_session(
            current_state="DRIVER_CONFIRM", root_cause=gps_service.BATTERY_ISSUE,
            driver_name="Deepak Singh", driver_phone="9871234560",
        )
        session, outbound = sm.handle_driver_confirm(session, "haan", "919999900001")

        assert session["handler"] == "DRIVER"
        assert session["current_state"] == "WAIT_DONE"
        assert len(outbound) == 2
        recipients = {m["phone"] for m in outbound}
        assert recipients == {"919999900001", "9871234560"}

    def test_new_driver_entity_extraction_transfers(self, monkeypatch):
        monkeypatch.setattr(
            sm.llm, "extract_name_and_phone",
            MagicMock(return_value={"name": "Ramesh", "phone": "9876543210"}),
        )
        session = base_session(current_state="ASK_NEW_DRIVER", root_cause=gps_service.MAIN_POWER_DISCONNECTED)

        session, outbound = sm.handle_ask_new_driver(session, "Ramesh 9876543210", "919999900001")

        assert session["driver_name"] == "Ramesh"
        assert session["driver_phone"] == "9876543210"
        assert session["handler"] == "DRIVER"
        assert session["current_state"] == "WAIT_DONE"
        assert len(outbound) == 2

    def test_mid_flow_driver_request_intercepted_by_dispatcher(self, monkeypatch):
        """
        Owner is mid-troubleshooting (WAIT_DONE) and suddenly types
        'driver se baat karo' — process_message must reroute to the driver
        handoff BEFORE handle_wait_done ever sees the message.
        """
        monkeypatch.setattr(sm.llm, "classify_wait_done_reply", MagicMock(
            side_effect=AssertionError("handle_wait_done should not have run")
        ))
        session = base_session(
            current_state="WAIT_DONE", root_cause=gps_service.BATTERY_ISSUE, handler="OWNER",
        )
        session, outbound = sm.process_message(session, "driver se baat karo", "919999900001")

        assert session["current_state"] == "ASK_NEW_DRIVER"  # no saved driver on this session


# ============================ 10. USER ASKS A QUESTION MID-FLOW ===========

class TestMidFlowQuestion:
    def test_need_help_returns_steps_without_changing_state(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_wait_done_reply", MagicMock(return_value="NEED_HELP"))
        session = base_session(current_state="WAIT_DONE", root_cause=gps_service.BATTERY_ISSUE)

        session, outbound = sm.handle_wait_done(session, "yeh kaise karu?", "919999900001")

        assert session["current_state"] == "WAIT_DONE"  # still waiting, just got help text
        assert "battery" in outbound[0]["text"].lower() or "terminal" in outbound[0]["text"].lower()

    def test_need_help_for_main_power_gives_power_specific_steps(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_wait_done_reply", MagicMock(return_value="NEED_HELP"))
        session = base_session(current_state="WAIT_DONE", root_cause=gps_service.MAIN_POWER_DISCONNECTED)

        session, outbound = sm.handle_wait_done(session, "wiring kaha hai?", "919999900001")

        from prompts.templates import render
        assert outbound[0]["text"] == render("MAIN_POWER_HELP_STEPS")


# ============================ 11. USER GIVES IRRELEVANT INPUT =============

class TestIrrelevantInput:
    def test_physical_damage_unclear_reprompts_with_buttons(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="UNCLEAR"))
        session = base_session(current_state="ASK_PHYSICAL_DAMAGE", root_cause=gps_service.BATTERY_ISSUE)

        session, outbound = sm.handle_ask_physical_damage(session, "banana", "919999900001")

        assert session["current_state"] == "ASK_PHYSICAL_DAMAGE"
        assert "interactive" in outbound[0]

    def test_ask_handler_gibberish_falls_back_to_self_or_driver_prompt(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_self_or_driver", MagicMock(return_value="UNCLEAR"))
        session = base_session(current_state="ASK_HANDLER", root_cause=gps_service.BATTERY_ISSUE)

        session, outbound = sm.handle_ask_handler(session, "xyz123 random text", "919999900001")

        assert "SELF ya DRIVER" in outbound[0]["interactive"]["body"]["text"]


# ===================================== 12. SESSION RESUME AFTER INTERRUPT =

class TestSessionResumeAfterInterruption:
    def test_session_persists_state_across_separate_requests(self, tmp_sessions_backend, monkeypatch):
        # --- turn 1: outage detected, agent creates the session and sends
        # the battery alert, then the "connection" drops (nothing else
        # happens for a while) ---
        session = session_manager.create_session(
            phone_number="919999911111", vehicle_no="MH12ZZ9999",
            **telemetry(voltage=10.5, main_power_connected=True, gps_online=False),
        )
        session, _ = sm.handle_start(session, "", "919999911111")
        session_manager.update_session(session)

        assert session["current_state"] == "ASK_HANDLER"

        # --- process restarts / new webhook hit later: reload purely from
        # disk, simulating an interrupted session being resumed ---
        reloaded = session_manager.find_session("919999911111")
        assert reloaded is not None
        assert reloaded["current_state"] == "ASK_HANDLER"
        assert reloaded["root_cause"] == gps_service.BATTERY_ISSUE

        # --- turn 2 continues the flow from where it left off ---
        monkeypatch.setattr(sm.llm, "classify_self_or_driver", MagicMock(return_value="SELF"))
        reloaded, outbound = sm.process_message(reloaded, "main khud kar lunga", "919999911111")
        session_manager.update_session(reloaded)

        final = session_manager.find_session("919999911111")
        assert final["current_state"] == "WAIT_DONE"
        assert final["handler"] == "OWNER"

    def test_driver_can_resume_conversation_after_handoff(self, tmp_sessions_backend, monkeypatch):
        session = session_manager.create_session(
            phone_number="919999922222", vehicle_no="MH12YY8888",
            driver_name="Deepak", driver_phone="919888800000",
            current_state="DRIVER_CONFIRM", root_cause=gps_service.MAIN_POWER_DISCONNECTED,
        )
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="YES"))
        session, _ = sm.handle_driver_confirm(session, "yes", "919999922222")
        session_manager.update_session(session)

        # driver's own number should now resolve the same session row
        found = session_manager.find_session("919888800000")
        assert found is not None
        assert found["current_state"] == "WAIT_DONE"
        assert found["handler"] == "DRIVER"


# =================================== 13. COMPLAINT (TICKET) CREATION ======

class TestComplaintTicketCreation:
    def test_confirm_summary_yes_creates_ticket(self, tmp_csv_backend, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="YES"))
        session = base_session(
            current_state="CONFIRM_SUMMARY",
            root_cause=gps_service.BATTERY_ISSUE,
            current_location="Pune Bypass",
            extracted_service_location="Pune",
            service_date="2026-07-05",
            service_time_window="05:00 PM",
            contact_person="Raju",
            contact_number="9876500000",
        )
        session, outbound = sm.handle_confirm_summary(session, "haan confirm kar do", "919999900001")

        assert session["current_state"] == "COMPLETED"
        assert session["ticket_id"].startswith("TKT-")
        assert session["engineer_id"] == "ENG001"  # zone "Pune" match
        assert "TKT-" in outbound[0]["text"]

        tickets_csv, _ = tmp_csv_backend
        assert os.path.exists(tickets_csv)
        with open(tickets_csv) as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["vehicle_no"] == "MH12AB1234"
        assert rows[0]["status"] == "ASSIGNED"

    def test_confirm_summary_no_routes_to_correction_not_ticket(self, tmp_csv_backend, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="NO"))
        session = base_session(current_state="CONFIRM_SUMMARY")

        session, outbound = sm.handle_confirm_summary(session, "nahi galat hai", "919999900001")

        assert session["current_state"] == "ASK_BOOKING_CORRECTION"
        assert "ticket_id" not in session or not session.get("ticket_id")

    def test_ticket_falls_back_to_default_engineer_for_unknown_zone(self, tmp_csv_backend):
        engineer = engineer_service.assign_engineer("Some Unmapped Town")
        assert engineer["engineer_id"] == "ENG005"
        assert engineer["zone"] == "Default"


# ============================================== 14. SERVICE BOOKING (E2E) =

class TestServiceBookingEndToEnd:
    def test_full_booking_flow_produces_ticket(self, tmp_csv_backend, monkeypatch):
        phone = "919999900001"
        session = base_session(current_state="ASK_CURRENT_LOCATION", root_cause=gps_service.BATTERY_ISSUE)

        monkeypatch.setattr(sm.llm, "extract_free_text", MagicMock(side_effect=[
            "Mumbai Highway",   # current location
            "Pune",             # destination
            "Raju",             # contact person
        ]))
        session, outbound = sm.handle_ask_current_location(session, "Mumbai Highway ke pass", phone)
        assert session["current_state"] == "ASK_DESTINATION_LOCATION"
        assert session["current_location"] == "Mumbai Highway"

        session, outbound = sm.handle_ask_destination_location(session, "Pune ja rahe hain", phone)
        assert session["current_state"] == "ASK_SERVICE_CITY_CONFIRMATION"
        assert session["destination_location"] == "Pune"

        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="YES"))
        session, outbound = sm.handle_ask_service_city_confirmation(session, "haan Pune theek hai", phone)
        assert session["current_state"] == "ASK_SERVICE_DATE"
        assert session["extracted_service_location"] == "Pune"

        monkeypatch.setattr(sm.llm, "extract_date", MagicMock(return_value="2026-07-05"))
        session, outbound = sm.handle_ask_service_date(session, "5 July", phone)
        assert session["current_state"] == "ASK_SERVICE_TIME_WINDOW"
        assert session["service_date"] == "2026-07-05"

        monkeypatch.setattr(sm.llm, "extract_time", MagicMock(return_value="05:00 PM"))
        session, outbound = sm.handle_ask_service_time_window(session, "5 baje", phone)
        assert session["current_state"] == "ASK_CONTACT_PERSON"  # no driver on file
        assert session["service_time_window"] == "05:00 PM"

        session, outbound = sm.handle_ask_contact_person(session, "Raju hai site pe", phone)
        assert session["current_state"] == "ASK_CONTACT_NUMBER"
        assert session["contact_person"] == "Raju"

        session, outbound = sm.handle_ask_contact_number(session, "9876500000", phone)
        assert session["current_state"] == "CONFIRM_SUMMARY"
        assert session["contact_number"] == "9876500000"
        assert "Pune" in outbound[0]["text"]  # booking summary shows service city

        session, outbound = sm.handle_confirm_summary(session, "haan sahi hai", phone)
        assert session["current_state"] == "COMPLETED"
        assert session["ticket_id"].startswith("TKT-")
        assert session["engineer_id"] == "ENG001"  # Pune zone

    def test_invalid_contact_number_is_rejected(self):
        session = base_session(current_state="ASK_CONTACT_NUMBER")
        session, outbound = sm.handle_ask_contact_number(session, "not a number", "919999900001")

        assert session["current_state"] == "ASK_CONTACT_NUMBER"
        assert "sahi nahi" in outbound[0]["text"].lower()

    def test_service_date_options_shortcut_2_days(self):
        session = base_session(current_state="ASK_SERVICE_DATE_OPTIONS")
        session, outbound = sm.handle_ask_service_date_options(session, "1", "919999900001")

        assert session["current_state"] == "ASK_SERVICE_TIME_WINDOW"
        assert session["service_date"] == sm.add_days_to_today(2)


# =============================== 15. LLM CONTEXT RETENTION =================

class TestLLMContextRetention:
    def test_llm_is_invoked_with_the_current_state_at_each_turn(self, monkeypatch):
        yes_no_mock = MagicMock(return_value="YES")
        monkeypatch.setattr(sm.llm, "classify_yes_no", yes_no_mock)

        session = base_session(current_state="ASK_SERVICE_CITY_CONFIRMATION", destination_location="Nagpur")
        sm.handle_ask_service_city_confirmation(session, "haan", "919999900001")
        assert yes_no_mock.call_args[0][0] == "ASK_SERVICE_CITY_CONFIRMATION"

        session["current_state"] = "CONFIRM_SUMMARY"
        sm.handle_confirm_summary(session, "haan", "919999900001")
        assert yes_no_mock.call_args[0][0] == "CONFIRM_SUMMARY"

    def test_entities_extracted_earlier_are_still_available_later(self, monkeypatch):
        """
        Context retention isn't just about the LLM call args — extracted
        session fields from turn 1 must still be readable (and rendered
        into the summary) several turns later.
        """
        monkeypatch.setattr(sm.llm, "extract_free_text", MagicMock(return_value="Nagpur"))
        session = base_session(current_state="ASK_CURRENT_LOCATION")
        session, _ = sm.handle_ask_current_location(session, "Nagpur mein hu", "919999900001")

        # ... several unrelated turns later, current_location must persist
        session["current_state"] = "ASK_CONTACT_NUMBER"
        session["extracted_service_location"] = "Nagpur"
        session["service_date"] = "2026-07-06"
        session["service_time_window"] = "10:00 AM"
        session["contact_person"] = "Site Guard"

        session, outbound = sm.handle_ask_contact_number(session, "9123456789", "919999900001")
        assert "Nagpur" in outbound[0]["text"]  # from turn 1, still in the summary


# =================================== 16. ENTITY EXTRACTION ================

class TestEntityExtraction:
    def test_extract_name_and_phone_valid(self, monkeypatch):
        monkeypatch.setattr(
            sm.llm, "extract_name_and_phone",
            MagicMock(return_value={"name": "Suresh", "phone": "9123456789"}),
        )
        session = base_session(current_state="ASK_NEW_DRIVER")
        session, outbound = sm.handle_ask_new_driver(session, "Suresh 9123456789", "919999900001")

        assert session["driver_name"] == "Suresh"
        assert session["driver_phone"] == "9123456789"

    def test_extract_name_and_phone_missing_phone_reprompts(self, monkeypatch):
        monkeypatch.setattr(
            sm.llm, "extract_name_and_phone",
            MagicMock(return_value={"name": "Suresh", "phone": ""}),
        )
        session = base_session(current_state="ASK_NEW_DRIVER")
        session, outbound = sm.handle_ask_new_driver(session, "Suresh hai bas", "919999900001")

        assert session["current_state"] == "ASK_NEW_DRIVER"
        assert "10-digit" in outbound[0]["text"]

    def test_phone_regex_recovers_number_even_if_llm_missed_it(self, monkeypatch):
        # extract_name_and_phone returns nothing useful, but the raw
        # message itself contains a valid number -> PHONE_RE should catch it
        monkeypatch.setattr(
            sm.llm, "extract_name_and_phone",
            MagicMock(return_value={"name": "Suresh", "phone": ""}),
        )
        session = base_session(current_state="ASK_NEW_DRIVER")
        session, outbound = sm.handle_ask_new_driver(session, "Suresh, call on 9123456789 please", "919999900001")

        assert session["driver_phone"] == "9123456789"
        assert session["current_state"] == "WAIT_DONE"

    def test_extract_date_used_for_workshop_expected_date(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "extract_date", MagicMock(return_value="2026-07-10"))
        session = base_session(current_state="ASK_EXPECTED_DATE")
        session, outbound = sm.handle_ask_expected_date(session, "10 July tak", "919999900001")

        assert session["extracted_appointment_date"] == "2026-07-10"
        assert session["current_state"] == "COMPLETED"

    def test_extract_date_empty_reprompts(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "extract_date", MagicMock(return_value=""))
        session = base_session(current_state="ASK_EXPECTED_DATE")
        session, outbound = sm.handle_ask_expected_date(session, "pata nahi", "919999900001")

        assert session["current_state"] == "ASK_EXPECTED_DATE"


# =============================== 17. CONFIRMATION DETECTION ================

class TestConfirmationDetection:
    @pytest.mark.parametrize("reply,mocked_value,expected_state", [
        ("haan bilkul", "YES", "ASK_CURRENT_LOCATION"),
        ("nahi thik nahi hai", "NO", "WAIT_DONE"),
    ])
    def test_physical_damage_yes_no(self, monkeypatch, reply, mocked_value, expected_state):
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value=mocked_value))
        session = base_session(current_state="ASK_PHYSICAL_DAMAGE", root_cause=gps_service.BATTERY_ISSUE)

        session, outbound = sm.handle_ask_physical_damage(session, reply, "919999900001")
        assert session["current_state"] == expected_state

    def test_button_payload_bypasses_llm_entirely(self, monkeypatch):
        # PAYLOAD_YES / PAYLOAD_NO from WhatsApp buttons must short-circuit
        # the LLM classification path completely (handle_driver_confirm
        # checks the payload before ever touching the LLM).
        blown_up = MagicMock(side_effect=AssertionError("llm should not be called for a button payload"))
        monkeypatch.setattr(sm.llm, "classify_yes_no", blown_up)
        session = base_session(
            current_state="DRIVER_CONFIRM", root_cause=gps_service.BATTERY_ISSUE,
            driver_name="Deepak", driver_phone="9871234560",
        )

        session, outbound = sm.handle_driver_confirm(session, "PAYLOAD_YES", "919999900001")
        assert session["current_state"] == "WAIT_DONE"
        assert session["handler"] == "DRIVER"


# ==================== 18. API VERIFICATION AFTER EVERY TROUBLESHOOTING STEP

class TestAPIVerificationEveryStep:
    """
    Every time the user says 'Done', the agent must re-check real telemetry
    (never just trust the user's word). These tests hit gps_service
    directly with the different telemetry combinations it must handle.
    """

    def test_verify_gps_true_when_gps_online(self):
        session = base_session(**telemetry(gps_online=True))
        assert gps_service.verify_gps(session) is True

    def test_verify_gps_false_when_gps_offline(self):
        session = base_session(**telemetry(gps_online=False))
        assert gps_service.verify_gps(session) is False

    @pytest.mark.parametrize("voltage,expected", [(10.0, False), (11.5, True), (12.6, True)])
    def test_battery_issue_resolution_check(self, voltage, expected):
        session = base_session(**telemetry(voltage=voltage))
        assert gps_service.is_power_issue_resolved(session, gps_service.BATTERY_ISSUE) is expected

    @pytest.mark.parametrize("connected,expected", [(True, True), (False, False)])
    def test_main_power_issue_resolution_check(self, connected, expected):
        session = base_session(**telemetry(main_power_connected=connected))
        assert gps_service.is_power_issue_resolved(session, gps_service.MAIN_POWER_DISCONNECTED) is expected

    def test_every_done_reply_re_reads_telemetry_not_cached_state(self, monkeypatch):
        """
        Regression guard: handle_wait_done must call the real-time checks
        every single time, not just once. We simulate two consecutive
        'Done' messages where telemetry changes in between.
        """
        session = base_session(
            current_state="WAIT_DONE", root_cause=gps_service.BATTERY_ISSUE,
            **telemetry(voltage=10.5, main_power_connected=True, gps_online=False),
        )
        session, outbound = sm.handle_wait_done(session, "Done", "919999900001")
        assert session["current_state"] == "ASK_PHYSICAL_DAMAGE"  # still broken

        # user says NO to physical damage, agent asks them to try again
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="NO"))
        session, outbound = sm.handle_ask_physical_damage(session, "nahi", "919999900001")
        assert session["current_state"] == "WAIT_DONE"

        # telemetry now improves in the "real world" before the 2nd "Done"
        session["main_powervoltage"] = "12.6"
        session, outbound = sm.handle_wait_done(session, "Done", "919999900001")
        assert session["current_state"] == "ASK_VEHICLE_STATUS"  # freshly re-checked


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))