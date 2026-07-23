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
from datetime import date
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import state_machine as sm          # noqa: E402
from core import session_manager               # noqa: E402
from core import llm_handler                   # noqa: E402
from services import gps_service, ticket_service, engineer_service  # noqa: E402
from config import settings                    # noqa: E402

_REAL_ANSWER_FROM_KB = llm_handler.answer_from_knowledge_base
_REAL_CLASSIFY_GLOBAL_INTENT = llm_handler.classify_global_intent
_REAL_ANSWER_OFF_TOPIC_REMARK = llm_handler.answer_off_topic_remark


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
        "extract_free_text", "extract_name_and_phone", "answer_from_knowledge_base",
        "extract_booking_slots", "extract_tech_dispatch_slots", "answer_off_topic_remark",
    ):
        monkeypatch.setattr(sm.llm, name, _boom, raising=True)

    # classify_global_intent runs on EVERY process_message() call regardless
    # of state (it replaced is_general_question/classify_ticket_inquiry/
    # is_driver_update_intent as one consolidated check), so default it to
    # "not a special case" rather than making every existing test mock it —
    # tests exercising the driver-update/ticket-inquiry/general-question
    # paths override this per-test.
    monkeypatch.setattr(sm.llm, "classify_global_intent", MagicMock(return_value="FLOW_REPLY"), raising=True)
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
        # An accidental vehicle is handled through other channels
        # (insurance/garage) — same as WORKSHOP, this bot only tracks when
        # it'll be running again, never routes into the service-booking
        # flow (current/destination location, contact person, etc.).
        ("ACCIDENT", "ASK_EXPECTED_DATE", "ASK_EXPECTED_DATE_ACCIDENT"),
        ("GPS_REMOVED", "ASK_DESTINATION_LOCATION", "ASK_DESTINATION_LOCATION"),
        ("GPS_DAMAGED", "ASK_GPS_REPAIR_CONFIRMATION", "ASK_GPS_REPAIR_CONFIRMATION"),
        ("RUNNING", "ASK_DESTINATION_LOCATION", "ASK_DESTINATION_LOCATION"),
    ])
    def test_vehicle_status_routes_correctly(self, monkeypatch, llm_value, expected_state, expected_template_key):
        monkeypatch.setattr(sm.llm, "classify_vehicle_status", MagicMock(return_value=llm_value))
        monkeypatch.setattr(sm.llm, "extract_date", MagicMock(return_value=""))  # no date given in this message
        monkeypatch.setattr(sm.llm, "extract_booking_slots", MagicMock(return_value={}))  # empty extraction for bulk
        session = base_session(current_state="ASK_VEHICLE_STATUS")

        session, outbound = sm.handle_ask_vehicle_status(session, "some free text reply", "919999900001")

        assert session["vehicle_state"] == llm_value
        assert session["current_state"] == expected_state
        from prompts.templates import render
        # Handle both text and button messages
        msg = outbound[0]
        expected_text = render(expected_template_key)
        if "text" in msg:  # text message
            assert msg["text"] == expected_text
        elif "interactive" in msg:  # button message
            assert expected_text in msg["interactive"]["body"]["text"]

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
        assert session["driver_phone"] == "919876543210"  # normalized: 91 + bare 10-digit number
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
        session = base_session(current_state="ASK_CURRENT_LOCATION", vehicle_state="RUNNING")

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
        assert session["current_state"] == "ASK_SERVICE_TIME_WINDOW"  # Skips date question because service_city_question_mode is "TODAY"
        assert session["extracted_service_location"] == "Pune"
        assert session["service_date"] == date.today().isoformat()

        monkeypatch.setattr(sm.llm, "extract_time", MagicMock(return_value="05:00 PM"))
        session, outbound = sm.handle_ask_service_time_window(session, "5 baje", phone)
        assert session["current_state"] == "ASK_CONTACT_PERSON"  # no driver on file
        assert session["service_time_window"] == "05:00 PM"

        session, outbound = sm.handle_ask_contact_person(session, "Raju hai site pe", phone)
        assert session["current_state"] == "ASK_CONTACT_NUMBER"
        assert session["contact_person"] == "Raju"

        session, outbound = sm.handle_ask_contact_number(session, "9876500000", phone)
        assert session["current_state"] == "COMPLETED"
        assert session["contact_number"] == "9876500000"
        assert len(outbound) == 2  # summary + ticket confirmation
        assert "Pune" in outbound[0]["text"]  # booking summary shows service city
        assert session["ticket_id"].startswith("TKT-")
        assert session["engineer_id"] == "ENG001"  # Pune zone

    def test_destination_today_booking_prompt_goes_directly_to_time_window(self, monkeypatch):
        phone = "919999900001"
        session = base_session(current_state="ASK_DESTINATION_LOCATION", current_location="Nagpur")

        monkeypatch.setattr(sm.llm, "extract_free_text", MagicMock(return_value="Preet vihar"))
        session, outbound = sm.handle_ask_destination_location(session, "Preet vihar", phone)

        assert session["current_state"] == "ASK_SERVICE_CITY_CONFIRMATION"
        assert "aaj ke liye service book kar dein" in outbound[0]["text"].lower()

        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="YES"))
        session, outbound = sm.handle_ask_service_city_confirmation(session, "haan", phone)

        assert session["current_state"] == "ASK_SERVICE_TIME_WINDOW"
        assert session["service_date"] == date.today().isoformat()

    def test_broken_vehicle_asks_gps_problem_type(self, monkeypatch):
        session = base_session(current_state="ASK_VEHICLE_STATUS")
        monkeypatch.setattr(sm.llm, "classify_vehicle_status", MagicMock(return_value="GPS_DAMAGED"))

        session, outbound = sm.handle_ask_vehicle_status(session, "GPS toot gaya hai", "919999900001")

        assert session["current_state"] == "ASK_GPS_REPAIR_CONFIRMATION"
        assert "Kya GPS repair ya replace karwana hai?" in outbound[0]["interactive"]["body"]["text"]
        assert outbound[0]["interactive"]["type"] == "button"

    def test_generic_vehicle_issue_asks_gps_or_vehicle_clarification(self):
        session = base_session(current_state="ASK_VEHICLE_STATUS")

        session, outbound = sm.handle_ask_vehicle_status(session, "gadi khrab h", "919999900001")

        assert session["current_state"] == "ASK_VEHICLE_STATUS"
        assert "Kya GPS kharab hai ya vehicle ka problem hai?" in outbound[0]["interactive"]["body"]["text"]
        assert outbound[0]["interactive"]["type"] == "button"

    def test_gps_problem_type_yes_sets_gps_damaged(self, monkeypatch):
        session = base_session(current_state="ASK_GPS_REPAIR_CONFIRMATION")
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="YES"))

        session, outbound = sm.handle_ask_gps_repair_confirmation(session, "haan", "919999900001")

        assert session["current_state"] == "ASK_CURRENT_LOCATION"
        assert "Vehicle abhi kis location par hai?" in outbound[0]["text"]

    def test_gps_damaged_full_flow_skips_redundant_date_question(self, monkeypatch):
        """End-to-end GPS damage flow: confirm GPS repair -> location -> confirm location -> skip date -> ask time"""
        phone = "919999900001"
        
        # Step 1: Vehicle status shows GPS_DAMAGED
        session = base_session(current_state="ASK_VEHICLE_STATUS")
        monkeypatch.setattr(sm.llm, "classify_vehicle_status", MagicMock(return_value="GPS_DAMAGED"))
        session, outbound = sm.handle_ask_vehicle_status(session, "GPS toot gaya hai", phone)
        assert session["current_state"] == "ASK_GPS_REPAIR_CONFIRMATION"
        assert session["vehicle_state"] == "GPS_DAMAGED"
        
        # Step 2: Confirm GPS repair
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="YES"))
        session, outbound = sm.handle_ask_gps_repair_confirmation(session, "Haan", phone)
        assert session["current_state"] == "ASK_CURRENT_LOCATION"
        assert session["vehicle_state"] == "GPS_DAMAGED"  # Preserved
        
        # Step 3: Provide location
        monkeypatch.setattr(sm.llm, "extract_free_text", MagicMock(return_value="Delhi"))
        session, outbound = sm.handle_ask_current_location(session, "delhi", phone)
        assert session["current_state"] == "ASK_SERVICE_CITY_CONFIRMATION"
        assert session["current_location"] == "Delhi"
        assert session["destination_location"] == "Delhi"
        assert session.get("service_city_question_mode") == "TODAY"
        assert "Delhi" in outbound[0]["text"]
        assert "aaj" in outbound[0]["text"]
        
        # Step 4: Confirm location - MUST SKIP DATE QUESTION and go directly to time
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="YES"))
        session, outbound = sm.handle_ask_service_city_confirmation(session, "haa", phone)
        assert session["current_state"] == "ASK_SERVICE_TIME_WINDOW", f"Expected ASK_SERVICE_TIME_WINDOW but got {session['current_state']}"
        assert "Kis time" in outbound[0]["text"] or "time" in outbound[0]["text"].lower()
        assert "aaj" not in outbound[0]["text"]  # Should NOT ask for date confirmation again

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
        assert session["driver_phone"] == "919123456789"  # normalized: 91 + bare 10-digit number

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

        assert session["driver_phone"] == "919123456789"  # normalized: 91 + bare 10-digit number
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


