"""
core/slot_registry.py

The v2 tool-calling engine's deterministic "what do we still need to know"
ground truth. Rather than re-encoding a second, parallel table of booking
fields (which would drift from the original over time), this delegates
straight to the private helpers `core/state_machine.py` already has for
exactly this job — `_next_missing_booking_state`/`_prompt_for_booking_state`
and `_next_missing_direct_tech_state`/`_prompt_for_direct_tech_state`. Those
functions are pure and side-effect-light (aside from stashing
`pending_quick_date`), so importing them here is reuse, not duplication.

Used two ways by `core/agent_engine.py`:
  1. The `ask_for` tool's argument names a field/state to prompt for.
  2. If the LLM's chosen tool fails schema validation twice in a row, the
     engine falls back to whatever this module says is missing, so it can
     never silently stall even under total LLM failure.
"""
from core import state_machine as _sm


def next_missing_booking_field(session: dict) -> str:
    """Returns the next booking-flow state name still needed, or
    'CONFIRM_SUMMARY' once every field is filled."""
    return _sm._next_missing_booking_state(session)


def prompt_for_booking_field(session: dict, state: str) -> str:
    return _sm._prompt_for_booking_state(session, state)


def next_missing_dispatch_field(session: dict) -> str:
    """Returns 'ASK_DIRECT_TECH_LOCATION' if a direct dispatch request is
    still missing its one hard-blocking slot (location), else ''."""
    return _sm._next_missing_direct_tech_state(session)


def prompt_for_dispatch_field(session: dict, state: str) -> str:
    return _sm._prompt_for_direct_tech_state(session, state)


def next_missing_slot(session: dict) -> str:
    """
    The general-purpose fallback target for the `ask_for` tool: what's the
    single next thing worth asking about, given everything already known.
    Delegates to the booking-flow logic, which covers the common case
    (location -> destination -> city confirmation -> date -> time ->
    contact). Direct-tech-dispatch's narrower single-field flow is handled
    separately in agent_engine's dedicated pre-check, since that path
    never reaches the general reasoning call in the first place.
    """
    return next_missing_booking_field(session)
