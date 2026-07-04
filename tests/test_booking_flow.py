from datetime import date, timedelta

from core.state_machine import handle_ask_expected_date, handle_ask_service_date, handle_ask_vehicle_status


def make_session():
    return {
        "vehicle_no": "MH16EF9012",
        "current_state": "ASK_SERVICE_DATE",
        "current_location": "Delhi",
        "destination_location": "Pune",
        "extracted_service_location": "Pune",
    }


def test_yes_reply_does_not_become_date(monkeypatch):
    session = make_session()

    # Simulate a simple yes reply; should not store "yes" as a date.
    class DummyLLM:
        def classify_yes_no(self, *_args, **_kwargs):
            return "YES"

        def extract_date(self, *_args, **_kwargs):
            return ""

    import core.state_machine as state_machine

    monkeypatch.setattr(state_machine, "llm", DummyLLM())
    updated_session, outbound = handle_ask_service_date(session, "Haa", "9999999999")

    assert updated_session["service_date"] == date.today().isoformat()
    assert updated_session["current_state"] == "ASK_SERVICE_TIME_WINDOW"
    assert outbound[0]["text"].startswith("Kis time") or "time" in outbound[0]["text"].lower()


def test_tomorrow_date_is_normalized_for_kal(monkeypatch):
    session = make_session()

    class DummyLLM:
        def extract_date(self, *_args, **_kwargs):
            return "2026-07-05"

        def classify_yes_no(self, *_args, **_kwargs):
            return "UNCLEAR"

    import core.state_machine as state_machine

    monkeypatch.setattr(state_machine, "llm", DummyLLM())
    updated_session, _ = handle_ask_service_date(session, "Kal", "9999999999")

    assert updated_session["service_date"] == "2026-07-05"


def test_workshop_status_uses_short_expected_date_prompt(monkeypatch):
    session = {"current_state": "ASK_VEHICLE_STATUS", "vehicle_no": "MH16EF9012"}

    class DummyLLM:
        def classify_vehicle_status(self, *_args, **_kwargs):
            return "WORKSHOP"

        def generate_contextual_response(self, *_args, **_kwargs):
            return ""

    import core.state_machine as state_machine

    monkeypatch.setattr(state_machine, "llm", DummyLLM())
    updated_session, outbound = handle_ask_vehicle_status(session, "garage me standing hai", "9999999999")

    assert updated_session["current_state"] == "ASK_EXPECTED_DATE"
    assert outbound[0]["text"].strip() == "Vehicle kab tak running mein aa jayegi?"


def test_expected_date_closes_with_short_confirmation(monkeypatch):
    session = {"current_state": "ASK_EXPECTED_DATE"}

    class DummyLLM:
        def extract_date(self, *_args, **_kwargs):
            return "2026-07-05"

    import core.state_machine as state_machine

    monkeypatch.setattr(state_machine, "llm", DummyLLM())
    updated_session, outbound = handle_ask_expected_date(session, "Kal", "9999999999")

    assert updated_session["current_state"] == "COMPLETED"
    assert "✅ Thik hai" in outbound[0]["text"]
    assert "Filhal case close kar rahe hain" in outbound[0]["text"]