# =================================== 19. KNOWLEDGE BASE (GENERAL QUESTIONS) =

class TestKnowledgeBase:
    def test_general_question_answered_without_changing_state(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_global_intent", MagicMock(return_value="GENERAL_QUESTION"))
        monkeypatch.setattr(
            sm.llm, "answer_from_knowledge_base",
            MagicMock(return_value="Hamari support team subah 9 se raat 9 baje tak available hai."),
        )
        session = base_session(
            current_state="ASK_SERVICE_DATE",
            root_cause=gps_service.BATTERY_ISSUE,
            last_prompt_text="Service kab schedule karni hai?",
        )

        session, outbound = sm.process_message(session, "aap kitne baje tak available ho?", "919999900001")

        assert session["current_state"] == "ASK_SERVICE_DATE"  # untouched
        assert "9 se raat 9" in outbound[0]["text"]
        assert "Service kab schedule karni hai?" in outbound[0]["text"]  # pending question replayed

    def test_flow_reply_is_not_treated_as_general_question(self, monkeypatch):
        classify_mock = MagicMock(return_value="YES")
        monkeypatch.setattr(sm.llm, "classify_yes_no", classify_mock)
        global_intent_mock = MagicMock(return_value="FLOW_REPLY")
        monkeypatch.setattr(sm.llm, "classify_global_intent", global_intent_mock)

        session = base_session(current_state="ASK_PHYSICAL_DAMAGE", root_cause=gps_service.BATTERY_ISSUE)
        session, outbound = sm.process_message(session, "haan sahi hai", "919999900001")

        global_intent_mock.assert_called_once()
        assert session["current_state"] == "ASK_CURRENT_LOCATION"

    def test_button_payloads_skip_general_question_check_entirely(self, monkeypatch):
        global_intent_mock = MagicMock(side_effect=AssertionError("should not classify a raw button payload"))
        monkeypatch.setattr(sm.llm, "classify_global_intent", global_intent_mock)

        session = base_session(current_state="ASK_HANDLER", root_cause=gps_service.BATTERY_ISSUE, driver_name="Deepak", driver_phone="9871234560")
        session, outbound = sm.process_message(session, "PAYLOAD_DRIVER", "919999900001")

        assert session["current_state"] == "DRIVER_CONFIRM"

    def test_last_prompt_text_is_recorded_after_every_normal_turn(self, monkeypatch):
        session = base_session(current_state="ASK_CURRENT_LOCATION", root_cause=gps_service.BATTERY_ISSUE)
        monkeypatch.setattr(sm.llm, "extract_free_text", MagicMock(return_value="Nagpur"))

        session, outbound = sm.process_message(session, "Nagpur mein hu", "919999900001")

        assert session["last_prompt_text"] == outbound[0]["text"]

    def test_answer_from_knowledge_base_uses_real_file_and_stays_grounded(self, monkeypatch):
        """
        Exercises the real (non-mocked) answer_from_knowledge_base against
        the actual data/knowledge_base.md shipped with the project — but
        stubs the underlying LLM call itself so this test doesn't need a
        live API key or network access.
        """
        monkeypatch.setattr(sm.llm, "answer_from_knowledge_base", _REAL_ANSWER_FROM_KB)

        fake_llm_response = '{"value": "Hamari support team subah 9 baje se raat 9 baje tak available hai."}'
        monkeypatch.setattr(llm_handler, "_call_llm", lambda *_a, **_k: fake_llm_response)

        answer = sm.llm.answer_from_knowledge_base("aap kitne baje available ho?")
        assert "9 baje" in answer

    def test_missing_knowledge_base_file_falls_back_gracefully(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "answer_from_knowledge_base", _REAL_ANSWER_FROM_KB)
        monkeypatch.setattr(settings, "KNOWLEDGE_BASE_PATH", "/tmp/does_not_exist_kb.md")
        monkeypatch.setattr(llm_handler, "_kb_cache", {"text": None})

        answer = sm.llm.answer_from_knowledge_base("kuch bhi poochna hai")
        assert "available nahi hai" in answer


# ==================== 19b. TICKET-STATUS INQUIRY (via classify_global_intent) =

class TestTicketInquiryRouting:
    def test_explicit_ticket_id_is_looked_up_without_any_llm_call(self, monkeypatch):
        """An explicit TKT-XXXXXXXX in the message is a cheap regex parse —
        it must never even reach classify_global_intent."""
        intent_mock = MagicMock(side_effect=AssertionError("should not classify an explicit ticket ID"))
        monkeypatch.setattr(sm.llm, "classify_global_intent", intent_mock)
        monkeypatch.setattr(
            sm.ticket_service, "get_ticket_by_id",
            MagicMock(return_value={
                "ticket_id": "TKT-448C059E", "status": "ASSIGNED", "vehicle_no": "MH16EF9012",
                "service_location": "Punjabi Bagh", "service_date": "2026-07-18",
                "service_time": "11:00 AM", "engineer_name": "Rahul Deshmukh",
                "engineer_phone": "919000000005",
            }),
        )
        session = base_session(current_state="ASK_VEHICLE_STATUS")

        session, outbound = sm.process_message(session, "TKT-448C059E", "919999900001")

        assert "TKT-448C059E" in outbound[0]["text"]
        assert "Punjabi Bagh" in outbound[0]["text"]

    def test_ticket_inquiry_intent_uses_session_own_ticket_id(self, monkeypatch):
        """'Kya meri koi complaint register hai' (no ID given) falls back
        to whatever ticket is already on this session."""
        monkeypatch.setattr(sm.llm, "classify_global_intent", MagicMock(return_value="TICKET_INQUIRY"))
        monkeypatch.setattr(
            sm.ticket_service, "get_ticket_by_id",
            MagicMock(return_value={
                "ticket_id": "TKT-999AAAAA", "status": "ASSIGNED", "vehicle_no": "MH16EF9012",
                "service_location": "Nagpur", "service_date": "2026-07-21",
                "service_time": "05:00 PM", "engineer_name": "Test Engineer",
                "engineer_phone": "919000000001",
            }),
        )
        session = base_session(current_state="COMPLETED", ticket_id="TKT-999AAAAA")

        session, outbound = sm.process_message(session, "kya meri koi complaint register hai", "919999900001")

        assert "TKT-999AAAAA" in outbound[0]["text"]
        assert "ASSIGNED" in outbound[0]["text"]

    def test_ticket_inquiry_intent_with_no_ticket_at_all_replies_gracefully(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_global_intent", MagicMock(return_value="TICKET_INQUIRY"))
        session = base_session(current_state="ASK_VEHICLE_STATUS")

        session, outbound = sm.process_message(session, "kya meri koi complaint register hai", "919999900001")

        assert "nahi mila" in outbound[0]["text"].lower()

    def test_classify_global_intent_parses_llm_response(self, monkeypatch):
        """Unit-level check of the real function (not the autouse mock) —
        stubs the raw LLM call so this doesn't need network access."""
        monkeypatch.setattr(sm.llm, "classify_global_intent", _REAL_CLASSIFY_GLOBAL_INTENT, raising=True)
        monkeypatch.setattr(llm_handler, "_call_llm", lambda *_a, **_k: '{"value": "TICKET_INQUIRY"}')
        result = sm.llm.classify_global_intent("COMPLETED", "kya meri koi complaint register hai")
        assert result == "TICKET_INQUIRY"

    def test_classify_global_intent_defaults_to_flow_reply_on_llm_failure(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_global_intent", _REAL_CLASSIFY_GLOBAL_INTENT, raising=True)

        def _boom(*_a, **_k):
            raise RuntimeError("network down")
        monkeypatch.setattr(llm_handler, "_call_llm", _boom)
        result = sm.llm.classify_global_intent("ASK_VEHICLE_STATUS", "anything")
        assert result == "FLOW_REPLY"


class TestDirectTechDispatchViaGlobalIntent:
    def test_phrasing_the_regex_misses_still_dispatches_via_llm_intent(self, monkeypatch):
        """'send your person' isn't caught by the cheap _is_direct_tech_request
        regex (only 'send tech/technician/engineer' or Hinglish subject+verb
        match) — it has to fall through to classify_global_intent."""
        assert not sm._is_direct_tech_request("Hmm gps is not working send your person in Punjab Bagh")

        monkeypatch.setattr(sm.llm, "classify_global_intent", MagicMock(return_value="DIRECT_TECH_DISPATCH"))
        monkeypatch.setattr(
            sm.llm, "extract_tech_dispatch_slots",
            lambda *_a, **_k: {
                "service_location": "Punjab Bagh", "service_date": "",
                "service_time_window": "", "contact_person": "", "contact_number": "",
            },
        )
        monkeypatch.setattr(
            sm.ticket_service, "create_ticket",
            MagicMock(return_value={
                "ticket_id": "TKT-DISPATCH1", "engineer_id": "ENG-1",
                "engineer_name": "Engineer One", "engineer_phone": "919900000000",
                "existing_ticket": False,
            }),
        )
        session = base_session(current_state="ASK_VEHICLE_STATUS")

        session, outbound = sm.process_message(
            session, "Hmm gps is not working send your person in Punjab Bagh", "919999900001",
        )

        assert session["current_state"] == "COMPLETED"
        assert session["extracted_service_location"] == "Punjab Bagh"
        assert any("TKT-DISPATCH1" in (out.get("text") or "") for out in outbound)

    def test_plain_status_update_is_not_misrouted_as_dispatch(self, monkeypatch):
        """A bare GPS-damage status report with no request to send anyone
        must stay FLOW_REPLY, not get swept into dispatch."""
        monkeypatch.setattr(sm.llm, "classify_global_intent", MagicMock(return_value="FLOW_REPLY"))
        monkeypatch.setattr(sm.llm, "classify_vehicle_status", MagicMock(return_value="GPS_DAMAGED"))
        session = base_session(current_state="ASK_VEHICLE_STATUS")

        session, outbound = sm.process_message(session, "gps kharab hai", "919999900001")

        assert session["current_state"] == "ASK_GPS_REPAIR_CONFIRMATION"


class TestRunningStatusNeverSkipsCityConfirmation:
    """Regression coverage for a real transcript bug: when a RUNNING/
    GPS_REMOVED status message already names a destination, the flow used
    to jump straight to ASK_SERVICE_DATE, skipping the one step
    (ASK_SERVICE_CITY_CONFIRMATION) that actually sets
    extracted_service_location — so a location correction given alongside
    a later date/time answer (e.g. "nahi parso delhi me") was silently
    dropped, and the eventual ticket/summary showed a blank service
    location. City confirmation must never be skipped just because a
    destination is already known."""

    def test_destination_known_upfront_routes_to_city_confirmation_not_date(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_vehicle_status", MagicMock(return_value="RUNNING"))
        monkeypatch.setattr(sm.llm, "extract_booking_slots", MagicMock(return_value={
            "current_location": "", "destination_location": "", "service_date": "",
            "service_time_window": "", "contact_person": "", "contact_number": "",
        }))
        session = base_session(current_state="ASK_VEHICLE_STATUS", destination_location="Pune")

        session, outbound = sm.handle_ask_vehicle_status(session, "pune jaa rahi hai", "919999900001")

        assert session["current_state"] == "ASK_SERVICE_CITY_CONFIRMATION"
        assert "Pune" in outbound[0]["text"]

    def test_current_location_defaults_from_telemetry_when_never_asked(self, monkeypatch):
        """This flow never has a dedicated 'where is the vehicle right now'
        question for RUNNING vehicles — current_location should default to
        the telemetry last_location instead of staying blank on the ticket."""
        monkeypatch.setattr(sm.llm, "classify_vehicle_status", MagicMock(return_value="RUNNING"))
        monkeypatch.setattr(sm.llm, "extract_booking_slots", MagicMock(return_value={
            "current_location": "", "destination_location": "Pune", "service_date": "",
            "service_time_window": "", "contact_person": "", "contact_number": "",
        }))
        session = base_session(current_state="ASK_VEHICLE_STATUS", last_location="Nagpur")

        session, outbound = sm.handle_ask_vehicle_status(session, "pune jaa rahi hai", "919999900001")

        assert session["current_location"] == "Nagpur"

    def test_city_confirmation_yes_sets_extracted_service_location(self, monkeypatch):
        """End-to-end: once city confirmation actually runs, the field that
        was previously left blank gets set."""
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="YES"))
        session = base_session(
            current_state="ASK_SERVICE_CITY_CONFIRMATION",
            destination_location="Pune", service_city_confirmed="",
        )

        session, outbound = sm.handle_ask_service_city_confirmation(session, "haan Pune sahi hai", "919999900001")

        assert session["extracted_service_location"] == "Pune"


class TestUnclearYesNoRetryAcknowledgesInsteadOfSilentlyRepeating:
    """Regression coverage for a real transcript bug: an ambiguous reply
    ('hmm') to a yes/no confirmation used to make the bot silently re-send
    the exact same block verbatim, with zero acknowledgment — reading as a
    stuck/duplicated message. Every classify_yes_no-based confirmation
    handler's unclear fallback must now prefix a short clarifying line."""

    def test_driver_contact_confirmation_hmm_gets_clarifying_prefix(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="UNCLEAR"))
        session = base_session(
            current_state="ASK_DRIVER_CONTACT_CONFIRMATION",
            driver_name="Sarvesh Swami", driver_phone="918290323758",
        )

        session, outbound = sm.handle_driver_contact_confirmation(session, "hmm", "919999900001")

        body_text = outbound[0]["interactive"]["body"]["text"]
        assert body_text.startswith("Samajh nahi paaya")
        assert "Sarvesh Swami" in body_text

    def test_confirm_summary_unclear_shows_summary_instead_of_bare_fallback(self, monkeypatch):
        """This one used to be a total dead-end: render("FALLBACK") alone,
        with no booking summary at all — worse than the others."""
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="UNCLEAR"))
        session = base_session(current_state="CONFIRM_SUMMARY", extracted_service_location="Pune")

        session, outbound = sm.handle_confirm_summary(session, "hmm", "919999900001")

        assert outbound[0]["text"].startswith("Samajh nahi paaya")
        assert "Pune" in outbound[0]["text"]
        assert "samajh nahi paaya." != outbound[0]["text"]


