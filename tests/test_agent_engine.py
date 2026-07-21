"""
tests/test_agent_engine.py

End-to-end tests for the v2 tool-calling engine (core/agent_engine.py),
mirroring test.py's mocking style. Every test mocks
core.agent_engine.llm.decide_next_action (the one consolidated reasoning
call) plus whatever leaf LLM/service call the chosen tool needs — nothing
here hits a real network or a real CSV file.
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import agent_engine
from core import state_machine as sm


def _session(**overrides):
    session = {
        "phone_number": "919999900001",
        "vehicle_no": "MH12AB1234",
        "last_location": "Pune Bypass",
        "handler": "OWNER",
        "current_state": "ASK_VEHICLE_STATUS",
        "pending_action_json": "",
    }
    session.update(overrides)
    return session


def _decision(monkeypatch, tool_name, tool_args=None):
    monkeypatch.setattr(
        agent_engine.llm, "decide_next_action",
        MagicMock(return_value={"tool_name": tool_name, "tool_args": tool_args or {}}),
    )


class TestDeterministicPreChecksNeverCallTheLLM:
    """These four cases must resolve without ever reaching decide_next_action
    — mocking it to raise proves the pre-check actually short-circuited."""

    def _boom_if_called(self, monkeypatch):
        monkeypatch.setattr(
            agent_engine.llm, "decide_next_action",
            MagicMock(side_effect=AssertionError("decide_next_action should not have been called")),
        )

    def test_explicit_ticket_id_short_circuits(self, monkeypatch):
        self._boom_if_called(monkeypatch)
        monkeypatch.setattr(
            sm.ticket_service, "get_ticket_by_id",
            MagicMock(return_value={
                "ticket_id": "TKT-448C059E", "status": "ASSIGNED", "vehicle_no": "MH12AB1234",
                "service_location": "Pune", "service_date": "2026-07-25",
                "service_time": "11:00 AM", "engineer_name": "Rahul", "engineer_phone": "919000000005",
            }),
        )
        session = _session()
        session, outbound = agent_engine.process_message_v2(session, "TKT-448C059E", "919999900001")
        assert "TKT-448C059E" in outbound[0]["text"]

    def test_generic_acknowledgment_short_circuits(self, monkeypatch):
        self._boom_if_called(monkeypatch)
        session = _session()
        session, outbound = agent_engine.process_message_v2(session, "thanks", "919999900001")
        assert outbound[0]["text"]

    def test_driver_handoff_request_short_circuits(self, monkeypatch):
        self._boom_if_called(monkeypatch)
        monkeypatch.setattr(sm.driver_service, "get_driver_details", MagicMock(return_value={"name": "", "phone": ""}))
        session = _session(current_state="WAIT_DONE")
        session, outbound = agent_engine.process_message_v2(session, "driver se baat karao", "919999900001")
        assert session["current_state"] == "ASK_NEW_DRIVER"

    def test_direct_tech_dispatch_phrase_short_circuits(self, monkeypatch):
        self._boom_if_called(monkeypatch)
        monkeypatch.setattr(
            sm.llm, "extract_tech_dispatch_slots",
            lambda *_a, **_k: {"service_location": "Punjabi Bagh", "service_date": "", "service_time_window": "", "contact_person": "", "contact_number": ""},
        )
        monkeypatch.setattr(
            sm.ticket_service, "create_ticket",
            MagicMock(return_value={"ticket_id": "TKT-DIRECT1", "engineer_id": "ENG-1", "engineer_name": "Eng One", "engineer_phone": "919900000000", "existing_ticket": False}),
        )
        session = _session()
        session, outbound = agent_engine.process_message_v2(session, "abhi koi bhej do", "919999900001")
        assert session["current_state"] == "COMPLETED"
        assert any("TKT-DIRECT1" in (o.get("text") or "") for o in outbound)


class TestAskForTool:
    def test_ask_for_prompts_next_missing_booking_field(self, monkeypatch):
        _decision(monkeypatch, "ask_for")
        session = _session(current_state="ASK_CURRENT_LOCATION", current_location="Nagpur")
        session, outbound = agent_engine.process_message_v2(session, "kuch bhi", "919999900001")
        assert session["current_state"] == "ASK_DESTINATION_LOCATION"
        assert outbound[0]["text"]


class TestConfirmGate:
    def test_create_ticket_is_gated_not_executed_immediately(self, monkeypatch):
        create_ticket_mock = MagicMock(return_value={"ticket_id": "TKT-SHOULDNOT", "engineer_id": "", "engineer_name": "", "engineer_phone": "", "existing_ticket": False})
        monkeypatch.setattr(sm.ticket_service, "create_ticket", create_ticket_mock)
        _decision(monkeypatch, "create_ticket")

        session = _session(
            current_location="Nagpur", destination_location="Pune", service_city_confirmed="TRUE",
            extracted_service_location="Pune", service_date="2026-07-25", service_time_window="05:00 PM",
            contact_person="Raju", contact_number="9876500000",
        )
        session, outbound = agent_engine.process_message_v2(session, "haan sab sahi hai", "919999900001")

        create_ticket_mock.assert_not_called()
        assert session["current_state"] == "CONFIRM_SUMMARY"
        assert session["pending_action_json"]

    def test_confirming_yes_executes_the_stashed_tool(self, monkeypatch):
        create_ticket_mock = MagicMock(return_value={"ticket_id": "TKT-REAL0001", "engineer_id": "ENG001", "engineer_name": "Ramesh", "engineer_phone": "919000000001", "existing_ticket": False})
        monkeypatch.setattr(sm.ticket_service, "create_ticket", create_ticket_mock)
        monkeypatch.setattr(agent_engine.llm, "classify_yes_no", MagicMock(return_value="YES"))

        session = _session(
            current_state="CONFIRM_SUMMARY",
            pending_action_json='{"tool_name": "create_ticket", "tool_args": {}}',
        )
        session, outbound = agent_engine.process_message_v2(session, "haan", "919999900001")

        create_ticket_mock.assert_called_once()
        assert session["pending_action_json"] == ""
        assert session["current_state"] == "COMPLETED"
        assert any("TKT-REAL0001" in (o.get("text") or "") for o in outbound)

    def test_declining_cancels_the_pending_action(self, monkeypatch):
        create_ticket_mock = MagicMock()
        monkeypatch.setattr(sm.ticket_service, "create_ticket", create_ticket_mock)
        monkeypatch.setattr(agent_engine.llm, "classify_yes_no", MagicMock(return_value="NO"))

        session = _session(
            current_state="CONFIRM_SUMMARY",
            pending_action_json='{"tool_name": "create_ticket", "tool_args": {}}',
        )
        session, outbound = agent_engine.process_message_v2(session, "nahi", "919999900001")

        create_ticket_mock.assert_not_called()
        assert session["pending_action_json"] == ""

    def test_unclear_confirmation_reprompts_without_resolving(self, monkeypatch):
        create_ticket_mock = MagicMock()
        monkeypatch.setattr(sm.ticket_service, "create_ticket", create_ticket_mock)
        monkeypatch.setattr(agent_engine.llm, "classify_yes_no", MagicMock(return_value="UNCLEAR"))

        session = _session(
            current_state="CONFIRM_SUMMARY",
            pending_action_json='{"tool_name": "create_ticket", "tool_args": {}}',
        )
        session, outbound = agent_engine.process_message_v2(session, "hmm shayad", "919999900001")

        create_ticket_mock.assert_not_called()
        assert session["pending_action_json"]  # still pending — nothing resolved

    def test_update_ticket_status_is_also_gated(self, monkeypatch):
        update_mock = MagicMock(return_value={"ticket_id": "TKT-EXIST01", "status": "IN_PROGRESS"})
        monkeypatch.setattr(sm.ticket_service, "update_ticket_status", update_mock)
        _decision(monkeypatch, "update_ticket_status", {"new_status": "IN_PROGRESS", "note": ""})

        session = _session(current_state="COMPLETED", ticket_id="TKT-EXIST01")
        session, outbound = agent_engine.process_message_v2(session, "kaam shuru ho gaya", "919999900001")

        update_mock.assert_not_called()
        assert session["pending_action_json"]


class TestTicketInquiryTool:
    def test_ticket_inquiry_tool_looks_up_real_ticket(self, monkeypatch):
        _decision(monkeypatch, "ticket_inquiry", {"ticket_id": ""})
        monkeypatch.setattr(
            sm.ticket_service, "get_ticket_by_id",
            MagicMock(return_value={
                "ticket_id": "TKT-999AAAAA", "status": "ASSIGNED", "vehicle_no": "MH12AB1234",
                "service_location": "Nagpur", "service_date": "2026-07-21",
                "service_time": "05:00 PM", "engineer_name": "Test Engineer", "engineer_phone": "919000000001",
            }),
        )
        session = _session(current_state="COMPLETED", ticket_id="TKT-999AAAAA")
        session, outbound = agent_engine.process_message_v2(session, "meri complaint ka status kya hai", "919999900001")
        assert "TKT-999AAAAA" in outbound[0]["text"]


class TestUnknownToolFallback:
    def test_unknown_tool_name_falls_back_to_ask_for(self, monkeypatch):
        monkeypatch.setattr(
            agent_engine.llm, "decide_next_action",
            MagicMock(return_value={"tool_name": "not_a_real_tool", "tool_args": {}}),
        )
        session = _session(current_state="ASK_CURRENT_LOCATION")
        session, outbound = agent_engine.process_message_v2(session, "kuch bhi", "919999900001")
        # Falls back to ask_for's deterministic prompt rather than crashing.
        assert session["current_state"] in ("ASK_CURRENT_LOCATION", "ASK_DESTINATION_LOCATION")
        assert outbound[0]["text"]
