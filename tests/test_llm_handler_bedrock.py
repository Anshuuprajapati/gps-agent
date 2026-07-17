import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import core.llm_handler as llm_handler


class DummyResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [
                {"message": {"content": '{"value": "YES"}'}}
            ]
        }


def test_bedrock_provider_uses_configured_endpoint(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout, headers):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        captured["headers"] = headers
        return DummyResponse()

    monkeypatch.setattr(llm_handler.requests, "post", fake_post)
    monkeypatch.setattr(llm_handler.settings, "LLM_PROVIDER", "bedrock")
    monkeypatch.setattr(llm_handler.settings, "BEDROCK_API_KEY", "test-bearer-token")
    monkeypatch.setattr(llm_handler.settings, "BEDROCK_BASE_URL", "https://example.test/v1/chat/completions")
    monkeypatch.setattr(llm_handler.settings, "BEDROCK_MODEL", "openai.gpt-oss-120b")

    result = llm_handler._call_llm("hello")

    assert result == '{"value": "YES"}'
    assert captured["url"] == "https://example.test/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-bearer-token"
    assert captured["json"]["model"] == "openai.gpt-oss-120b"


def test_extract_structured_includes_conversation_context(monkeypatch):
    captured = {}

    def fake_call_llm(prompt):
        captured["prompt"] = prompt
        return '{"value": "2026-07-05"}'

    monkeypatch.setattr(llm_handler, "_call_llm", fake_call_llm)

    result = llm_handler.extract_date(
        "ASK_SERVICE_DATE",
        "kal",
        conversation_context="USER: first message\nBOT: follow-up question",
    )

    assert result == "2026-07-05"
    assert "CONVERSATION_CONTEXT:" in captured["prompt"]
    assert "USER: first message" in captured["prompt"]
    assert "CURRENT_STATE: ASK_SERVICE_DATE" in captured["prompt"]
