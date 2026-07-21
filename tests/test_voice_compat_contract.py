"""
tests/test_voice_compat_contract.py

voice/webhook.py maps DTMF digit presses (and speech hints) to LITERAL
text strings keyed by state name — e.g. pressing "1" in ASK_VEHICLE_STATUS
produces the literal text "Workshop me", fed into process_message() just
like WhatsApp free text. The v2 tool-calling engine (core/agent_engine.py)
reuses state_machine.py's own private handlers/helpers to write
session["current_state"], so it can only ever produce state names from
the same vocabulary the legacy engine already uses. This test is the
static guarantee that voice's menu/hint dictionaries never reference a
state name that doesn't actually exist in that vocabulary — if it did,
a caller pressing that digit would silently fall through to handle_start
(HANDLERS' fallback default) instead of continuing their case.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import state_machine
from voice import webhook as voice_webhook

# ASK_DIRECT_TECH_LOCATION has no HANDLERS entry — it only works because
# process_message() special-cases it before the HANDLERS.get(...) lookup
# (both engines do this). It's a legitimate reachable state, just not a
# dict key, so it's added to the allowed set explicitly.
_KNOWN_STATES = set(state_machine.HANDLERS.keys()) | {"ASK_DIRECT_TECH_LOCATION"}


def test_state_hints_only_reference_known_states():
    unknown = set(voice_webhook._STATE_HINTS.keys()) - _KNOWN_STATES
    assert not unknown, f"_STATE_HINTS references states that don't exist: {unknown}"


def test_state_menu_only_reference_known_states():
    unknown = set(voice_webhook._STATE_MENU.keys()) - _KNOWN_STATES
    assert not unknown, f"_STATE_MENU references states that don't exist: {unknown}"


def test_digit_map_is_derived_from_state_menu_and_stays_in_sync():
    # _DIGIT_MAP is built directly from _STATE_MENU at import time — this
    # just guards against someone splitting them apart later and letting
    # them drift.
    for state, menu in voice_webhook._STATE_MENU.items():
        assert voice_webhook._DIGIT_MAP[state] == {digit: label for digit, label in menu}


def test_every_known_state_is_reachable_from_handlers_or_special_case():
    """Sanity check the other direction too: every state voice can prompt
    a menu for must be something process_message can actually dispatch,
    whichever engine is running."""
    for state in voice_webhook._STATE_MENU:
        assert state in _KNOWN_STATES