class TestImplausibleExtractionAsksAgainInsteadOfAcceptingGarbage:
    """Regression coverage: extract_free_text used to have no way to say
    'nothing plausible here' — callers fell back to `value or message`,
    so a location/name question answered with unrelated gibberish stored
    that gibberish as the field, silently, instead of asking again."""

    def test_gibberish_current_location_reply_is_rejected_and_reasked(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "extract_free_text", MagicMock(return_value=""))
        session = base_session(current_state="ASK_CURRENT_LOCATION")

        session, outbound = sm.handle_ask_current_location(session, "asdkjaskjd banana pizza", "919999900001")

        assert session.get("current_location", "") == ""
        assert outbound[0]["text"].startswith("Samajh nahi paaya")
        assert session["current_state"] == "ASK_CURRENT_LOCATION"

    def test_gibberish_destination_reply_is_rejected_and_reasked(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "extract_free_text", MagicMock(return_value=""))
        session = base_session(current_state="ASK_DESTINATION_LOCATION")

        session, outbound = sm.handle_ask_destination_location(session, "banana pizza", "919999900001")

        assert session.get("destination_location", "") == ""
        assert outbound[0]["text"].startswith("Samajh nahi paaya")
        assert session["current_state"] == "ASK_DESTINATION_LOCATION"

    def test_gibberish_service_city_preference_is_rejected_and_reasked(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "extract_free_text", MagicMock(return_value=""))
        session = base_session(current_state="ASK_SERVICE_CITY_PREFERENCE")

        session, outbound = sm.handle_ask_service_city_preference(session, "banana pizza", "919999900001")

        assert session.get("extracted_service_location", "") == ""
        assert outbound[0]["text"].startswith("Samajh nahi paaya")
        assert session["current_state"] == "ASK_SERVICE_CITY_PREFERENCE"

    def test_gibberish_contact_person_reply_is_rejected_and_reasked(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "extract_free_text", MagicMock(return_value=""))
        session = base_session(current_state="ASK_CONTACT_PERSON")

        session, outbound = sm.handle_ask_contact_person(session, "banana pizza", "919999900001")

        assert session.get("contact_person", "") == ""
        assert outbound[0]["text"].startswith("Samajh nahi paaya")
        assert session["current_state"] == "ASK_CONTACT_PERSON"

    def test_valid_city_reply_still_works_normally(self, monkeypatch):
        """Sanity check the fix didn't break the ordinary path."""
        monkeypatch.setattr(sm.llm, "extract_free_text", MagicMock(return_value="Pune"))
        session = base_session(current_state="ASK_DESTINATION_LOCATION")

        session, outbound = sm.handle_ask_destination_location(session, "Pune jaa rahi hai", "919999900001")

        assert session["destination_location"] == "Pune"
        assert session["current_state"] == "ASK_SERVICE_CITY_CONFIRMATION"


