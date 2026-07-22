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

    monkeypatch.setattr(state_machine.llm, "classify_global_intent", lambda *_args, **_kwargs: "GENERAL_QUESTION")
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


def test_extract_tech_dispatch_slots_falls_back_for_punjabi_bagh_time(monkeypatch):
    import core.llm_handler as llm_handler

    monkeypatch.setattr(llm_handler, "_call_llm", lambda *_args, **_kwargs: "{}")

    slots = llm_handler.extract_tech_dispatch_slots("Send tech by tomorrow at Punjabi Bagh 11 am")

    assert slots["service_location"] == "Punjabi Bagh"
    assert slots["service_date"] == (date.today() + timedelta(days=1)).isoformat()
    assert slots["service_time_window"] == "11:00 AM"


def test_driver_reply_completes_direct_tech_flow_without_extra_phone_prompt(monkeypatch):
    import core.state_machine as state_machine

    session = {
        "current_state": "ASK_CONTACT_PERSON",
        "vehicle_no": "MH16EF9012",
        "driver_name": "Sarvesh Swami",
        "driver_phone": "8290323758",
        "extracted_service_location": "Punjabi Bagh",
        "service_date": "2026-07-18",
        "service_time_window": "11:00 AM",
    }

    monkeypatch.setattr(
        state_machine.ticket_service,
        "create_ticket",
        lambda session: {
            "ticket_id": "TKT-DRIVER1",
            "engineer_id": "ENG-1",
            "engineer_name": "Engineer One",
            "engineer_phone": "919900000000",
            "existing_ticket": False,
        },
    )

    updated_session, outbound = state_machine.handle_ask_contact_person(session, "driver se", "9999999999")

    assert updated_session["current_state"] == "COMPLETED"
    assert updated_session["contact_person"] == "Driver (Sarvesh Swami)"
    assert updated_session["contact_number"] == "8290323758"
    assert any("TKT-DRIVER1" in (out.get("text") or "") for out in outbound)


def test_direct_tech_skips_contact_person_prompt_if_driver_exists(monkeypatch):
    import core.state_machine as state_machine

    session = {
        "current_state": "COMPLETED",
        "vehicle_no": "MH16EF9012",
        "driver_name": "Sarvesh Swami",
        "driver_phone": "8290323758",
        "last_location": "Nagpur Bypass",
    }

    monkeypatch.setattr(
        state_machine.ticket_service,
        "create_ticket",
        lambda session: {
            "ticket_id": "TKT-SKIP-CONTACT",
            "engineer_id": "ENG-1",
            "engineer_name": "Engineer One",
            "engineer_phone": "919900000000",
            "existing_ticket": False,
        },
    )

    updated_session, outbound = state_machine.process_message(session, "Send tech by tomorrow at Punjabi Bagh 11 am", "9999999999")

    assert updated_session["current_state"] == "COMPLETED"
    assert updated_session["extracted_service_location"] == "Punjabi Bagh"
    assert updated_session["contact_person"] == "Sarvesh Swami"
    assert updated_session["contact_number"] == "8290323758"
    assert any("TKT-SKIP-CONTACT" in (out.get("text") or "") for out in outbound)


def test_completed_state_handles_vehicle_status_correction(monkeypatch):
    import core.state_machine as state_machine
    from core import llm_handler

    session = {
        "current_state": "COMPLETED",
        "vehicle_no": "MH16EF9012",
        "vehicle_state": "WORKSHOP",
        "extracted_appointment_date": "2026-07-20",
        "extracted_service_location": "punjabi Bagh",
        "service_time_window": "11:00 AM",
        "contact_number": "8290323758",
        "contact_person": "Sarvesh Swami",
        "current_location": "Nagpur",
    }

    monkeypatch.setattr(
        llm_handler,
        "classify_vehicle_status",
        lambda *_args, **_kwargs: "RUNNING"
    )
    monkeypatch.setattr(
        llm_handler,
        "extract_date",
        lambda *_args, **_kwargs: "2026-07-15"
    )

    updated_session, outbound = state_machine.handle_completed(session, "nahi vehicle running me hai 15 tariq ko aayegi", "9999999999")

    assert updated_session["vehicle_state"] == "RUNNING"
    assert updated_session["extracted_appointment_date"] == "2026-07-15"
    assert updated_session["service_date"] == "2026-07-15", "service_date should be updated with new date"
    assert updated_session["current_state"] == "COMPLETED"
    response_text = outbound[0]["text"]
    assert "update" in response_text.lower(), "Response should mention update"
    assert "2026-07-15" in response_text, "Response should show updated date in booking summary"
    assert "Sarvesh Swami" in response_text, "Response should preserve driver name in booking summary"


