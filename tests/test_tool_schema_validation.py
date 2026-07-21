"""
tests/test_tool_schema_validation.py

Unit tests for the "ask for one JSON object, validate in Python" contract
between llm_handler.decide_next_action and core/tools.py's tool menu.
None of these hit a real LLM — they stub core.llm_handler._call_llm the
same way the real gpt-oss-120b truncation bug was reproduced this session,
and assert the engine never crashes or silently stalls on a bad response.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import llm_handler
from core import tools


class TestValidateToolCall:
    def test_unknown_tool_name_is_rejected(self):
        with pytest.raises(ValueError):
            tools.validate_tool_call("made_up_tool", {})

    def test_missing_required_arg_is_rejected(self):
        with pytest.raises(ValueError):
            tools.validate_tool_call("update_ticket_status", {})  # new_status is required

    def test_invalid_enum_value_is_rejected(self):
        with pytest.raises(ValueError):
            tools.validate_tool_call("update_ticket_status", {"new_status": "BANANA"})

    def test_valid_update_ticket_status_call_passes(self):
        result = tools.validate_tool_call("update_ticket_status", {"new_status": "IN_PROGRESS"})
        assert result == {"new_status": "IN_PROGRESS", "note": ""}

    def test_optional_args_get_sane_defaults(self):
        result = tools.validate_tool_call("close_ticket", {})
        assert result == {"note": ""}

    def test_ask_for_accepts_empty_args(self):
        result = tools.validate_tool_call("ask_for", {})
        assert result == {"field": ""}


class TestDecideNextActionFailureModes:
    def test_malformed_json_falls_back_to_ask_for(self, monkeypatch):
        # The exact failure mode seen this session: a truncated JSON object
        # from a reasoning model that spent its token budget on hidden
        # reasoning before the visible JSON.
        monkeypatch.setattr(llm_handler, "_call_llm", lambda *_a, **_k: '{"tool_name": "create_ticket", "tool_ar')
        result = llm_handler.decide_next_action("ASK_VEHICLE_STATUS", "", "haan book kar do")
        assert result == {"tool_name": "ask_for", "tool_args": {}}

    def test_missing_tool_name_key_falls_back_to_ask_for(self, monkeypatch):
        monkeypatch.setattr(llm_handler, "_call_llm", lambda *_a, **_k: '{"tool_args": {}}')
        result = llm_handler.decide_next_action("ASK_VEHICLE_STATUS", "", "anything")
        assert result == {"tool_name": "ask_for", "tool_args": {}}

    def test_network_failure_falls_back_to_ask_for(self, monkeypatch):
        def _boom(*_a, **_k):
            raise RuntimeError("network down")
        monkeypatch.setattr(llm_handler, "_call_llm", _boom)
        result = llm_handler.decide_next_action("ASK_VEHICLE_STATUS", "", "anything")
        assert result == {"tool_name": "ask_for", "tool_args": {}}

    def test_well_formed_response_is_parsed_through(self, monkeypatch):
        monkeypatch.setattr(
            llm_handler, "_call_llm",
            lambda *_a, **_k: '{"tool_name": "ticket_inquiry", "tool_args": {"ticket_id": "TKT-ABC12345"}}',
        )
        result = llm_handler.decide_next_action("COMPLETED", "", "status kya hai")
        assert result == {"tool_name": "ticket_inquiry", "tool_args": {"ticket_id": "TKT-ABC12345"}}

    def test_response_missing_tool_args_gets_empty_dict_default(self, monkeypatch):
        monkeypatch.setattr(llm_handler, "_call_llm", lambda *_a, **_k: '{"tool_name": "transfer_to_driver"}')
        result = llm_handler.decide_next_action("WAIT_DONE", "", "driver se baat karao")
        assert result == {"tool_name": "transfer_to_driver", "tool_args": {}}