class TestDriverHandoffCapturesPhoneGivenInline:
    """Regression coverage for a real transcript bug: 'Veh on the way h ap
    driver se bt kr lo\n8130995093' triggers the generic driver-handoff
    interrupt via '_is_driver_request', but the handoff only ever showed
    the EXISTING driver on file and silently discarded the new number
    given in the same breath — forcing the user to retype it after
    separately rejecting the stale driver via DRIVER_CONFIRM's NO."""

    def test_handoff_message_with_inline_number_transfers_directly(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "extract_name_and_phone", MagicMock(return_value={"name": "", "phone": ""}))
        session = base_session(
            current_state="WAIT_DONE", root_cause="BATTERY",
            driver_name="Sarvesh Swami", driver_phone="918290323758",
        )

        session, outbound = sm._start_driver_handoff(
            session, "Veh on the way h ap driver se bt kr lo\n8130995093", "919999900001",
        )

        # No DRIVER_CONFIRM detour — the given number is used immediately.
        assert session["current_state"] == "WAIT_DONE"
        assert session["handler"] == "DRIVER"
        assert session["driver_phone"] == "918130995093"
        # No name was given inline — the existing name on file is kept
        # rather than being discarded.
        assert session["driver_name"] == "Sarvesh Swami"

    def test_handoff_message_with_inline_name_and_number_uses_both(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "extract_name_and_phone", MagicMock(return_value={"name": "Ramesh", "phone": "8130995093"}))
        session = base_session(current_state="WAIT_DONE", root_cause="BATTERY")

        session, outbound = sm._start_driver_handoff(
            session, "naya driver Ramesh hai, 8130995093 pe baat kar lo", "919999900001",
        )

        assert session["driver_name"] == "Ramesh"
        assert session["driver_phone"] == "918130995093"
        assert session["handler"] == "DRIVER"

    def test_handoff_message_without_a_number_still_shows_confirm_as_before(self, monkeypatch):
        """Sanity check the fix didn't break the ordinary path — no number
        in the message means DRIVER_CONFIRM still runs as before."""
        session = base_session(
            current_state="WAIT_DONE", root_cause="BATTERY",
            driver_name="Sarvesh Swami", driver_phone="918290323758",
        )

        session, outbound = sm._start_driver_handoff(session, "driver se baat kar lo", "919999900001")

        assert session["current_state"] == "DRIVER_CONFIRM"

    def test_ask_new_driver_phone_only_reply_stages_it_and_asks_only_for_name(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "extract_name_and_phone", MagicMock(return_value={"name": "", "phone": "8130995093"}))
        session = base_session(current_state="ASK_NEW_DRIVER", driver_name="Sarvesh Swami", driver_phone="918290323758")

        session, outbound = sm.handle_ask_new_driver(session, "8130995093", "919999900001")

        assert session["driver_phone"] == "918130995093"
        assert session["driver_name"] == ""
        assert "naam" in outbound[0]["text"].lower()
        assert session["handler"] != "DRIVER"

    def test_ask_new_driver_name_only_reply_pairs_with_staged_phone_not_asked_again(self, monkeypatch):
        """The follow-up turn to the previous test: only a name is given
        this time — the phone from the prior turn must be reused, not
        asked for a second time."""
        monkeypatch.setattr(sm.llm, "extract_name_and_phone", MagicMock(return_value={"name": "Ramesh", "phone": ""}))
        session = base_session(current_state="ASK_NEW_DRIVER", driver_name="", driver_phone="918130995093")

        session, outbound = sm.handle_ask_new_driver(session, "Ramesh", "919999900001")

        assert session["driver_name"] == "Ramesh"
        assert session["driver_phone"] == "918130995093"
        assert session["handler"] == "DRIVER"

    def test_ask_new_driver_name_only_does_not_reuse_a_stale_unrelated_phone(self, monkeypatch):
        """If driver_name is still non-empty (the ordinary 'rejected an
        existing driver, now giving a new one' path), a name-only reply
        must NOT silently pair with whatever old phone happens to be on
        file — that phone was never confirmed as belonging to this name."""
        monkeypatch.setattr(sm.llm, "extract_name_and_phone", MagicMock(return_value={"name": "Ramesh", "phone": ""}))
        session = base_session(current_state="ASK_NEW_DRIVER", driver_name="Sarvesh Swami", driver_phone="918290323758")

        session, outbound = sm.handle_ask_new_driver(session, "Ramesh", "919999900001")

        assert session["handler"] != "DRIVER"
        assert "number" in outbound[0]["text"].lower() or "mobile" in outbound[0]["text"].lower()


