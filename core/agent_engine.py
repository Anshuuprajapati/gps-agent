"""
core/agent_engine.py

The v2 tool-calling engine — a per-turn reasoning call (llm_handler.
decide_next_action) picks one tool from core/tools.py's fixed menu,
instead of core/state_machine.py's rigid per-state HANDLERS dispatch.
The tool implementations themselves are the SAME deterministic functions
the legacy engine already uses (imported, not reimplemented), so ticket
creation, driver handoff, and general-question answering behave
identically either way.

Routed to via core/router.py, alongside (not instead of) the untouched
legacy engine, per a per-session `engine_version` flag pinned once at
session creation.

process_message_v2(session, message, sender_phone) -> (session, outbound)
mirrors state_machine.process_message's exact external contract.
"""
import json

from core import llm_handler as llm
from core import state_machine as sm
from core import tools
from prompts.templates import render

_MAX_TOOL_RETRIES = 1


def _confirmation_prompt_for(tool_name: str, tool_args: dict, session: dict) -> str:
    if tool_name == "create_ticket":
        return sm._build_booking_summary(session) + "\n\nKya main ticket book kar doon? (haan/nahi)"
    if tool_name == "update_ticket_status":
        return f"Ticket {session.get('ticket_id', '')} ko '{tool_args.get('new_status', '')}' mark kar doon? (haan/nahi)"
    if tool_name == "close_ticket":
        return f"Ticket {session.get('ticket_id', '')} band kar doon? (haan/nahi)"
    return "Kya main aage badhoon? (haan/nahi)"


def _resolve_pending_confirmation(session: dict, message: str, sender_phone: str):
    """
    If a gated tool call is awaiting yes/no from last turn, resolve it
    before anything else this turn — including before the reasoning call,
    so a stray "haan"/"nahi" is never misinterpreted as a fresh intent.
    Returns (session, outbound) if this turn is fully handled, else None.
    """
    pending_raw = session.get("pending_action_json") or ""
    if not pending_raw:
        return None

    try:
        pending = json.loads(pending_raw)
    except (ValueError, TypeError):
        session["pending_action_json"] = ""
        return None

    conversation_context = sm.build_conversation_context(session)
    decision = llm.classify_yes_no(session.get("current_state", "START"), message, conversation_context)

    if decision == "YES":
        session["pending_action_json"] = ""
        tool_name = pending.get("tool_name", "")
        tool_args = pending.get("tool_args", {})
        executor = tools.TOOL_EXECUTORS.get(tool_name)
        if executor is None:
            # Shouldn't happen — tool_name was already validated before
            # being stashed — but never crash a real conversation over it.
            return session, [sm._msg(sender_phone, "Kuch gadbad ho gayi, dobara try karein.")]
        return executor(session, tool_args, message, sender_phone)

    if decision == "NO":
        session["pending_action_json"] = ""
        return session, [sm._msg(sender_phone, "Thik hai, cancel kar diya. Aap kya update karna chahenge?")]

    # UNCLEAR — re-ask the same yes/no rather than guessing either way.
    prompt = _confirmation_prompt_for(pending.get("tool_name", ""), pending.get("tool_args", {}), session)
    return session, [sm._msg(sender_phone, prompt)]


def _decide_validated_tool(session: dict, message: str, conversation_context: str):
    """
    Calls decide_next_action, validates the result against core/tools.py's
    schemas, and retries once with the validation error fed back on
    failure. Falls back to the deterministic ask_for tool (never a crash,
    never a stall) if the LLM still can't produce something valid.
    """
    state = session.get("current_state", "START")
    context = conversation_context

    for attempt in range(_MAX_TOOL_RETRIES + 1):
        decision = llm.decide_next_action(state, conversation_context, message, context)
        tool_name = decision.get("tool_name", "")
        tool_args_raw = decision.get("tool_args", {}) or {}
        try:
            tool_args = tools.validate_tool_call(tool_name, tool_args_raw)
            return tool_name, tool_args
        except ValueError as exc:
            context = (
                conversation_context
                + f"\n\n(Your previous tool choice was invalid: {exc}. "
                "Pick a valid tool_name from the menu with matching tool_args.)"
            )

    return "ask_for", {}


def process_message_v2(session: dict, message: str, sender_phone: str):
    pending_result = _resolve_pending_confirmation(session, message, sender_phone)
    if pending_result is not None:
        return pending_result

    # Cheap deterministic pre-checks, reused verbatim from
    # state_machine.py in the same order it applies them today, so
    # cross-cutting behavior (ticket-ID lookup, driver handoff, direct
    # tech dispatch) doesn't drift between the two engines.
    ticket_id_in_message = sm._extract_ticket_id(message)
    if ticket_id_in_message:
        return sm._handle_ticket_inquiry(session, message, sender_phone, ticket_id_in_message)

    if sm._is_driver_change_request(message):
        return sm._handle_driver_change_request(session, sender_phone)

    if session.get("current_state") == "ASK_DIRECT_TECH_LOCATION" or sm._is_direct_tech_request(message):
        return sm._handle_direct_tech_request(session, message, sender_phone)

    if not sm._normalize_payload(message) and sm._is_generic_acknowledgment(message):
        return session, [sm._msg(sender_phone, render("GENERIC_ACK"))]

    if (
        session.get("handler", "OWNER") == "OWNER"
        and session.get("current_state") not in ("COMPLETED", "DRIVER_CONFIRM", "ASK_NEW_DRIVER", "ASK_HANDLER")
        and sm._is_driver_request(message)
    ):
        return sm._start_driver_handoff(session, sender_phone)

    # One consolidated reasoning call decides which backend tool to run.
    conversation_context = sm.build_conversation_context(session)
    tool_name, tool_args = _decide_validated_tool(session, message, conversation_context)

    if tool_name in tools.CONFIRM_BEFORE:
        session["pending_action_json"] = json.dumps({"tool_name": tool_name, "tool_args": tool_args})
        session["current_state"] = "CONFIRM_SUMMARY"
        prompt = _confirmation_prompt_for(tool_name, tool_args, session)
        return session, [sm._msg(sender_phone, prompt)]

    executor = tools.TOOL_EXECUTORS[tool_name]
    return executor(session, tool_args, message, sender_phone)
