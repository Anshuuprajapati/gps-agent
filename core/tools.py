"""
core/tools.py

The v2 tool-calling engine's fixed action menu. Every tool is a thin
wrapper around an EXISTING service/state_machine function — no backend
behavior is reimplemented here, only re-dispatched, so ticket creation,
driver handoff, and general-question answering behave identically to the
legacy engine.

Each executor has the shape:
    (session: dict, tool_args: dict, message: str, sender_phone: str) -> (session, outbound)
"""
from typing import Literal

from pydantic import BaseModel, ValidationError

from core import state_machine as sm
from core import slot_registry
from services import ticket_service
from prompts.templates import render


class AskForArgs(BaseModel):
    field: str = ""  # advisory only — the executor never trusts this over slot_registry


class AnswerQuestionArgs(BaseModel):
    pass


class TicketInquiryArgs(BaseModel):
    ticket_id: str = ""


class TransferToDriverArgs(BaseModel):
    pass


class DispatchTechnicianArgs(BaseModel):
    service_location: str = ""


class CreateTicketArgs(BaseModel):
    pass


class UpdateTicketStatusArgs(BaseModel):
    new_status: Literal["OPEN", "ASSIGNED", "IN_PROGRESS", "RESOLVED", "CLOSED"]
    note: str = ""


class CloseTicketArgs(BaseModel):
    note: str = ""


class EscalateArgs(BaseModel):
    reason: str = ""


class NoOpArgs(BaseModel):
    pass


TOOL_ARGS_MODELS = {
    "ask_for": AskForArgs,
    "answer_question": AnswerQuestionArgs,
    "ticket_inquiry": TicketInquiryArgs,
    "transfer_to_driver": TransferToDriverArgs,
    "dispatch_technician": DispatchTechnicianArgs,
    "create_ticket": CreateTicketArgs,
    "update_ticket_status": UpdateTicketStatusArgs,
    "close_ticket": CloseTicketArgs,
    "escalate": EscalateArgs,
    "no_op": NoOpArgs,
}

# Tools that must never fire the instant the LLM picks them — the engine
# stashes the call and asks for an explicit yes/no first (see
# core/agent_engine.py's confirm-gate). `dispatch_technician` is
# deliberately NOT gated: an earlier fix this session made "send a tech
# now" a zero-friction, no-further-questions flow at the user's explicit
# request ("nothing ask, direct ticket create") — gating it here would
# silently regress that.
CONFIRM_BEFORE = {"create_ticket", "update_ticket_status", "close_ticket"}


def validate_tool_call(tool_name: str, tool_args: dict) -> dict:
    """Returns a validated args dict, or raises ValueError (unknown tool
    name or schema mismatch) for the caller to retry/fall back on."""
    model_cls = TOOL_ARGS_MODELS.get(tool_name)
    if model_cls is None:
        raise ValueError(f"Unknown tool_name: {tool_name!r}")
    try:
        validated = model_cls.model_validate(tool_args or {})
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
    return validated.model_dump()


def _tool_ask_for(session, tool_args, message, sender_phone):
    # Never trust the LLM's suggested field over the deterministic ground
    # truth — slot_registry is what actually decides what's still missing.
    state = slot_registry.next_missing_slot(session)
    session["current_state"] = state
    prompt = slot_registry.prompt_for_booking_field(session, state)
    return session, [sm._msg(sender_phone, prompt)]


def _tool_answer_question(session, tool_args, message, sender_phone):
    return sm._handle_general_question(session, message, sender_phone)


def _tool_ticket_inquiry(session, tool_args, message, sender_phone):
    ticket_id = (tool_args.get("ticket_id") or "").strip()
    return sm._handle_ticket_inquiry(session, message, sender_phone, ticket_id)


def _tool_transfer_to_driver(session, tool_args, message, sender_phone):
    return sm._start_driver_handoff(session, sender_phone)


def _tool_dispatch_technician(session, tool_args, message, sender_phone):
    return sm._handle_direct_tech_request(session, message, sender_phone)


def _tool_create_ticket(session, tool_args, message, sender_phone):
    return sm._create_and_confirm_ticket_directly(session, sender_phone)


def _tool_update_ticket_status(session, tool_args, message, sender_phone):
    ticket_id = session.get("ticket_id", "")
    if not ticket_id:
        return session, [sm._msg(sender_phone, "Aapka koi ticket nahi mila is number par.")]
    try:
        ticket = ticket_service.update_ticket_status(
            ticket_id, tool_args.get("new_status", ""), tool_args.get("note", "")
        )
    except ValueError as exc:
        return session, [sm._msg(sender_phone, f"Yeh update nahi ho saka: {exc}")]
    return session, [sm._msg(sender_phone, f"Ticket {ticket['ticket_id']} ab {ticket['status']} hai.")]


def _tool_close_ticket(session, tool_args, message, sender_phone):
    ticket_id = session.get("ticket_id", "")
    if not ticket_id:
        return session, [sm._msg(sender_phone, "Aapka koi ticket nahi mila is number par.")]
    try:
        ticket = ticket_service.close_ticket(ticket_id, tool_args.get("note", ""))
    except ValueError as exc:
        return session, [sm._msg(sender_phone, f"Yeh band nahi ho saka: {exc}")]
    return session, [sm._msg(sender_phone, f"Ticket {ticket['ticket_id']} band kar diya gaya hai. Dhanyavaad!")]


def _tool_escalate(session, tool_args, message, sender_phone):
    # Stub — real escalation policy (supervisor routing, SLA timers) is
    # explicitly out of scope for this pass; this just gives the LLM a
    # safe, honest place to land instead of forcing a wrong tool choice.
    return session, [sm._msg(sender_phone, "Iske liye hum jald hi aapko ek supervisor se connect karenge.")]


def _tool_no_op(session, tool_args, message, sender_phone):
    return session, [sm._msg(sender_phone, render("GENERIC_ACK"))]


TOOL_EXECUTORS = {
    "ask_for": _tool_ask_for,
    "answer_question": _tool_answer_question,
    "ticket_inquiry": _tool_ticket_inquiry,
    "transfer_to_driver": _tool_transfer_to_driver,
    "dispatch_technician": _tool_dispatch_technician,
    "create_ticket": _tool_create_ticket,
    "update_ticket_status": _tool_update_ticket_status,
    "close_ticket": _tool_close_ticket,
    "escalate": _tool_escalate,
    "no_op": _tool_no_op,
}