class TestDriverHandoffAndChangeViaGlobalIntent:
    """Regression coverage for a real transcript bug: 'driver sa baat kar
    lo' (a common Hinglish spelling variant of 'se') was not recognized by
    the hardcoded _is_driver_request regex, and — unlike ticket-inquiry,
    direct-tech-dispatch, and general-question — driver-handoff had never
    been migrated onto classify_global_intent as a fallback. The message
    fell all the way through to the vehicle-status classifier, which
    misclassified it as DEFER_UNKNOWN and silently closed the case."""

    def test_misspelled_handoff_phrase_still_routes_via_llm_intent(self, monkeypatch):
        assert not sm._is_driver_request("driver sa baat kar lo")

        monkeypatch.setattr(sm.llm, "classify_global_intent", MagicMock(return_value="DRIVER_HANDOFF"))
        session = base_session(
            current_state="ASK_VEHICLE_STATUS", root_cause="BATTERY",
            driver_name="Sarvesh Swami", driver_phone="918290323758",
        )

        session, outbound = sm.process_message(session, "driver sa baat kar lo", "919999900001")

        assert session["current_state"] == "DRIVER_CONFIRM"

    def test_driver_handoff_intent_respects_the_same_guard_as_the_regex(self, monkeypatch):
        """Once a driver has already taken over (handler=DRIVER), a stray
        DRIVER_HANDOFF classification must not re-trigger the handoff flow
        — same guard the regex-based check already enforced."""
        monkeypatch.setattr(sm.llm, "classify_global_intent", MagicMock(return_value="DRIVER_HANDOFF"))
        monkeypatch.setattr(sm.llm, "classify_wait_done_reply", MagicMock(return_value="UNCLEAR"))
        session = base_session(current_state="WAIT_DONE", handler="DRIVER", root_cause="BATTERY")

        session, outbound = sm.process_message(session, "driver sa baat kar lo", "918290323758")

        assert session["current_state"] != "DRIVER_CONFIRM"

    def test_driver_change_intent_routes_via_llm_when_regex_misses_it(self, monkeypatch):
        assert not sm._is_driver_change_request("ye driver sahi nahi hai, koi aur chahiye")

        monkeypatch.setattr(sm.llm, "classify_global_intent", MagicMock(return_value="DRIVER_CHANGE"))
        session = base_session(current_state="WAIT_DONE", root_cause="BATTERY")

        session, outbound = sm.process_message(session, "ye driver sahi nahi hai, koi aur chahiye", "919999900001")

        assert session["current_state"] == "ASK_NEW_DRIVER"


