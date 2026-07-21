"""
core/router.py

The strangler-fig seam between the legacy per-state state machine
(core/state_machine.py, untouched) and the new v2 tool-calling engine
(core/agent_engine.py). A session is pinned to whichever engine it first
gets routed through — `engine_version` is read once here and never
re-evaluated mid-conversation, so a case can never switch engines partway
through, and rolling the default back is a flag flip, not a code revert.

whatsapp/webhook.py and voice/webhook.py call this instead of
state_machine.process_message directly.
"""
from config import settings
from core import state_machine
from core import agent_engine


def process_message(session: dict, message: str, sender_phone: str):
    if not session.get("engine_version"):
        session["engine_version"] = settings.AGENT_ENGINE_DEFAULT_FOR_NEW

    if session["engine_version"] == "v2":
        return agent_engine.process_message_v2(session, message, sender_phone)
    return state_machine.process_message(session, message, sender_phone)
