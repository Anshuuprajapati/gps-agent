"""
voice/speech_format.py

The bot's message templates (prompts/templates.py) are shared with
WhatsApp, so they're full of emojis, "1) 2) 3)" numbered lists, newlines,
and typing-oriented instructions ("Reply YES ya NO", "Done likh dijiye").
All of that reads badly out loud through Twilio's <Say>.

This module does NOT touch templates.py (WhatsApp still needs those as-is)
— it's a voice-only cleanup pass applied to text right before it's spoken.

Public entrypoint: to_speech(text) -> str
"""

import re

# --------------------------------------------------------------- emojis --

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # symbols & pictographs, supplemental symbols
    "\U00002600-\U000027BF"   # misc symbols & dingbats (☎, ✅, ✔ etc.)
    "\U0001F1E6-\U0001F1FF"   # regional indicators
    "\U00002190-\U000021FF"   # arrows
    "\U00002B00-\U00002BFF"   # misc symbols and arrows
    "\U0001F000-\U0001F0FF"
    "\uFE0F"                  # variation selector used after many emoji
    "]+",
    flags=re.UNICODE,
)


def _strip_emojis(text: str) -> str:
    return _EMOJI_RE.sub("", text)


# ------------------------------------------------- digit-by-digit numbers --
# Twilio's TTS reads a bare number like "9876543210" as one huge number
# ("nine billion, eight hundred..."). Phone numbers, vehicle plates, and
# ticket/engineer IDs need to be read one character at a time instead
# ("nine eight seven six ...", "M H one two A B ...").

_TICKET_ID_RE = re.compile(r"\bTKT-[A-Za-z0-9]{4,12}\b")
_ALNUM_ID_RE = re.compile(
    r"\b(?=[A-Za-z0-9]{4,12}\b)(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{4,12}\b"
)
_LONG_DIGIT_RUN_RE = re.compile(r"\b\d{7,}\b")


def _spell_out(token: str) -> str:
    return " ".join(list(token.replace("-", "")))


def _spell_numbers(text: str) -> str:
    text = _TICKET_ID_RE.sub(lambda m: _spell_out(m.group(0)), text)
    text = _ALNUM_ID_RE.sub(lambda m: _spell_out(m.group(0)), text)
    text = _LONG_DIGIT_RUN_RE.sub(lambda m: _spell_out(m.group(0)), text)
    return text


# --------------------------------------------------- numbered lists -> speech

_LIST_ITEM_RE = re.compile(r"^\s*\d+[\).]\s*(.+?)\s*$")


def _flatten_lists_and_lines(text: str) -> str:
    """
    Turns:
        "1) Workshop me\n2) Accident hua hai\n3) Vehicle chal rahi hai"
    into:
        "Workshop me. Accident hua hai. Vehicle chal rahi hai."
    and generally collapses newlines (chat line-breaks) into spoken text
    instead of leaving them as literal breaks.

    Deliberately NOT joined with "ya" (or) — some numbered lists in the
    templates are alternatives to choose between (vehicle status options)
    but others are sequential steps to follow (battery/power help steps),
    and "ya" would wrongly turn "do step 1, then step 2" into "do step 1
    OR step 2". Plain separate sentences read correctly either way.
    """
    out_chunks = []
    list_buffer = []

    def flush_list():
        if list_buffer:
            for item in list_buffer:
                item = item.rstrip(".")
                out_chunks.append(item + ".")
            list_buffer.clear()

    for line in text.split("\n"):
        m = _LIST_ITEM_RE.match(line)
        if m:
            list_buffer.append(m.group(1))
            continue
        flush_list()
        stripped = line.strip()
        if stripped:
            out_chunks.append(stripped)
    flush_list()
    return " ".join(out_chunks)


# ------------------------------------------------ typing-speak -> talking-speak

# Applied as an ordered list (not a dict) so longer/more specific phrases
# are replaced before their shorter substrings.
_PHRASE_REPLACEMENTS = [
    (r'"Done"\s*likh(?:\s*kar)?\s*(?:bhejein|dijiye|bataiye)', "'Done' bol dijiye"),
    (r"likh kar bata(?:iye|yein)", "bol kar bataiye"),
    (r"likh kar bhejein", "bol dijiye"),
    (r"likh dijiye", "bol dijiye"),
    (r"\blikhein\b", "boliye"),
    (r"\blikh kar\b", "bol kar"),
    (r"reply\s+YES\s+ya\s+NO", "Haan ya Nahi boliye", ),
    (r"reply\s+YES", "Haan boliye"),
    (r"\bReply\b", "Bataiye"),
    (r"\bbhejein\b", "bataiye"),
    (r"\bbhejiye\b", "bataiye"),
    (r"\bshare kijiye\b", "bataiye"),
    (r"\bshare karenge\b", "batayenge"),
    (r"\btype\b", "boliye"),
]
_COMPILED_PHRASE_REPLACEMENTS = [
    (re.compile(pat, re.IGNORECASE), repl) for pat, repl in _PHRASE_REPLACEMENTS
]


def _talkify_instructions(text: str) -> str:
    for pattern, repl in _COMPILED_PHRASE_REPLACEMENTS:
        text = pattern.sub(repl, text)
    return text


def _strip_chat_punctuation(text: str) -> str:
    # smart & straight quotes, markdown emphasis characters — meaningless
    # (or actively read aloud as "quote"/"asterisk") once spoken
    text = text.replace('"', "").replace("'", "")
    text = text.replace("\u201c", "").replace("\u201d", "")
    text = text.replace("*", "").replace("_", "")
    return text


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# ------------------------------------------------------------- public API --

def to_speech(text: str) -> str:
    """Run the full chat-text -> spoken-text pipeline, in order."""
    if not text:
        return text
    text = _strip_emojis(text)
    text = _spell_numbers(text)
    text = _flatten_lists_and_lines(text)
    text = _talkify_instructions(text)
    text = _strip_chat_punctuation(text)
    text = _collapse_whitespace(text)
    return text