class TestAccidentStatusNeverEntersServiceBookingFlow:
    """Regression coverage for a real transcript bug: an ACCIDENT status
    used to jump straight into ASK_CURRENT_LOCATION — the start of the
    full service-booking flow (destination, service city, contact person,
    ticket creation) — even though an accidental vehicle is handled
    through other channels (insurance/garage), not this bot dispatching
    someone. It must behave exactly like WORKSHOP: ask only for an
    expected running-again date, then close."""

    def test_accident_status_asks_expected_date_not_current_location(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_vehicle_status", MagicMock(return_value="ACCIDENT"))
        monkeypatch.setattr(sm.llm, "extract_date", MagicMock(return_value=""))
        session = base_session(current_state="ASK_VEHICLE_STATUS")

        session, outbound = sm.handle_ask_vehicle_status(session, "vehicle ka accident ho gya?", "919999900001")

        assert session["current_state"] == "ASK_EXPECTED_DATE"
        assert session["current_state"] != "ASK_CURRENT_LOCATION"

    def test_accident_status_with_date_already_given_closes_immediately(self, monkeypatch):
        """Same fast-path WORKSHOP already had — if a date is right there
        in the message, skip the follow-up question entirely."""
        monkeypatch.setattr(sm.llm, "classify_vehicle_status", MagicMock(return_value="ACCIDENT"))
        monkeypatch.setattr(sm.llm, "extract_date", MagicMock(return_value="2026-07-25"))
        session = base_session(current_state="ASK_VEHICLE_STATUS")

        session, outbound = sm.handle_ask_vehicle_status(session, "accident ho gaya, 25 tak aa jayegi", "919999900001")

        assert session["current_state"] == "COMPLETED"
        assert session["extracted_appointment_date"] == "2026-07-25"


class TestOffTopicRemarkGeneratesGroundedReply:
    """New capability: a message that's neither a flow reply nor a real
    question (venting, small talk, an ambiguous aside) gets a short LLM-
    generated acknowledgment instead of the generic 'didn't understand'
    fallback — grounded the same way answer_from_knowledge_base is, and
    always resuming whatever was pending."""

    def test_off_topic_remark_routes_to_grounded_reply_and_resumes_pending_question(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_global_intent", MagicMock(return_value="OFF_TOPIC_REMARK"))
        monkeypatch.setattr(
            sm.llm, "answer_off_topic_remark",
            MagicMock(return_value="Samajh sakta hoon, GPS issues frustrating ho sakte hain."),
        )
        session = base_session(
            current_state="ASK_VEHICLE_STATUS",
            last_prompt_text="Namaste!\n\nVehicle ki current status batayein.",
        )

        session, outbound = sm.process_message(session, "yaar bahut pareshaan kar diya iss GPS ne", "919999900001")

        assert "Samajh sakta hoon" in outbound[0]["text"]
        assert "Vehicle ki current status batayein." in outbound[0]["text"]
        assert session["current_state"] == "ASK_VEHICLE_STATUS"

    def test_off_topic_remark_llm_failure_falls_back_gracefully(self, monkeypatch):
        """Unit-level check of the real function (not the autouse mock) —
        stubs the raw LLM call so this doesn't need network access."""
        monkeypatch.setattr(sm.llm, "answer_off_topic_remark", _REAL_ANSWER_OFF_TOPIC_REMARK, raising=True)

        def _boom(*_a, **_k):
            raise RuntimeError("network down")
        monkeypatch.setattr(llm_handler, "_call_llm", _boom)

        result = sm.llm.answer_off_topic_remark("ye kya bakwas hai")

        assert result == llm_handler._OFF_TOPIC_ACK_FALLBACK

    def test_off_topic_remark_parses_real_function_response(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "answer_off_topic_remark", _REAL_ANSWER_OFF_TOPIC_REMARK, raising=True)
        monkeypatch.setattr(llm_handler, "_call_llm", lambda *_a, **_k: '{"value": "Thik hai, samajh gaya."}')

        result = sm.llm.answer_off_topic_remark("random chit chat", "some context")

        assert result == "Thik hai, samajh gaya."


# ============================ 20. BUG-FIX REGRESSION TESTS ================
# One test per bug found in the audit — each of these would have FAILED
# against the pre-fix code.

class TestBugFixRegressions:
    def test_physical_damage_calls_llm_exactly_once_for_text_replies(self, monkeypatch):
        """Bug: classify_yes_no was called twice (once unconditionally,
        once again inside the else-branch) — doubling cost/latency."""
        mock = MagicMock(return_value="YES")
        monkeypatch.setattr(sm.llm, "classify_yes_no", mock)
        session = base_session(current_state="ASK_PHYSICAL_DAMAGE", root_cause=gps_service.BATTERY_ISSUE)

        sm.handle_ask_physical_damage(session, "haan hai", "919999900001")

        assert mock.call_count == 1

    def test_physical_damage_button_payload_never_touches_llm(self, monkeypatch):
        """Bug: a button click still triggered one wasted LLM call before
        being overwritten by the payload check."""
        mock = MagicMock(side_effect=AssertionError("button payload should never reach the LLM"))
        monkeypatch.setattr(sm.llm, "classify_yes_no", mock)
        session = base_session(current_state="ASK_PHYSICAL_DAMAGE", root_cause=gps_service.BATTERY_ISSUE)

        session, outbound = sm.handle_ask_physical_damage(session, "PAYLOAD_YES", "919999900001")
        assert session["current_state"] == "ASK_CURRENT_LOCATION"

    def test_alternate_contact_keeps_valid_number_even_with_nahi_in_message(self, monkeypatch):
        """Bug: missing parens meant ANY message containing 'nahi' got
        treated as 'no contact provided', discarding a valid phone number
        that was also present in the same message."""
        monkeypatch.setattr(
            sm.llm, "extract_name_and_phone",
            MagicMock(return_value={"name": "Site guard", "phone": "9876543210"}),
        )
        session = base_session(current_state="ASK_ALTERNATE_CONTACT")

        session, outbound = sm.handle_ask_alternate_contact(
            session, "Nahi driver ka number hi sahi hai 9876543210", "919999900001"
        )

        assert session["contact_number"] == "9876543210"
        assert session["contact_number"] != "NOT_PROVIDED"

    def test_alternate_contact_still_handles_genuine_not_provided(self):
        # make sure fixing the bug above didn't break the legitimate case
        session = base_session(current_state="ASK_ALTERNATE_CONTACT")
        session, outbound = sm.handle_ask_alternate_contact(session, "Nahi, koi number nahi hai", "919999900001")
        assert session["contact_number"] == "NOT_PROVIDED"

    def test_booking_correction_does_not_double_extract_once_already_updated(self, monkeypatch):
        """Bug: missing parens meant the contact-person branch could fire
        even when an earlier field in the same message had already set
        updated=True, wasting a call and risking an incorrect overwrite."""
        city_mock = MagicMock(return_value="Pune")
        contact_mock = MagicMock(side_effect=AssertionError("should not run — service city already matched"))
        monkeypatch.setattr(sm.llm, "extract_free_text", lambda state, msg, kind: (
            city_mock(state, msg, kind) if kind == "preferred service city" else contact_mock(state, msg, kind)
        ))
        session = base_session(current_state="ASK_BOOKING_CORRECTION")

        # this message contains "city" (matches the city branch first) AND
        # "phone" (used to wrongly re-trigger the contact-person branch too)
        session, outbound = sm.handle_ask_booking_correction(
            session, "Service city Pune, phone sahi hai", "919999900001"
        )

        assert session["extracted_service_location"] == "Pune"
        assert session["current_state"] == "COMPLETED"

    def test_service_date_yes_uses_the_date_actually_shown_to_customer(self, monkeypatch):
        """Bug: the 'yes' branch re-derived aaj-vs-kal from datetime.now()
        a second time — if the hour ticked past the 19:00 cutoff between
        question and reply, 'yes' could get booked for the wrong day."""
        monkeypatch.setattr(sm.llm, "extract_date", MagicMock(return_value=""))
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="YES"))
        session = base_session(current_state="ASK_SERVICE_DATE", pending_quick_date="2026-08-15")

        session, outbound = sm.handle_ask_service_date(session, "haan", "919999900001")

        assert session["service_date"] == "2026-08-15"

    def test_driver_phone_normalized_to_match_metas_incoming_format(self, monkeypatch):
        """Bug: a driver's number saved as a bare 10-digit string would
        never match Meta's full-country-code 'from' field on their next
        incoming message — find_session() would report 'no active case'."""
        monkeypatch.setattr(
            sm.llm, "extract_name_and_phone",
            MagicMock(return_value={"name": "Ramesh", "phone": "9876543210"}),
        )
        session = base_session(current_state="ASK_NEW_DRIVER")
        session, outbound = sm.handle_ask_new_driver(session, "Ramesh 9876543210", "919999900001")

        assert session["driver_phone"] == "919876543210"
        # this is exactly the format Meta would send as `from` when the
        # driver later messages in — find_session()'s exact-match lookup
        # now actually succeeds
        assert session["driver_phone"].startswith("91") and len(session["driver_phone"]) == 12

    def test_engineer_assignment_exact_zone_match_beats_substring_collision(self, tmp_path, monkeypatch):
        """Bug: pure substring matching meant a zone like 'Punepur' could
        shadow the correct 'Pune' zone depending on CSV row order, since
        'pune' in 'punepur' is True."""
        engineers_csv = tmp_path / "engineers_ambiguous.csv"
        with open(engineers_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["engineer_id", "engineer_name", "phone_number", "zone"])
            w.writerow(["ENG_WRONG", "Wrong Engineer", "919000000099", "Punepur"])
            w.writerow(["ENG_RIGHT", "Right Engineer", "919000000001", "Pune"])
        monkeypatch.setattr(settings, "ENGINEERS_CSV", str(engineers_csv))

        engineer = engineer_service.assign_engineer("Pune")
        assert engineer["engineer_id"] == "ENG_RIGHT"

    def test_concurrent_ticket_creation_does_not_lose_tickets(self, tmp_csv_backend):
        """Bug: ticket_service.create_ticket() had zero file locking —
        concurrent bookings could race and one ticket would silently
        vanish from tickets.csv."""
        import threading

        sessions = [base_session(vehicle_no=f"MH12AB{i:04d}", extracted_service_location="Pune") for i in range(10)]
        results = []
        results_lock = threading.Lock()

        def _create(s):
            ticket = ticket_service.create_ticket(s)
            with results_lock:
                results.append(ticket)

        threads = [threading.Thread(target=_create, args=(s,)) for s in sessions]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        tickets_csv, _ = tmp_csv_backend
        with open(tickets_csv) as f:
            rows = list(csv.DictReader(f))

        assert len(results) == 10
        assert len(rows) == 10  # none lost to the race
        assert len({r["ticket_id"] for r in rows}) == 10  # all unique

    def test_session_transaction_finds_processes_and_writes_atomically(self, tmp_sessions_backend):
        session_manager.create_session(phone_number="919999977777", vehicle_no="MH12QQ0001", current_state="WAIT_DONE")

        with session_manager.session_transaction("919999977777") as session:
            assert session is not None
            assert session["current_state"] == "WAIT_DONE"
            session["current_state"] = "COMPLETED"

        reloaded = session_manager.find_session("919999977777")
        assert reloaded["current_state"] == "COMPLETED"

    def test_session_transaction_no_session_does_not_write_anything(self, tmp_sessions_backend):
        with session_manager.session_transaction("919999900000_does_not_exist") as session:
            assert session is None
        # should not have created a stray row for a phone number with no session
        assert session_manager.find_session("919999900000_does_not_exist") is None


