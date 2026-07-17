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
    assert "Main aapki madad yahin continue karunga" in outbound[0]["text"]


def test_conversation_memory_keeps_only_last_five_entries():
    import core.state_machine as state_machine

    session = {
        "current_state": "ASK_SERVICE_DATE",
        "conversation_summary": "\n".join([
            "USER: one",
            "BOT: two",
            "USER: three",
            "BOT: four",
            "USER: five",
        ]),
    }

    state_machine.record_conversation_turn(session, "six", [{"text": "seven"}])

    assert session["conversation_summary"].splitlines() == [
        "USER: three",
        "BOT: four",
        "USER: five",
        "USER: six",
        "BOT: seven",
    ]


def test_build_conversation_context_includes_saved_summary():
    import core.state_machine as state_machine

    session = {
        "vehicle_no": "MH16EF9012",
        "current_state": "ASK_SERVICE_DATE",
        "vehicle_state": "RUNNING",
        "conversation_summary": "USER: kal subah\nBOT: theek hai",
    }

    context = state_machine.build_conversation_context(session)

    assert "current_state: ASK_SERVICE_DATE" in context
    assert "vehicle_no: MH16EF9012" in context
    assert "USER: kal subah" in context
    assert "BOT: theek hai" in context


def test_completed_state_uses_open_ended_reply(monkeypatch):
    import core.state_machine as state_machine

    session = {"current_state": "COMPLETED", "ticket_id": "TKT-1234"}

    monkeypatch.setattr(state_machine.gps_service, "verify_gps", lambda _session: True)

    updated_session, outbound = state_machine.handle_completed(session, "hello", "9999999999")

    assert updated_session["current_state"] == "COMPLETED"
    assert "GPS data aana shuru ho gaya hai" in outbound[0]["text"]
    assert "continue" in outbound[0]["text"].lower()


def test_completed_state_can_still_answer_general_questions(monkeypatch):
    import core.state_machine as state_machine

    session = {"current_state": "COMPLETED", "ticket_id": "TKT-1234", "last_prompt_text": "last prompt"}

    monkeypatch.setattr(state_machine.llm, "is_general_question", lambda *_args, **_kwargs: "GENERAL_QUESTION")
    monkeypatch.setattr(state_machine.llm, "answer_from_knowledge_base", lambda *_args, **_kwargs: "Hamari team 9 se 9 available hai.")

    updated_session, outbound = state_machine.process_message(session, "Aap kitne baje tak available ho?", "9999999999")

    assert updated_session["current_state"] == "COMPLETED"
    assert "Hamari team 9 se 9 available hai." in outbound[0]["text"]
    assert "last prompt" in outbound[0]["text"]


def test_existing_ticket_is_reused_instead_of_creating_a_new_one(monkeypatch, tmp_path):
    import pandas as pd
    import services.ticket_service as ticket_service
    from config import settings

    tickets_csv = tmp_path / "tickets.csv"
    pd.DataFrame([
        {
            "ticket_id": "TKT-EXIST01",
            "vehicle_no": "MH16EF9012",
            "issue_type": "BATTERY",
            "current_location": "Nagpur",
            "service_location": "Pune",
            "service_date": "2026-07-18",
            "service_time": "05:00 PM",
            "contact_person": "Anshu",
            "contact_number": "9876500000",
            "engineer_id": "ENG-1",
            "engineer_name": "Engineer One",
            "engineer_phone": "919900000000",
            "status": "ASSIGNED",
        }
    ]).to_csv(tickets_csv, index=False)

    monkeypatch.setattr(settings, "TICKETS_CSV", str(tickets_csv))

    session = {"vehicle_no": "MH16EF9012", "extracted_service_location": "Pune", "vehicle_state": "RUNNING"}

    ticket = ticket_service.create_ticket(session)

    assert ticket["ticket_id"] == "TKT-EXIST01"
    assert ticket["existing_ticket"] is True

    saved = pd.read_csv(tickets_csv, dtype=str).fillna("")
    assert len(saved) == 1
    assert saved.iloc[0]["ticket_id"] == "TKT-EXIST01"


def test_completed_state_reassures_when_gps_is_back_online(monkeypatch):
    import core.state_machine as state_machine

    session = {"current_state": "COMPLETED", "vehicle_no": "MH16EF9012"}

    monkeypatch.setattr(state_machine.gps_service, "verify_gps", lambda _session: True)

    updated_session, outbound = state_machine.handle_completed(session, "hello gadi running ho gyi", "9999999999")

    assert updated_session["current_state"] == "COMPLETED"
    assert "GPS data aana shuru ho gaya hai" in outbound[0]["text"]


def test_completed_state_reopens_vehicle_status_flow_when_gps_is_still_offline(monkeypatch):
    import core.state_machine as state_machine

    session = {"current_state": "COMPLETED", "vehicle_no": "MH16EF9012"}

    monkeypatch.setattr(state_machine.gps_service, "verify_gps", lambda _session: False)

    updated_session, outbound = state_machine.handle_completed(session, "haa gps nahi chal raha", "9999999999")

    assert updated_session["current_state"] == "ASK_VEHICLE_STATUS"
    assert "GPS data abhi receive nahi ho raha hai" in outbound[0]["text"]
    assert "Workshop" in outbound[1]["interactive"] ["action"]["buttons"][0]["reply"]["title"]