def test_vehicle_status_unclear_extracts_destination_and_continues(monkeypatch):
    import core.state_machine as state_machine
    from core import llm_handler

    session = {
        "current_state": "ASK_VEHICLE_STATUS",
        "vehicle_no": "MH16EF9012",
    }

    monkeypatch.setattr(
        llm_handler,
        "classify_vehicle_status",
        lambda *_args, **_kwargs: "UNCLEAR"
    )
    monkeypatch.setattr(
        llm_handler,
        "extract_free_text",
        lambda *_args, **_kwargs: "Pune"
    )

    updated_session, outbound = state_machine.handle_ask_vehicle_status(session, "pune jaa rahi hai pta nahi kab aayegi", "9999999999")

    assert updated_session["destination_location"] == "Pune"
    assert updated_session["vehicle_state"] == "RUNNING"
    # City confirmation is never skipped just because a destination is known
    # (see _next_missing_booking_state's documented policy) — only after an
    # explicit yes/no on the service city does the flow move to the date.
    assert updated_session["current_state"] == "ASK_SERVICE_CITY_CONFIRMATION"


def test_driver_update_message_extracts_and_applies_new_driver(monkeypatch):
    import core.state_machine as state_machine
    from core import llm_handler

    session = {
        "current_state": "ASK_VEHICLE_STATUS",
        "vehicle_no": "MH16EF9012",
        "driver_name": "Old Driver",
        "driver_phone": "9876543210",
    }

    monkeypatch.setattr(llm_handler, "classify_global_intent", lambda *_args, **_kwargs: "DRIVER_UPDATE")
    monkeypatch.setattr(
        llm_handler,
        "extract_name_and_phone",
        lambda *_args, **_kwargs: {"name": "New Driver", "phone": "8290323758"}
    )

    updated_session, outbound = state_machine.process_message(session, "driver ye hai asdfghjkl 1234567890", "9999999999")

    assert updated_session["driver_name"] == "New Driver"
    assert updated_session["driver_phone"] == "918290323758"
    assert any("New Driver" in (out.get("text") or "") for out in outbound)
    assert any("8290323758" in (out.get("text") or "") for out in outbound)


def test_driver_change_request_asks_for_new_driver(monkeypatch):
    import core.state_machine as state_machine

    session = {
        "current_state": "COMPLETED",
        "vehicle_no": "MH16EF9012",
        "driver_name": "Sarvesh Swami",
        "driver_phone": "8290323758",
    }

    updated_session, outbound = state_machine.process_message(session, "driver change hai", "9999999999")

    assert updated_session["current_state"] == "ASK_NEW_DRIVER"
    assert "driver" in outbound[0]["text"].lower()


def test_process_message_uses_direct_tech_contact_handler_for_driver_reply(monkeypatch):
    import core.state_machine as state_machine

    session = {
        "current_state": "ASK_CONTACT_PERSON",
        "handler": "OWNER",
        "vehicle_no": "MH16EF9012",
        "driver_name": "Sarvesh Swami",
        "driver_phone": "8290323758",
        "extracted_service_location": "Punjabi Bagh",
        "service_date": "2026-07-18",
        "service_time_window": "11:00 AM",
    }

    monkeypatch.setattr(
        state_machine.ticket_service,
        "create_ticket",
        lambda session: {
            "ticket_id": "TKT-DRIVER2",
            "engineer_id": "ENG-1",
            "engineer_name": "Engineer One",
            "engineer_phone": "919900000000",
            "existing_ticket": False,
        },
    )

    updated_session, outbound = state_machine.process_message(session, "driver se", "9999999999")

    assert updated_session["current_state"] == "COMPLETED"
    assert updated_session["contact_person"] == "Driver (Sarvesh Swami)"
    assert updated_session["contact_number"] == "8290323758"
    assert any("TKT-DRIVER2" in (out.get("text") or "") for out in outbound)


