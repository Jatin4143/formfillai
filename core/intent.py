"""
Intent detection, smalltalk checks, and form selection routing.
"""

import logging
import re
from core.db import list_forms, fetch_form_schema, form_meta_by_key

log = logging.getLogger("core.intent")

GREETING_PATTERNS = [
    r"\b(hi|hello|hey|heyy|heya|hola|namaste|good\s*(morning|afternoon|evening|day)|greetings|howdy|sup|yo)\b",
]
GREETING_RE = re.compile("|".join(GREETING_PATTERNS), re.IGNORECASE)

SMALLTALK_PATTERNS = [
    r"\b(how\s*are\s*you|who\s*are\s*you|what\s*can\s*you\s*do|what\s*do\s*you\s*do|help(\s*me)?|what\s*forms?(\s*do\s*you\s*have)?|show(\s*me)?\s*(forms?|options?|menu)|list(\s*of)?\s*forms?|options?|menu)\b",
]
SMALLTALK_RE = re.compile("|".join(SMALLTALK_PATTERNS), re.IGNORECASE)

CANCEL_PATTERNS = [
    r"^(cancel|exit|stop|quit|reset|start\s*over|back|abort)$",
    r"^(cancel\s*(the\s*)?form|stop\s*(the\s*)?form|exit\s*form)$",
]
CANCEL_RE = re.compile("|".join(CANCEL_PATTERNS), re.IGNORECASE)

NEGATIVE_PATTERNS = [
    r"^(no|nope|nah|not\s*really|not\s*now|no\s*thanks|no\s*thank\s*you|nothing|nothing\s*else|that'?s?\s*all|all\s*good|i'?m?\s*good|none)[!.]*$",
]
NEGATIVE_RE = re.compile("|".join(NEGATIVE_PATTERNS), re.IGNORECASE)

BYE_PATTERNS = [
    r"^(bye|goodbye|bye\s*bye|see\s*ya|see\s*you|cya|take\s*care)[!.]*$",
]
BYE_RE = re.compile("|".join(BYE_PATTERNS), re.IGNORECASE)

ACK_PATTERNS = [
    r"^(ok|okay|cool|nice|great|fine|sure|alright|got\s*it|perfect|thanks|thank\s*you|thx|cheers|k)[!.]*$",
]
ACK_RE = re.compile("|".join(ACK_PATTERNS), re.IGNORECASE)

FORM_INTENT_PREFIXES_RE = re.compile(
    r"^(?:(?:hi|hey|hello|please|can\s+you|could\s+you|i\s+want\s+to|i\s+would\s+like\s+to|i\s+need\s+to|help\s+me(?:\s+to|\s+with)?|can\s+you\s+help\s+me(?:\s+to|\s+with)?|with|let(?:\'s|s)?|start|open|fill(?:\s+out|\s+up)?|create|new|raise|lodge|register|submit|do|make)\s+)+(?:a\s+|an\s+|the\s+)?",
    re.IGNORECASE,
)
FORM_INTENT_SUFFIXES_RE = re.compile(
    r"(?:\s+(?:form|please|now|entry))+$",
    re.IGNORECASE,
)

_DOMAIN_WORDS_CACHE = None


def invalidate_domain_words_cache():
    """Clear cached domain words so new forms in DB are reflected immediately."""
    global _DOMAIN_WORDS_CACHE
    _DOMAIN_WORDS_CACHE = None


def get_domain_intent_words() -> set[str]:
    """Dynamically extract significant keywords from all forms, aliases, and fields in the DB."""
    global _DOMAIN_WORDS_CACHE
    if _DOMAIN_WORDS_CACHE is not None:
        return _DOMAIN_WORDS_CACHE

    words = set()
    try:
        forms = list_forms()
        for f in forms:
            for token in re.findall(r"\b[a-z0-9_-]+\b", f["form_name"].lower()):
                if len(token) > 2 and token not in {"form", "add", "the", "for", "new"}:
                    words.add(token)
            for alias in f.get("aliases", []):
                for token in re.findall(r"\b[a-z0-9_-]+\b", str(alias).lower()):
                    if len(token) > 2 and token not in {"form", "add", "the", "for", "new"}:
                        words.add(token)
            schema = fetch_form_schema(f["key"])
            if schema and schema.get("schema"):
                for field in schema["schema"].get("fields", []):
                    for token in re.findall(r"\b[a-z0-9_-]+\b", field["label"].lower()):
                        if len(token) > 2:
                            words.add(token)
                    for token in re.findall(r"\b[a-z0-9_-]+\b", field["name"].lower()):
                        if len(token) > 2:
                            words.add(token)
    except Exception as exc:
        log.warning("Could not compile dynamic domain keywords: %s", exc)

    _DOMAIN_WORDS_CACHE = words
    return _DOMAIN_WORDS_CACHE