# =========================== 21. BULK "GIVE EVERYTHING AT ONCE" EXTRACTION =

class TestBulkBookingExtraction:
    def test_multi_field_message_skips_straight_to_confirmation(self, monkeypatch):
        """
        Customer gives location, destination, date, time, contact person
        AND number all in one message from the very first booking
        question — should land straight on CONFIRM_SUMMARY, skipping
        every intermediate question.
        """
        monkeypatch.setattr(sm.llm, "extract_booking_slots", MagicMock(return_value={
            "current_location": "Nagpur Bypass",
            "destination_location": "Pune",
            "service_date": "",
            "service_time_window": "",
            "contact_person": "",
            "contact_number": "",
        }))
        session = base_session(current_state="ASK_CURRENT_LOCATION")

        session, outbound = sm.process_message(
            session,
            "Meri gaadi Nagpur Bypass ke paas hai aur hume Pune jaana hai kal subah",
            "919999900001",
        )

        assert session["current_location"] == "Nagpur Bypass"
        assert session["destination_location"] == "Pune"
        # city confirmation is never skipped, even when both fields are known
        assert session["current_state"] == "ASK_SERVICE_CITY_CONFIRMATION"

    def test_giving_everything_at_once_from_the_start(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "extract_booking_slots", MagicMock(return_value={
            "current_location": "Nagpur Bypass",
            "destination_location": "Pune",
            "service_date": "2026-07-10",
            "service_time_window": "05:00 PM",
            "contact_person": "Rahul",
            "contact_number": "9876543210",
        }))
        session = base_session(current_state="ASK_CURRENT_LOCATION")

        session, outbound = sm.process_message(
            session,
            "Nagpur bypass ke paas hu, Pune jaana hai, 10 July shaam 5 baje, contact Rahul 9876543210",
            "919999900001",
        )

        # still stops at the mandatory city-confirmation safety check,
        # everything else got skipped
        assert session["current_state"] == "ASK_SERVICE_CITY_CONFIRMATION"
        assert session["service_date"] == "2026-07-10"
        assert session["service_time_window"] == "05:00 PM"
        assert session["contact_person"] == "Rahul"
        assert session["contact_number"] == "9876543210"

    def test_short_single_answer_never_triggers_bulk_extraction(self, monkeypatch):
        """
        Ordinary one-word replies must NOT pay for the extra LLM call —
        the length pre-filter should skip bulk extraction entirely.
        """
        bulk_mock = MagicMock(side_effect=AssertionError("should not attempt bulk extraction on a short reply"))
        monkeypatch.setattr(sm.llm, "extract_booking_slots", bulk_mock)
        monkeypatch.setattr(sm.llm, "extract_free_text", MagicMock(return_value="Nagpur"))

        session = base_session(current_state="ASK_CURRENT_LOCATION", vehicle_state="RUNNING")
        session, outbound = sm.process_message(session, "Nagpur", "919999900001")

        assert session["current_state"] == "ASK_DESTINATION_LOCATION"

    def test_single_extra_field_is_not_enough_to_fast_forward(self, monkeypatch):
        """
        Only ONE field found (besides normal single-question flow) should
        NOT trigger the jump — falls through to the state's own handler,
        which still extracts that one field normally.
        """
        monkeypatch.setattr(sm.llm, "extract_booking_slots", MagicMock(return_value={
            "current_location": "Nagpur Bypass, right next to the old fort area",
            "destination_location": "",
            "service_date": "",
            "service_time_window": "",
            "contact_person": "",
            "contact_number": "",
        }))
        monkeypatch.setattr(sm.llm, "extract_free_text", MagicMock(return_value="Nagpur Bypass, right next to the old fort area"))
        session = base_session(current_state="ASK_CURRENT_LOCATION", vehicle_state="RUNNING")

        session, outbound = sm.process_message(
            session, "Nagpur Bypass ke paas hai, wahi purane fort ke area mein", "919999900001"
        )

        # normal single-field advancement, not a bulk jump
        assert session["current_state"] == "ASK_DESTINATION_LOCATION"

    def test_bulk_extraction_never_overwrites_already_captured_fields(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "extract_booking_slots", MagicMock(return_value={
            "current_location": "Should Not Overwrite",
            "destination_location": "Pune",
            "service_date": "2026-07-10",
            "service_time_window": "",
            "contact_person": "",
            "contact_number": "",
        }))
        session = base_session(current_state="ASK_DESTINATION_LOCATION", current_location="Original Nagpur Location")

        session, outbound = sm.process_message(
            session, "Pune jaana hai 10 July ko, jaldi book kar dijiye please", "919999900001"
        )

        assert session["current_location"] == "Original Nagpur Location"  # untouched
        assert session["destination_location"] == "Pune"

    def test_workshop_status_with_date_in_same_message_skips_ask_expected_date(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_vehicle_status", MagicMock(return_value="WORKSHOP"))
        monkeypatch.setattr(sm.llm, "extract_date", MagicMock(return_value="2026-07-20"))
        session = base_session(current_state="ASK_VEHICLE_STATUS")

        session, outbound = sm.handle_ask_vehicle_status(
            session, "Gaadi workshop mein hai, 20 July tak ready ho jayegi", "919999900001"
        )

        assert session["current_state"] == "COMPLETED"
        assert session["extracted_appointment_date"] == "2026-07-20"

    def test_workshop_status_without_date_still_asks_separately(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_vehicle_status", MagicMock(return_value="WORKSHOP"))
        monkeypatch.setattr(sm.llm, "extract_date", MagicMock(return_value=""))
        session = base_session(current_state="ASK_VEHICLE_STATUS")

        session, outbound = sm.handle_ask_vehicle_status(session, "Gaadi workshop mein hai", "919999900001")

        assert session["current_state"] == "ASK_EXPECTED_DATE"

    def test_running_status_reuses_location_and_destination_from_status_message(self, monkeypatch):
        monkeypatch.setattr(sm.llm, "classify_vehicle_status", MagicMock(return_value="RUNNING"))
        monkeypatch.setattr(sm.llm, "extract_booking_slots", MagicMock(return_value={
            "current_location": "Delhi",
            "destination_location": "Pune",
            "service_date": "",
            "service_time_window": "",
            "contact_person": "",
            "contact_number": "",
        }))
        session = base_session(current_state="ASK_VEHICLE_STATUS")

        session, outbound = sm.handle_ask_vehicle_status(
            session,
            "Delhi se Pune jaa rahi hai",
            "919999900001",
        )

        assert session["current_location"] == "Delhi"
        assert session["destination_location"] == "Pune"
        assert session["current_state"] == "ASK_SERVICE_CITY_CONFIRMATION"

    def test_bulk_extraction_works_identically_through_voice_path(self, monkeypatch):
        """
        Voice and WhatsApp share process_message() — a long spoken
        transcript with several answers at once should fast-forward
        exactly the same way as a WhatsApp message would.
        """
        monkeypatch.setattr(sm.llm, "extract_booking_slots", MagicMock(return_value={
            "current_location": "Nagpur Bypass",
            "destination_location": "Pune",
            "service_date": "",
            "service_time_window": "",
            "contact_person": "",
            "contact_number": "",
        }))
        session = base_session(current_state="ASK_CURRENT_LOCATION")

        # simulates a Twilio SpeechResult transcript, not a WhatsApp text
        session, outbound = sm.process_message(
            session,
            "meri gaadi nagpur bypass ke paas hai aur hamein pune jana hai",
            "919999900001",
        )

        assert session["destination_location"] == "Pune"
        assert session["current_state"] == "ASK_SERVICE_CITY_CONFIRMATION"


# =============================== 22. DIRECT BOOKING (NO CONFIRMATION) =====

class TestDirectBooking:
    """
    Verify that booking is created directly without confirmation step.
    Once all details are provided, ticket is immediately created and
    session state becomes COMPLETED.
    """
    def test_direct_ticket_creation_after_contact_number(self, monkeypatch, tmp_csv_backend):
        """When contact number is provided, ticket is created immediately."""
        monkeypatch.setattr(ticket_service, "create_ticket", MagicMock(return_value={
            "ticket_id": "TICKET_123",
            "engineer_id": "ENG_001",
            "engineer_name": "Ramesh",
            "engineer_phone": "919876543210",
        }))
        
        session = base_session(
            current_state="ASK_CONTACT_NUMBER",
            current_location="Nagpur",
            extracted_service_location="Nagpur",
            service_date="2026-07-10",
            service_time_window="10:00 AM",
            contact_person="Driver"
        )
        
        session, outbound = sm.handle_ask_contact_number(session, "9123456789", "919999900001")
        
        # Should NOT ask for confirmation; should show summary + ticket directly
        assert session["current_state"] == "COMPLETED"
        assert session["contact_number"] == "9123456789"
        assert session["ticket_id"] == "TICKET_123"
        assert len(outbound) == 2  # Summary + Ticket confirmation messages
        assert "Nagpur" in outbound[0]["text"]  # Summary has location
        assert "TICKET_123" in outbound[1]["text"]  # Ticket message

    def test_direct_booking_from_driver_contact_confirmation_yes(self, monkeypatch, tmp_csv_backend):
        """When driver confirms contact is correct, ticket is created immediately."""
        monkeypatch.setattr(sm.llm, "classify_yes_no", MagicMock(return_value="YES"))
        monkeypatch.setattr(ticket_service, "create_ticket", MagicMock(return_value={
            "ticket_id": "TICKET_456",
            "engineer_id": "ENG_002",
            "engineer_name": "Suresh",
            "engineer_phone": "919987654321",
        }))
        
        session = base_session(
            current_state="DRIVER_CONTACT_CONFIRMATION",
            driver_name="Deepak",
            driver_phone="9123456789",
            current_location="Pune",
            extracted_service_location="Pune",
            service_date="2026-07-11",
            service_time_window="02:00 PM",
        )
        
        session, outbound = sm.handle_driver_contact_confirmation(
            session, "Haan driver ka number sahi hai", "919999900001"
        )
        
        assert session["current_state"] == "COMPLETED"
        assert session["contact_number"] == "9123456789"  # Normalized with 91
        assert session["ticket_id"] == "TICKET_456"
        assert len(outbound) == 2

    def test_booking_correction_then_direct_creation(self, monkeypatch, tmp_csv_backend):
        """When correction is provided and validated, ticket is created directly."""
        monkeypatch.setattr(sm.llm, "extract_structured", MagicMock(return_value={
            "extracted_service_location": "Nagpur",
            "contact_person": "Manager",
            "contact_number": "9876543210",
        }))
        monkeypatch.setattr(ticket_service, "create_ticket", MagicMock(return_value={
            "ticket_id": "TICKET_789",
            "engineer_id": "ENG_003",
            "engineer_name": "Ravi",
            "engineer_phone": "919876543200",
        }))
        
        session = base_session(
            current_state="ASK_BOOKING_CORRECTION",
            current_location="Nagpur",
            service_date="2026-07-12",
            service_time_window="03:00 PM",
        )
        
        session, outbound = sm.handle_ask_booking_correction(
            session, "Service city Nagpur, contact Manager 9876543210", "919999900001"
        )
        
        assert session["current_state"] == "COMPLETED"
        assert session["ticket_id"] == "TICKET_789"
        assert len(outbound) == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))