def test_direct_send_tech_message_creates_ticket_without_asking_status(monkeypatch):
    import core.state_machine as state_machine

    session = {
        "current_state": "COMPLETED",
        "vehicle_no": "MH16EF9012",
        "last_location": "Nagpur Bypass",
    }

    monkeypatch.setattr(
        state_machine.llm,
        "extract_tech_dispatch_slots",
        lambda *_args, **_kwargs: {
            "service_location": "Punjabi Bagh",
            "service_date": "2026-07-18",
            "service_time_window": "11:00 AM",
            "contact_person": "Anshu",
            "contact_number": "9876500000",
        },
    )
    monkeypatch.setattr(
        state_machine.ticket_service,
        "create_ticket",
        lambda session: {
            "ticket_id": "TKT-DIRECT1",
            "engineer_id": "ENG-1",
            "engineer_name": "Engineer One",
            "engineer_phone": "919900000000",
            "existing_ticket": False,
        },
    )

    updated_session, outbound = state_machine.process_message(session, "Send tech by tomorrow at Punjabi Bagh 11 am", "9999999999")

    assert updated_session["current_state"] == "COMPLETED"
    assert updated_session["extracted_service_location"] == "Punjabi Bagh"
    assert updated_session["service_date"] == "2026-07-18"
    assert updated_session["service_time_window"] == "11:00 AM"
    assert updated_session["contact_person"] == "Anshu"
    assert updated_session["contact_number"] == "9876500000"
    assert any("TKT-DIRECT1" in (out.get("text") or "") for out in outbound)


def test_direct_send_tech_defaults_missing_contact_and_creates_ticket(monkeypatch):
    """
    A direct dispatch request only ever blocks on location — missing
    contact info (or date/time) is auto-defaulted so the ticket is
    created immediately instead of asking a follow-up question.
    """
    import core.state_machine as state_machine

    session = {
        "current_state": "COMPLETED",
        "vehicle_no": "MH16EF9012",
        "last_location": "Nagpur Bypass",
    }

    monkeypatch.setattr(
        state_machine.llm,
        "extract_tech_dispatch_slots",
        lambda *_args, **_kwargs: {
            "service_location": "Punjabi Bagh",
            "service_date": "2026-07-18",
            "service_time_window": "11:00 AM",
            "contact_person": "",
            "contact_number": "",
        },
    )
    monkeypatch.setattr(
        state_machine.ticket_service,
        "create_ticket",
        lambda session: {
            "ticket_id": "TKT-DIRECT2",
            "engineer_id": "ENG-1",
            "engineer_name": "Engineer One",
            "engineer_phone": "919900000000",
            "existing_ticket": False,
        },
    )

    updated_session, outbound = state_machine.process_message(session, "Send tech by tomorrow at Punjabi Bagh 11 am", "9999999999")

    assert updated_session["current_state"] == "COMPLETED"
    assert updated_session["extracted_service_location"] == "Punjabi Bagh"
    assert updated_session["service_date"] == "2026-07-18"
    assert updated_session["service_time_window"] == "11:00 AM"
    assert updated_session["contact_person"] == "NOT_PROVIDED"
    assert updated_session["contact_number"] == "NOT_PROVIDED"
    assert any("TKT-DIRECT2" in (out.get("text") or "") for out in outbound)


def test_direct_send_tech_location_only_then_creates_ticket_with_defaults(monkeypatch):
    """
    Once location is captured (even if it took a follow-up because it was
    missing from the first message), a direct dispatch request no longer
    asks for date/time/contact — those auto-default and the ticket is
    created immediately.
    """
    import core.state_machine as state_machine

    session = {
        "current_state": "COMPLETED",
        "vehicle_no": "MH16EF9012",
        "last_location": "Nagpur Bypass",
    }

    monkeypatch.setattr(
        state_machine.llm,
        "extract_tech_dispatch_slots",
        lambda *_args, **_kwargs: {
            "service_location": "",
            "service_date": "",
            "service_time_window": "",
            "contact_person": "",
            "contact_number": "",
        },
    )

    updated_session, outbound = state_machine.process_message(session, "Send tech by tomorrow at punjabi Bagh 11 am", "9999999999")

    assert updated_session["current_state"] == "ASK_DIRECT_TECH_LOCATION"
    assert "location" in outbound[0]["text"].lower()

    monkeypatch.setattr(state_machine.llm, "extract_tech_dispatch_slots", lambda *_args, **_kwargs: {
        "service_location": "Punjabi Bagh",
        "service_date": "",
        "service_time_window": "",
        "contact_person": "",
        "contact_number": "",
    })
    monkeypatch.setattr(
        state_machine.ticket_service,
        "create_ticket",
        lambda session: {
            "ticket_id": "TKT-DIRECT3",
            "engineer_id": "ENG-1",
            "engineer_name": "Engineer One",
            "engineer_phone": "919900000000",
            "existing_ticket": False,
        },
    )

    updated_session, outbound = state_machine.process_message(updated_session, "Punjabi Bagh", "9999999999")

    assert updated_session["current_state"] == "COMPLETED"
    assert updated_session["extracted_service_location"] == "Punjabi Bagh"
    assert updated_session["service_date"]
    assert updated_session["service_time_window"]
    assert any("TKT-DIRECT3" in (out.get("text") or "") for out in outbound)


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