def is_negative_or_closing(text: str) -> bool:
    """Return True if the text indicates a closing or negative reply."""
    if not text:
        return False
    cleaned = text.strip().lower()
    return bool(NEGATIVE_RE.match(cleaned) or BYE_RE.match(cleaned))


def is_acknowledgment(text: str) -> bool:
    """Return True if the text is a simple acknowledgment (ok, sure, thanks, etc.)."""
    if not text:
        return False
    cleaned = text.strip().lower()
    return bool(ACK_RE.match(cleaned))


def is_greeting(text: str) -> bool:
    """Return True if text contains a greeting pattern."""
    if not text:
        return False
    cleaned = text.strip().lower()
    return bool(GREETING_RE.search(cleaned))


def get_short_reply(text: str):
    """Return an instant canned response for simple closings/acknowledgments."""
    if not text:
        return None
    cleaned = text.strip().lower()
    if BYE_RE.match(cleaned):
        return "Goodbye! Have a great day."
    if NEGATIVE_RE.match(cleaned):
        return "No problem! Let me know whenever you need anything."
    if ACK_RE.match(cleaned):
        return "You're welcome! Feel free to let me know whenever you'd like to fill a form."
    return None


def is_greeting_or_smalltalk(text: str) -> bool:
    """Return True if the text is purely smalltalk without domain keywords."""
    if not text:
        return False
    cleaned = text.strip().lower()
    if not cleaned:
        return False

    domain_words = get_domain_intent_words()
    words = set(re.findall(r"\b[a-z0-9_-]+\b", cleaned))
    if any(w in domain_words for w in words):
        return False

    if is_negative_or_closing(cleaned) or is_acknowledgment(cleaned):
        return True

    if GREETING_RE.search(cleaned) or SMALLTALK_RE.search(cleaned):
        return True

    return False


def is_cancel_command(text: str) -> bool:
    """Return True if the user asks to cancel the active form."""
    if not text:
        return False
    return bool(CANCEL_RE.match(text.strip().lower()))


def clean_form_selection_text(user_text: str) -> str:
    """Strip common conversational wrappers around form names."""
    text = user_text.strip().lower()
    text = FORM_INTENT_PREFIXES_RE.sub("", text).strip()
    text = FORM_INTENT_SUFFIXES_RE.sub("", text).strip()
    return text.strip(" .,!?:;\"'")


def detect_form_key(user_text: str) -> str | None:
    """Match user text against known forms by exact name, key, or aliases."""
    if is_greeting_or_smalltalk(user_text):
        return None
    text = user_text.strip().lower()
    forms = list_forms()

    for form in forms:
        if text == form["form_name"].lower() or text == form["key"]:
            return form["key"]

    cleaned = clean_form_selection_text(user_text)
    for form in forms:
        if cleaned == form["form_name"].lower() or cleaned == form["key"]:
            return form["key"]

    for form in forms:
        for alias in form["aliases"]:
            pattern = r'\b' + re.escape(alias.lower()) + r'\b'
            if re.search(pattern, text):
                return form["key"]

    return None


def message_is_only_form_name(user_text: str, form: dict) -> bool:
    """True when the message only picks a form and has no field values."""
    raw_clean = user_text.strip().lower().strip(" .,!?:;\"'")
    cleaned = clean_form_selection_text(user_text)
    names = {str(form.get("form_name") or form.get("name") or "").strip().lower(), str(form.get("key") or "").strip().lower()}
    for alias in form.get("aliases", []):
        names.add(str(alias).strip().lower())

    if raw_clean in names or cleaned in names:
        return True

    for n in names:
        if cleaned and (n == cleaned or (n.startswith(cleaned) and len(cleaned) >= 4)):
            return True

    return False


def should_switch_to_form(user_text: str, current_form: dict = None, completed: bool = False) -> str | None:
    """Return form_key when this message should start/switch forms in the current chat."""
    form_key = detect_form_key(user_text)
    if form_key is None:
        return None

    if current_form is None or completed:
        return form_key

    meta = form_meta_by_key(form_key)
    if meta and message_is_only_form_name(user_text, meta):
        return form_key
    return None

