"""
tests/test_ticket_lifecycle.py

Pure unit tests for the ticket status lifecycle added to
services/ticket_service.py this session (update_ticket_status/close_ticket)
- no engine dependency, no LLM calls involved.
"""
import os
import sys
import csv

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from services import ticket_service


@pytest.fixture
def tmp_tickets_csv(tmp_path, monkeypatch):
    tickets_csv = tmp_path / "tickets.csv"
    monkeypatch.setattr(settings, "TICKETS_CSV", str(tickets_csv))
    return tickets_csv


def _make_ticket(vehicle_no="MH12ZZ9999", **overrides):
    session = {
        "vehicle_no": vehicle_no,
        "root_cause": "MAIN_POWER",
        "current_location": "Pune Bypass",
        "extracted_service_location": "Pune",
        "service_date": "2026-07-25",
        "service_time_window": "05:00 PM",
        "contact_person": "Raju",
        "contact_number": "9876500000",
        "vehicle_state": "RUNNING",
    }
    session.update(overrides)
    return ticket_service.create_ticket(session)


class TestCreateTicketStatus:
    def test_new_ticket_with_engineer_starts_assigned(self, tmp_tickets_csv):
        ticket = _make_ticket()
        assert ticket["status"] == "ASSIGNED"
        assert ticket["status_updated_at"]
        assert ticket["status_note"] == ""

    def test_gps_damaged_ticket_with_no_engineer_starts_open(self, tmp_tickets_csv):
        ticket = _make_ticket(vehicle_state="GPS_DAMAGED")
        assert ticket["engineer_id"] == ""
        assert ticket["status"] == "OPEN"


class TestUpdateTicketStatus:
    def test_assigned_to_in_progress_is_legal(self, tmp_tickets_csv):
        ticket = _make_ticket()
        updated = ticket_service.update_ticket_status(ticket["ticket_id"], "IN_PROGRESS")
        assert updated["status"] == "IN_PROGRESS"

    def test_in_progress_to_resolved_to_closed(self, tmp_tickets_csv):
        ticket = _make_ticket()
        ticket_service.update_ticket_status(ticket["ticket_id"], "IN_PROGRESS")
        ticket_service.update_ticket_status(ticket["ticket_id"], "RESOLVED", note="GPS repaired")
        closed = ticket_service.update_ticket_status(ticket["ticket_id"], "CLOSED")
        assert closed["status"] == "CLOSED"

    def test_close_ticket_helper_closes_from_assigned(self, tmp_tickets_csv):
        ticket = _make_ticket()
        closed = ticket_service.close_ticket(ticket["ticket_id"], note="customer cancelled")
        assert closed["status"] == "CLOSED"
        assert closed["status_note"] == "customer cancelled"

    def test_open_ticket_can_be_closed_directly(self, tmp_tickets_csv):
        ticket = _make_ticket(vehicle_state="GPS_DAMAGED")
        closed = ticket_service.close_ticket(ticket["ticket_id"])
        assert closed["status"] == "CLOSED"

    def test_open_cannot_jump_straight_to_resolved(self, tmp_tickets_csv):
        ticket = _make_ticket(vehicle_state="GPS_DAMAGED")
        with pytest.raises(ValueError):
            ticket_service.update_ticket_status(ticket["ticket_id"], "RESOLVED")

    def test_closed_ticket_cannot_be_reopened(self, tmp_tickets_csv):
        ticket = _make_ticket()
        ticket_service.close_ticket(ticket["ticket_id"])
        with pytest.raises(ValueError):
            ticket_service.update_ticket_status(ticket["ticket_id"], "ASSIGNED")

    def test_unknown_ticket_id_raises(self, tmp_tickets_csv):
        with pytest.raises(ValueError):
            ticket_service.update_ticket_status("TKT-DOESNOTEXIST", "CLOSED")

    def test_status_update_persists_to_csv(self, tmp_tickets_csv):
        ticket = _make_ticket()
        ticket_service.update_ticket_status(ticket["ticket_id"], "IN_PROGRESS")

        with open(tmp_tickets_csv) as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["status"] == "IN_PROGRESS"
        assert rows[0]["ticket_id"] == ticket["ticket_id"]
