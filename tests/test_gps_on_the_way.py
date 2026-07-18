"""Test for GPS not working but vehicle on the way scenario."""


def test_gps_not_working_but_vehicle_on_the_way_with_date(monkeypatch):
    """When GPS doesn't work but vehicle is on the way and user provides expected date."""
    import core.state_machine as state_machine
    from core import llm_handler

    session = {
        "current_state": "ASK_GPS_REPAIR_CONFIRMATION",
        "vehicle_no": "MH16EF9012",
        "extracted_service_location": "punjabi Bagh",
        "service_time_window": "11:00 AM",
        "contact_number": "8290323758",
        "contact_person": "Sarvesh Swami",
        "current_location": "Nagpur",
    }

    monkeypatch.setattr(
        llm_handler,
        "classify_yes_no",
        lambda *_args, **_kwargs: "NO"
    )
    monkeypatch.setattr(
        llm_handler,
        "extract_date",
        lambda *_args, **_kwargs: "2026-07-19"
    )

    updated_session, outbound = state_machine.handle_ask_gps_repair_confirmation(
        session, 
        "Gps not working h but abhi vehicle on the way h jab aayegi tb inform krunga 19 ko", 
        "9999999999"
    )

    assert updated_session["current_state"] == "COMPLETED"
    assert updated_session["service_date"] == "2026-07-19"
    assert updated_session["extracted_appointment_date"] == "2026-07-19"
    response_text = outbound[0]["text"]
    assert "Thik hai" in response_text
    assert "2026-07-19" in response_text
    assert "Sarvesh Swami" in response_text


def test_gps_not_working_vehicle_on_the_way_without_date(monkeypatch):
    """When GPS doesn't work but vehicle is on the way without expected date."""
    import core.state_machine as state_machine
    from core import llm_handler

    session = {
        "current_state": "ASK_GPS_REPAIR_CONFIRMATION",
        "vehicle_no": "MH16EF9012",
        "extracted_service_location": "punjabi Bagh",
        "service_time_window": "11:00 AM",
        "contact_number": "8290323758",
        "contact_person": "Sarvesh Swami",
        "current_location": "Nagpur",
    }

    monkeypatch.setattr(
        llm_handler,
        "classify_yes_no",
        lambda *_args, **_kwargs: "NO"
    )
    monkeypatch.setattr(
        llm_handler,
        "extract_date",
        lambda *_args, **_kwargs: None  # No date provided
    )

    updated_session, outbound = state_machine.handle_ask_gps_repair_confirmation(
        session, 
        "Gps not working h but abhi vehicle on the way h jab aayegi tb inform krunga", 
        "9999999999"
    )

    assert updated_session["current_state"] == "ASK_GPS_UPDATE_DATE"
    response_text = outbound[0]["text"].lower()
    assert "phir kab update" in response_text or "expected date" in response_text
    assert "date" in response_text


def test_ask_gps_update_date_with_valid_date(monkeypatch):
    """When user provides date in ASK_GPS_UPDATE_DATE state."""
    import core.state_machine as state_machine
    from core import llm_handler

    session = {
        "current_state": "ASK_GPS_UPDATE_DATE",
        "vehicle_no": "MH16EF9012",
        "extracted_service_location": "punjabi Bagh",
        "service_time_window": "11:00 AM",
        "contact_number": "8290323758",
        "contact_person": "Sarvesh Swami",
        "current_location": "Nagpur",
    }

    monkeypatch.setattr(
        llm_handler,
        "extract_date",
        lambda *_args, **_kwargs: "2026-07-20"
    )

    updated_session, outbound = state_machine.handle_ask_gps_update_date(
        session, 
        "2026-07-20 3 pm", 
        "9999999999"
    )

    assert updated_session["current_state"] == "COMPLETED"
    assert updated_session["service_date"] == "2026-07-20"
    assert updated_session["extracted_appointment_date"] == "2026-07-20"
    response_text = outbound[0]["text"]
    assert "Booking" in response_text
    assert "2026-07-20" in response_text


def test_ask_gps_update_date_without_valid_date(monkeypatch):
    """When user doesn't provide valid date in ASK_GPS_UPDATE_DATE state."""
    import core.state_machine as state_machine
    from core import llm_handler

    session = {
        "current_state": "ASK_GPS_UPDATE_DATE",
        "vehicle_no": "MH16EF9012",
    }

    monkeypatch.setattr(
        llm_handler,
        "extract_date",
        lambda *_args, **_kwargs: None  # No valid date
    )

    updated_session, outbound = state_machine.handle_ask_gps_update_date(
        session, 
        "kal", 
        "9999999999"
    )

    assert updated_session["current_state"] == "ASK_GPS_UPDATE_DATE"
    response_text = outbound[0]["text"].lower()
    assert "format" in response_text or "date" in response_text
