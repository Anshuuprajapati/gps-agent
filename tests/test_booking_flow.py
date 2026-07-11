from datetime import date, timedelta

from core.state_machine import handle_ask_current_location, handle_ask_expected_date, handle_ask_service_date, handle_ask_vehicle_status, handle_start


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


def test_workshop_status_asks_expected_date(monkeypatch):
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


def test_running_status_asks_destination_location(monkeypatch):
    session = {"current_state": "ASK_VEHICLE_STATUS", "vehicle_no": "MH16EF9012"}

    class DummyLLM:
        def classify_vehicle_status(self, *_args, **_kwargs):
            return "RUNNING"

        def extract_booking_slots(self, *_args, **_kwargs):
            return {}

    import core.state_machine as state_machine

    monkeypatch.setattr(state_machine, "llm", DummyLLM())
    updated_session, outbound = handle_ask_vehicle_status(session, "gadi chal rahi hai", "9999999999")

    assert updated_session["current_state"] == "ASK_DESTINATION_LOCATION"
    assert outbound[0]["text"].strip() == "Vehicle kis jagah jaa rahi hai?"


def test_broken_vehicle_asks_gps_or_vehicle_clarification(monkeypatch):
    session = {"current_state": "ASK_VEHICLE_STATUS", "vehicle_no": "MH16EF9012"}

    import core.state_machine as state_machine

    updated_session, outbound = handle_ask_vehicle_status(session, "gaadi kharab hai", "9999999999")

    assert updated_session["current_state"] == "ASK_VEHICLE_STATUS"
    assert "Kya GPS kharab/tuta hai ya vehicle mein koi aur problem hai?" in outbound[0]["interactive"]["body"]["text"]


def test_start_unknown_root_cause_does_not_show_vehicle_status_options(monkeypatch):
    session = {"vehicle_no": "MH16EF9012", "last_location": "Delhi", "gpstime": "2026-07-05 10:00"}

    import core.state_machine as state_machine

    monkeypatch.setattr(state_machine.gps_service, "analyze_root_cause", lambda _session: "UNKNOWN")
    updated_session, outbound = handle_start(session, "", "9999999999")

    assert updated_session["current_state"] == "ASK_VEHICLE_STATUS"
    assert "Vehicle ki current status batayein" in outbound[0]["text"]
    assert "Vehicle abhi kis condition me hai?" not in outbound[0]["text"]
    assert "Vehicle status options" not in outbound[0]["text"]


def test_non_running_issue_skips_destination_location_question(monkeypatch):
    session = {"current_state": "ASK_CURRENT_LOCATION", "vehicle_state": "GPS_DAMAGED", "current_location": ""}

    class DummyLLM:
        def extract_free_text(self, *_args, **_kwargs):
            return "Delhi"

    import core.state_machine as state_machine

    monkeypatch.setattr(state_machine, "llm", DummyLLM())
    updated_session, outbound = handle_ask_current_location(session, "Delhi", "9999999999")

    assert updated_session["current_state"] == "ASK_SERVICE_CITY_CONFIRMATION"
    assert updated_session["current_location"] == "Delhi"
    assert updated_session["destination_location"] == "Delhi"
    assert "Kya Delhi mein aaj ke liye service book kar dein?" in outbound[0]["text"]


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
