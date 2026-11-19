"""
Form chat application.

Initializes and uses SQLite DB (forms.db) for:
- Form schemas (forms, form_fields)
- Form submissions (form_submissions)
- Chat session histories (chats)

Flow:
1. model returns which form and any fields already stated
2. fetch schema (GET)
3. keep values that pass validation
4. ask → validate → next field for whatever is still missing
5. submit answers (POST)
"""

import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import ollama
import streamlit as st
import multi_form_planner as mfp

from config import (
    MODEL,
    OLLAMA_BASE_URL,
    DB_PATH,
    APP_NAME,
    APP_TAGLINE,
    ollama_client,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("form_chat")
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False

GENERIC_SYSTEM_PROMPT = """
You are a courteous, clear, and professional AI form-filling assistant.

Your task: Ask ONE natural, polite, and professional question to collect the specified field from the user for the active form.

Guidelines:
1. Adapt your tone naturally to the context of the form and the specific field (e.g. professional for work/applications, helpful for requests, empathetic for issues/complaints).
2. Do NOT use dry, mechanical templates like "What is the [field]?" or "Please enter [field]". Ask conversational, natural questions.
3. If options are available, present them smoothly and clearly.
4. If the field is optional, politely let the user know they can reply with "skip" if not applicable or to leave it blank.
5. Ask ONLY about the specified field. Keep it to one clear sentence.
6. Return ONLY valid JSON: {"question": "Your question here"}

Examples:

Form: Visitor Entry, Field: visitor_name ("Visitor Name", required)
{"question": "Could you please share the visitor's full name?"}

Form: Job Application, Field: experience_years ("Years of Experience", required)
{"question": "May I know how many years of relevant experience you have?"}

Form: Feedback, Field: rating ("Rating", options: ["Excellent", "Good", "Fair", "Poor"])
{"question": "How would you rate your overall experience — Excellent, Good, Fair, or Poor?"}

Form: Service Request, Field: notes ("Additional Notes", optional)
{"question": "Is there anything else you would like our team to know? (You may reply 'skip' to leave this blank)."}
"""


EXTRACT_SYSTEM_PROMPT = """
You are an expert form data extractor.
Your task is to extract field values from the user's message based on the provided schema.

Rules:
1. Extract ONLY facts explicitly mentioned or stated by the user in their message.
2. For select fields, pick the matching option string from the given options list.
3. If a field is not mentioned or unknown, set its value to null.
4. Never invent, assume, or guess values.
5. Output ONLY valid JSON in format: {"fields": {"<field_name>": "<value or null>"}}
"""

CLASSIFY_SYSTEM_PROMPT = """
You identify which form the user wants to fill and extract any explicitly mentioned fields.

Rules:
1. "form" must be the matching form key from the catalog, or null if the message is a greeting or unrelated.
2. "fields" must contain only field values explicitly mentioned in the user message.
3. For select fields, pick the matching option from the options list.
4. If a field is not mentioned, use null.
5. Output ONLY valid JSON in format:
{"form": "<form_key or null>", "fields": {"<field_name>": "<value or null>"}}
"""

HELP_SYSTEM_PROMPT = f"""
You are {APP_NAME}, a friendly and helpful AI form-filling assistant.

RULES:
1. Keep replies very brief (1 to 2 short sentences maximum).
2. Plain text only. Never write essays, bulleted lists, long greetings, or unsolicited explanations.
3. If the user declined, said 'no', or is closing the conversation, reply with a short polite closing (e.g. "No problem! Let me know if you need anything later.").
4. If asking for help, briefly state that you can help fill forms and prompt them to pick a form or provide details.
"""

INDIAN_PLATE_PATTERN = r"^(?:[A-Z]{2}[ -]?\d{1,2}[ -]?[A-Z]{1,3}[ -]?\d{1,4}|\d{2}[ -]?BH[ -]?\d{4}[ -]?[A-HJ-NP-Z]{1,2})$"
INDIAN_PLATE_RE = re.compile(INDIAN_PLATE_PATTERN, re.IGNORECASE)
INDIAN_PLATE_EXTRACT_RE = re.compile(
    r"\b([A-Z]{2}[ -]?\d{1,2}[ -]?[A-Z]{1,3}[ -]?\d{1,4}|\d{2}[ -]?BH[ -]?\d{4}[ -]?[A-HJ-NP-Z]{1,2})\b",
    re.IGNORECASE,
)

EMAIL_RE = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
PHONE_RE = re.compile(r"^\+?[0-9\s\-()]{7,20}$")
NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def normalize_plate(text: str) -> str:
    """Normalize and format Indian vehicle number plate to uppercase."""
    return re.sub(r"\s+", " ", text.strip().upper())


def is_valid_indian_plate(text: str) -> bool:
    """Validate vehicle registration plate against Indian number plate format."""
    if not text:
        return False
    return bool(INDIAN_PLATE_RE.match(text.strip()))


def normalize_date(text: str) -> str | None:
    """Normalize date text (e.g. '5 dec', 'tomorrow', '20/11/2026') to standard 'YYYY-MM-DD'."""
    if not text or not text.strip():
        return None
    s = text.strip().lower()
    today = date.today()
    if s == "today":
        return today.isoformat()
    if s == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    if not re.search(r"\d", s):
        return None
    try:
        from dateutil import parser as dt_parser
        is_year_first = bool(re.match(r"^\d{4}[-/.]", s))
        dt = dt_parser.parse(s, yearfirst=is_year_first, dayfirst=not is_year_first)
        return dt.date().isoformat()
    except Exception:
        return None


def is_valid_date(text: str) -> bool:
    """Check if the provided text is a valid date (rejects random text like 'hcc')."""
    return normalize_date(text) is not None


NO_VEHICLE_PATTERNS = [
    r"\bno\s*(car|vehicle|vehical|bike|motorcycle|scooter|two\s*wheeler|four\s*wheeler|plate|cab|taxi|auto)\b",
    r"\bwithout\s*(a\s*|any\s*)?(car|vehicle|vehical|bike|motorcycle|scooter|two\s*wheeler|four\s*wheeler|plate)\b",
    r"\b(on\s*foot|by\s*foot)\b",
    r"\b(by\s*walk(ing)?|walk(ing)?|pedestrian)\b",
    r"\b(by\s*(cycle|bicycle)|cycling|bicycle|cycle)\b",
    r"\b(public\s*transport|metro|bus|train|auto\s*rickshaw)\b",
    r"^(no|none|na|n/a|nil|skip)$",
]
NO_VEHICLE_RE = re.compile("|".join(NO_VEHICLE_PATTERNS), re.IGNORECASE)

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


def is_negative_or_closing(text: str) -> bool:
    if not text:
        return False
    cleaned = text.strip().lower()
    return bool(NEGATIVE_RE.match(cleaned) or BYE_RE.match(cleaned))


def is_acknowledgment(text: str) -> bool:
    if not text:
        return False
    cleaned = text.strip().lower()
    return bool(ACK_RE.match(cleaned))


def is_greeting(text: str) -> bool:
    if not text:
        return False
    cleaned = text.strip().lower()
    return bool(GREETING_RE.search(cleaned))


def get_short_reply(text: str):
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
    """Return True if the text is purely a greeting, smalltalk, acknowledgment, or closing, without domain keywords from DB forms."""
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


def build_greeting_reply(forms=None, user_text=None):
    """Return a natural, 1-sentence model-side conversational reply."""
    if user_text:
        return mfp.generate_conversational_reply(user_text)
    return "Hello! I am here to help you fill out forms, register visitors, or report maintenance requests. How can I assist you today?"


def build_form_overview_message(form, accepted=None):
    """Produce a short, clean overview of the form fields."""
    schema = form["schema"]
    form_name = form["form_name"]

    real_accepted = {k: v for k, v in (accepted or {}).items() if v is not None and str(v).strip() != "" and str(v).lower() != "null"}
    if real_accepted:
        lines = [f"Let's fill out **{form_name}**.\n"]
        lines.append("Captured from your message:")
        for field in schema["fields"]:
            if field["name"] in real_accepted:
                lines.append(f"- {field['label']}: {real_accepted[field['name']]}")

        remaining = [f for f in schema["fields"] if f["name"] not in real_accepted]
        if remaining:
            lines.append("")
            lines.append("Still needed:")
            for idx, field in enumerate(remaining, 1):
                req_tag = "Required" if field.get("required", True) else "Optional"
                options = field.get("options")
                opts_str = f" ({', '.join(options)})" if options and field.get("type") == "select" else ""
                lines.append(f"{idx}. {field['label']} [{req_tag}]{opts_str}")
        return "\n".join(lines)

    return f"Let's fill out **{form_name}**."


# ---------------------------------------------------------------------------
# External API stand-ins (replace with HTTP calls in production)
# ---------------------------------------------------------------------------

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Ensure database tables exist and default forms/fields are seeded."""
    conn = get_connection()
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS forms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    aliases TEXT NOT NULL DEFAULT '[]'
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS form_fields (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    form_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    label TEXT NOT NULL,
                    type TEXT NOT NULL DEFAULT 'text',
                    required INTEGER NOT NULL DEFAULT 1,
                    options TEXT,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (form_id) REFERENCES forms(id),
                    UNIQUE(form_id, name)
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS form_submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    form_id INTEGER NOT NULL,
                    answers_json TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (form_id) REFERENCES forms(id)
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chats (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    title_locked INTEGER NOT NULL DEFAULT 0,
                    messages_json TEXT NOT NULL DEFAULT '[]',
                    answers_json TEXT NOT NULL DEFAULT '{}',
                    form_key TEXT,
                    current_field_name TEXT,
                    completed INTEGER NOT NULL DEFAULT 0,
                    submission_id INTEGER,
                    updated_at TEXT NOT NULL
                );
            """)
            # Seed default forms only when the forms table is empty so restarts
            # do not inflate SQLite AUTOINCREMENT sequences via INSERT OR IGNORE.
            if conn.execute("SELECT COUNT(*) FROM forms").fetchone()[0] == 0:
                conn.executescript("""
                    INSERT OR IGNORE INTO forms (key, name, aliases) VALUES
                    ('add_notice', 'Add Notice',
                     '["add notice", "notice", "new notice", "create notice"]'),
                    ('visitor_entry', 'Visitor Entry',
                     '["visitor entry", "visitor", "guest", "add visitor", "visitor pass"]'),
                    ('raise_complaint', 'Raise Complaint',
                     '["raise complaint", "complaint", "issue", "report issue", "problem"]');

                    -- Add Notice
                    INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                    SELECT id, 'wing', 'Wing', 'select', 0, '["All", "A Wing", "B Wing", "C Wing"]', 1
                    FROM forms WHERE key = 'add_notice';

                    INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                    SELECT id, 'subject', 'Subject', 'text', 1, NULL, 2
                    FROM forms WHERE key = 'add_notice';

                    INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                    SELECT id, 'description', 'Description', 'textarea', 1, NULL, 3
                    FROM forms WHERE key = 'add_notice';

                    -- Visitor Entry
                    INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                    SELECT id, 'visitor_name', 'Visitor Name', 'text', 1, NULL, 1
                    FROM forms WHERE key = 'visitor_entry';

                    INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                    SELECT id, 'flat_number', 'Flat Number', 'text', 1, NULL, 2
                    FROM forms WHERE key = 'visitor_entry';

                    INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                    SELECT id, 'purpose', 'Purpose', 'select', 1, '["Guest", "Delivery", "Service", "Other"]', 3
                    FROM forms WHERE key = 'visitor_entry';

                    INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                    SELECT id, 'vehicle_number', 'Vehicle Number', 'text', 0, NULL, 4
                    FROM forms WHERE key = 'visitor_entry';

                    -- Raise Complaint
                    INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                    SELECT id, 'category', 'Category', 'select', 1, '["Water", "Electricity", "Lift", "Parking", "Other"]', 1
                    FROM forms WHERE key = 'raise_complaint';

                    INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                    SELECT id, 'flat_number', 'Flat Number', 'text', 1, NULL, 2
                    FROM forms WHERE key = 'raise_complaint';

                    INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                    SELECT id, 'details', 'Details', 'textarea', 1, NULL, 3
                    FROM forms WHERE key = 'raise_complaint';

                    INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                    SELECT id, 'urgency', 'Urgency', 'select', 0, '["Low", "Medium", "High"]', 4
                    FROM forms WHERE key = 'raise_complaint';

                    -- Facility Booking
                    INSERT OR IGNORE INTO forms (key, name, aliases) VALUES
                    ('facility_booking', 'Facility Booking',
                     '["facility booking", "book facility", "amenity booking", "clubhouse booking"]');

                    INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                    SELECT id, 'facility', 'Facility', 'select', 1, '["Clubhouse", "Party Hall", "Gym", "Swimming Pool", "Sports Court", "Other"]', 1
                    FROM forms WHERE key = 'facility_booking';

                    INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                    SELECT id, 'booking_date', 'Booking Date', 'date', 1, NULL, 2
                    FROM forms WHERE key = 'facility_booking';

                    INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                    SELECT id, 'time_slot', 'Time Slot', 'select', 1, '["Morning", "Afternoon", "Evening"]', 3
                    FROM forms WHERE key = 'facility_booking';

                    INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                    SELECT id, 'purpose', 'Purpose', 'textarea', 0, NULL, 4
                    FROM forms WHERE key = 'facility_booking';

                    INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                    SELECT id, 'number_of_people', 'Number of People', 'number', 0, NULL, 5
                    FROM forms WHERE key = 'facility_booking';
                """)
            # Ensure any date fields are typed as 'date'
            conn.execute("UPDATE form_fields SET type = 'date' WHERE (name LIKE '%date%' OR label LIKE '%date%') AND type = 'text'")
    except sqlite3.Error as exc:
        log.error("Failed to initialize database: %s", exc)
    finally:
        conn.close()
    invalidate_domain_words_cache()


init_db()


def list_forms():
    """GET /api/forms"""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, key, name, aliases FROM forms ORDER BY name"
        ).fetchall()
    except sqlite3.Error:
        log.error("GET /api/forms failed")
        return []
    finally:
        conn.close()

    forms = [
        {
            "id": row["id"],
            "key": row["key"],
            "form_name": row["name"],
            "aliases": json.loads(row["aliases"] or "[]"),
        }
        for row in rows
    ]
    log.info(
        "GET /api/forms done | count=%s keys=%s",
        len(forms),
        [f["key"] for f in forms],
    )
    return forms


def fetch_form_schema(form_key):
    """GET /api/forms/{key}/schema"""
    conn = get_connection()
    try:
        form_row = conn.execute(
            "SELECT id, key, name, aliases FROM forms WHERE key = ?",
            (form_key,),
        ).fetchone()
        if form_row is None:
            log.warning("GET /api/forms/%s/schema done | not found", form_key)
            return None

        field_rows = conn.execute(
            """
            SELECT name, label, type, required, options
            FROM form_fields
            WHERE form_id = ?
            ORDER BY sort_order ASC, id ASC
            """,
            (form_row["id"],),
        ).fetchall()
    except sqlite3.Error:
        log.error("GET /api/forms/%s/schema failed", form_key)
        return None
    finally:
        conn.close()

    fields = []
    for row in field_rows:
        field = {
            "name": row["name"],
            "label": row["label"],
            "type": row["type"],
            "required": bool(row["required"]),
        }
        if row["options"]:
            field["options"] = json.loads(row["options"])
        fields.append(field)

    result = {
        "id": form_row["id"],
        "key": form_row["key"],
        "form_name": form_row["name"],
        "aliases": json.loads(form_row["aliases"] or "[]"),
        "schema": {"form_name": form_row["name"], "fields": fields},
        "system_prompt": GENERIC_SYSTEM_PROMPT,
    }
    log.info(
        "GET /api/forms/%s/schema done | form=%s fields=%s",
        form_key,
        result["form_name"],
        [f["name"] for f in fields],
    )
    return result


def submit_form(form_id, answers):
    """POST /api/forms/{id}/submit"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO form_submissions (form_id, answers_json) VALUES (?, ?)",
            (form_id, json.dumps(answers)),
        )
        submission_id = cur.lastrowid
        conn.commit()
        log.info(
            "POST /api/forms/%s/submit done | submission_id=%s answers=%s",
            form_id,
            submission_id,
            answers,
        )
        return submission_id
    except sqlite3.Error:
        log.error("POST /api/forms/%s/submit failed", form_id)
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Core chat logic
# ---------------------------------------------------------------------------

FORM_INTENT_PREFIXES_RE = re.compile(
    r"^(?:(?:hi|hey|hello|please|can\s+you|could\s+you|i\s+want\s+to|i\s+would\s+like\s+to|i\s+need\s+to|help\s+me(?:\s+to|\s+with)?|can\s+you\s+help\s+me(?:\s+to|\s+with)?|with|let(?:\'s|s)?|start|open|fill(?:\s+out|\s+up)?|create|new|raise|lodge|register|submit|do|make)\s+)+(?:a\s+|an\s+|the\s+)?",
    re.IGNORECASE,
)
FORM_INTENT_SUFFIXES_RE = re.compile(
    r"(?:\s+(?:form|please|now|entry))+$",
    re.IGNORECASE,
)


def clean_form_selection_text(user_text: str) -> str:
    """Strip common conversational wrappers around form names."""
    text = user_text.strip().lower()
    text = FORM_INTENT_PREFIXES_RE.sub("", text).strip()
    text = FORM_INTENT_SUFFIXES_RE.sub("", text).strip()
    return text.strip(" .,!?:;\"'")


def detect_form_key(user_text):
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


def message_is_only_form_name(user_text, form):
    """True when the message only picks a form and has no field values."""
    raw_clean = user_text.strip().lower().strip(" .,!?:;\"'")
    cleaned = clean_form_selection_text(user_text)
    names = {str(form["form_name"]).strip().lower(), str(form["key"]).strip().lower()}
    for alias in form.get("aliases", []):
        names.add(str(alias).strip().lower())

    if raw_clean in names or cleaned in names:
        return True

    for n in names:
        if cleaned and (n == cleaned or (n.startswith(cleaned) and len(cleaned) >= 4)):
            return True

    return False


def get_next_field(schema, answers):
    answers_dict = answers or {}
    for field in schema["fields"]:
        fname = field["name"]
        if fname not in answers_dict:
            return field
        val = answers_dict[fname]
        if field.get("required", True) and (val is None or str(val).strip() == ""):
            return field
    return None


def is_all_filled(schema, answers):
    return get_next_field(schema, answers) is None


def build_fallback_question(field):
    label = field["label"].lower()
    options = field.get("options") or []
    required = field.get("required", True)

    if options:
        q = f"What is the {label}? ({', '.join(options)})"
    else:
        q = f"What is the {label}?"

    if not required:
        q += ' (or type "skip")'
    return q


def get_question_for_field(form, field):
    prompt = f"""
Form: {form['form_name']}

Field to collect:
{json.dumps(field, indent=2)}

Ask ONE natural, clear, and polite question to collect this field.
Return JSON:
{{
  "question": "..."
}}
"""

    try:
        response = ollama_client.chat(
            model=MODEL,
            format="json",
            messages=[
                {"role": "system", "content": form["system_prompt"]},
                {"role": "user", "content": prompt},
            ],
        )
        result = json.loads(response["message"]["content"])
        question = (result.get("question") or "").strip()
        if question:
            return question
    except Exception:
        pass

    return build_fallback_question(field)


def normalize_select_answer(user_input, options):
    cleaned = user_input.strip().lower()
    if not cleaned:
        return None

    # 1. Exact match (case-insensitive)
    for opt in options:
        if cleaned == opt.lower():
            return opt

    # 2. Match if cleaned is an exact word or prefix of an option
    for opt in options:
        opt_lower = opt.lower()
        opt_words = re.findall(r"\b[a-z0-9]+\b", opt_lower)
        if cleaned in opt_words:
            return opt
        if opt_lower.startswith(cleaned) and len(cleaned) >= 2:
            return opt

    # 3. Match if option is contained inside user input as a distinct phrase
    for opt in options:
        opt_lower = opt.lower()
        if re.search(r"\b" + re.escape(opt_lower) + r"\b", cleaned):
            return opt

    return None


def validate_answer(field, user_input):
    options = field.get("options") or []
    required = field.get("required", True)
    cleaned = user_input.strip()

    if not cleaned:
        return False, "Please enter a value."

    if not required and cleaned.lower() in ("skip", "none", "na", "n/a", "nil", "no"):
        return True, None

    if cleaned.lower() in ("null", "none", "nil", "undefined"):
        return False, f"Please enter a valid {field['label'].lower()}."

    # Guard against treating greetings or smalltalk as field answers
    if is_greeting_or_smalltalk(cleaned):
        return False, f"That looks like a greeting. We are currently filling out **{field['label']}**. Please provide your {field['label'].lower()} (or type 'cancel' to exit)."

    field_name = field.get("name", "").lower()
    field_type = field.get("type", "text").lower()
    field_label = field.get("label", "").lower()

    # 1. Select options
    if field_type == "select" and options:
        matched = normalize_select_answer(cleaned, options)
        if matched is None:
            return False, f"Please choose one of: {', '.join(options)}"
        return True, matched

    # 2. Date fields
    if field_type == "date" or "date" in field_name:
        norm = normalize_date(cleaned)
        if not norm:
            return False, f"Please enter a valid date for {field['label']}."
        return True, norm

    # 3. Vehicle registration plate (for fields mentioning vehicle/plate)
    if "vehicle" in field_name or "plate" in field_name:
        if not required and (NO_VEHICLE_RE.search(cleaned) or cleaned.lower() in ("no", "none", "na", "n/a", "nil", "skip")):
            return True, None
        if is_valid_indian_plate(cleaned):
            return True, normalize_plate(cleaned)
        if not required:
            return False, "Please enter a valid Indian vehicle number plate (e.g. MH 12 AB 1234 or 22 BH 1234 AA), or type 'skip'."
        return False, "Please enter a valid Indian vehicle number plate (e.g. MH 12 AB 1234 or 22 BH 1234 AA)."

    # 3. Email address
    if field_type == "email" or "email" in field_name:
        if not EMAIL_RE.match(cleaned):
            return False, "Please enter a valid email address (e.g. name@example.com)."
        return True, cleaned.lower()

    # 4. Phone number
    if field_type in ("phone", "tel") or any(k in field_name for k in ("phone", "mobile", "contact")):
        if not PHONE_RE.match(cleaned):
            return False, "Please enter a valid phone number."
        return True, cleaned

    # 5. Number / numeric
    if field_type in ("number", "integer", "numeric") or any(k in field_name for k in ("age", "count", "quantity", "amount", "years")):
        if not NUMBER_RE.match(cleaned):
            return False, f"Please enter a valid numeric value for {field['label'].lower()}."
        return True, cleaned

    return True, cleaned


def _as_text(value):
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in ("null", "none", "nil", "n/a", "undefined"):
            return None
        return text
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _field_spec(fields):
    spec = []
    for field in fields:
        item = {
            "name": field["name"],
            "label": field["label"],
            "type": field["type"],
            "required": field.get("required", True),
        }
        if field.get("options"):
            item["options"] = field["options"]
        spec.append(item)
    return spec


def _resolve_form_key(raw, known):
    """Match the model's form value to a real key or exact form name."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or text.lower() in ("null", "none", "nil"):
        return None
    needle = re.sub(r"[\s_\-]+", "", text.lower())
    for key, name in known.items():
        if needle == re.sub(r"[\s_\-]+", "", key.lower()):
            return key
        if needle == re.sub(r"[\s_\-]+", "", name.lower()):
            return key
    return None


def format_bullet_spec(fields):
    lines = []
    for f in fields:
        name = f["name"]
        label = f.get("label", name)
        ftype = f.get("type", "text")
        opts = f.get("options")
        if opts:
            lines.append(f"- {name} ({label}, options: {json.dumps(opts)})")
        else:
            lines.append(f"- {name} ({label}, type: {ftype})")
    return "\n".join(lines)


def extract_heuristic_fields(fields, user_text: str) -> dict:
    """Deterministically extract fields from user_text using options, patterns, and explicit labels."""
    extracted = {}
    text = (user_text or "").strip()
    if not text:
        return extracted

    # 1. Exact options and format-specific patterns
    for field in fields:
        name = field["name"]
        n_lower = name.lower()
        label = field.get("label", name)
        l_lower = label.lower()
        ftype = field.get("type", "text")
        options = field.get("options") or []

        # Select option matching
        if ftype == "select" and options:
            for opt in options:
                if re.search(r'\b' + re.escape(opt) + r'\b', text, re.IGNORECASE):
                    extracted[name] = opt
                    break
                parts = opt.split()
                if len(parts) == 2:
                    inverted = f"{parts[1]} {parts[0]}"
                    if re.search(r'\b' + re.escape(inverted) + r'\b', text, re.IGNORECASE):
                        extracted[name] = opt
                        break

        # Vehicle number
        if "vehicle" in n_lower or "plate" in n_lower or "vehicle" in l_lower:
            if NO_VEHICLE_RE.search(text):
                extracted[name] = "skip"
            else:
                m = INDIAN_PLATE_EXTRACT_RE.search(text)
                if m:
                    extracted[name] = normalize_plate(m.group(1))

        # Email
        if "email" in n_lower or "email" in l_lower:
            m = re.search(r"\b([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)\b", text)
            if m:
                extracted[name] = m.group(1)

        # Phone / Mobile
        if "phone" in n_lower or "mobile" in n_lower or "contact" in n_lower:
            m = re.search(r"\b(\+?[0-9]{1,3}[ -]?[6-9]\d{9})\b", text)
            if m:
                extracted[name] = m.group(1)

        # Flat / Room / Unit number
        if "flat" in n_lower or "unit" in n_lower or "room" in n_lower or "flat" in l_lower:
            m = re.search(r'\b(?:flat|room|unit|apt|apartment)\s*(?:no\.?|number|#|is|:)?\s*([A-Za-z]?\s*[-/]?\s*\d{1,4}[A-Za-z]?)\b', text, re.IGNORECASE)
            if m:
                extracted[name] = m.group(1).strip()

    # 2. Labeled key-value markers: "Label: <value>", "Label is <value>", "name: <value>"
    for field in fields:
        fname = field["name"]
        if fname in extracted:
            continue
        flabel = field.get("label", fname)
        ftype = field.get("type", "text")

        candidates = [flabel, fname]
        if " " in flabel:
            candidates.extend(flabel.split())

        candidates = sorted(set(candidates), key=len, reverse=True)
        cand_pat = "|".join(re.escape(c) for c in candidates)

        # Textarea / multi-line fields (e.g. Description, Details, Notes, Message)
        if ftype == "textarea" or any(k in fname.lower() for k in ("desc", "detail", "note", "message", "reason", "issue")):
            m = re.search(rf'(?:^|[\n.;,])\s*(?:{cand_pat})\s*(?::|is|-|=)\s*(.+)$', text, re.IGNORECASE | re.DOTALL)
            if m:
                val = m.group(1).strip()
                if val:
                    extracted[fname] = val
                    continue

        # Standard text fields
        m = re.search(rf'(?:^|[\n.;,])\s*(?:{cand_pat})\s*(?::|is|-|=)\s*([^\n;.]+)', text, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if val and len(val) >= 2:
                extracted[fname] = val
                continue

        # Visitor name pattern: "Visitor <Name>"
        if "visitor" in fname.lower() and "name" in fname.lower():
            m_vis = re.search(r'\bvisitor\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)\b', text, re.IGNORECASE)
            if m_vis:
                extracted[fname] = m_vis.group(1).strip()

    return extracted


def extract_fields_from_text(form_name, user_text, fields):
    """Return {field_name: text} for values found in the message via hybrid (heuristic + LLM) extraction."""
    if not fields or not user_text.strip():
        return {}

    heuristic = extract_heuristic_fields(fields, user_text)
    remaining = [f for f in fields if f["name"] not in heuristic]

    # If all fields are already captured heuristically, return immediately without calling LLM
    if not remaining:
        log.info("EXTRACT done (heuristic full) | form=%s extracted=%s", form_name, heuristic)
        return heuristic

    bullet_spec = format_bullet_spec(fields)
    prompt = f"""Form: {form_name}

Fields to extract:
{bullet_spec}

User message:
"{user_text}"

Extract all present fields as JSON:
{{"fields": {{...}}}}"""

    llm_extracted = {}
    try:
        response = ollama_client.chat(
            model=MODEL,
            format="json",
            options={"temperature": 0.0},
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        result = json.loads(response["message"]["content"])
        raw = result.get("fields") or {}
        allowed = {field["name"] for field in fields}
        if isinstance(raw, dict):
            for name, value in raw.items():
                if name in allowed:
                    text_val = _as_text(value)
                    if text_val is not None:
                        llm_extracted[name] = text_val
    except Exception as exc:
        log.error("EXTRACT LLM failed | form=%s error=%s", form_name, exc)

    # Merge: heuristic takes precedence for exact matches
    final_extracted = dict(llm_extracted)
    final_extracted.update(heuristic)

    # Fallback for vehicle
    allowed = {field["name"] for field in fields}
    for name in allowed:
        n_lower = name.lower()
        if ("vehicle" in n_lower or "plate" in n_lower) and name not in final_extracted:
            if NO_VEHICLE_RE.search(user_text):
                final_extracted[name] = "skip"
            else:
                veh_match = INDIAN_PLATE_EXTRACT_RE.search(user_text)
                if veh_match:
                    final_extracted[name] = normalize_plate(veh_match.group(1))

    # Fallback for notice wing: general notices default to "All" wings if no specific wing is mentioned
    if "notice" in form_name.lower():
        if "wing" in allowed and "wing" not in final_extracted:
            final_extracted["wing"] = "All"

    log.info("EXTRACT done | form=%s extracted=%s", form_name, final_extracted)
    return final_extracted


def accept_extracted_value(field, raw):
    """Accept model extraction when it passes basic field validation."""
    if raw is None:
        return False, None
    if not isinstance(raw, str):
        return False, None
    cleaned = raw.strip()

    if cleaned.lower() in ("skip", "null", "none", "nil", "n/a", "undefined"):
        if not field.get("required", True):
            return True, None
        return False, None

    field_name = field.get("name", "").lower()
    if ("vehicle" in field_name or "plate" in field_name) and not field.get("required", True):
        if NO_VEHICLE_RE.search(cleaned) or cleaned.lower() in ("skip", "none", "nil", "n/a"):
            return True, None

    ok, value = validate_answer(field, cleaned)
    if not ok:
        return False, None
    return True, value


def merge_extracted_fields(schema, extracted, answers):
    """Keep extracted values that pass validation. Never overwrite filled fields."""
    pending = [field for field in schema["fields"] if field["name"] not in answers]
    accepted = {}
    for field in pending:
        raw = extracted.get(field["name"])
        if raw is None:
            continue
        ok, value = accept_extracted_value(field, raw)
        if ok:
            answers[field["name"]] = value
            accepted[field["name"]] = value
    log.info(
        "APPLY done | form=%s accepted=%s pending_left=%s",
        schema["form_name"],
        list(accepted.keys()),
        [f["name"] for f in schema["fields"] if f["name"] not in answers],
    )
    return accepted


def apply_extracted_answers(schema, user_text, answers):
    """Extract from the message, then keep values that pass validation."""
    pending = [field for field in schema["fields"] if field["name"] not in answers]
    extracted = extract_fields_from_text(schema["form_name"], user_text, pending)
    return merge_extracted_fields(schema, extracted, answers)


def classify_and_extract(user_text):
    """Model returns the form key and any fields already in the message."""
    if not user_text.strip() or is_greeting_or_smalltalk(user_text):
        return None, {}

    catalog = []
    known = {}
    for meta in list_forms():
        schema = fetch_form_schema(meta["key"])
        if schema is None:
            continue
        known[schema["key"]] = schema["form_name"]
        catalog.append(
            {
                "key": schema["key"],
                "form_name": schema["form_name"],
                "fields": _field_spec(schema["schema"]["fields"]),
            }
        )
    if not catalog:
        return None, {}

    prompt = f"""
Forms:
{json.dumps(catalog, indent=2)}

User message:
{user_text}

NEGATIVE CONSTRAINT:
Do NOT invent values. If the message only asks to fill a form, return empty fields: {{"form": "<key>", "fields": {{}}}}.

Return JSON:
{{"form": "<form key or null>", "fields": {{"<field name>": "<value or null>"}}}}
"""

    try:
        response = ollama_client.chat(
            model=MODEL,
            format="json",
            options={"temperature": 0.0},
            messages=[
                {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        result = json.loads(response["message"]["content"])
    except Exception as exc:
        log.error("CLASSIFY failed | error=%s", exc)
        return None, {}

    if not isinstance(result, dict):
        log.warning("CLASSIFY done | invalid payload=%r", result)
        return None, {}

    form_key = _resolve_form_key(result.get("form"), known)
    raw = result.get("fields") if isinstance(result.get("fields"), dict) else {}
    if form_key is None:
        log.info("CLASSIFY done | form=null raw_form=%r", result.get("form"))
        return None, {}

    schema = fetch_form_schema(form_key)
    allowed = {field["name"] for field in schema["schema"]["fields"]}
    extracted = {}
    for name, value in raw.items():
        if name not in allowed:
            continue
        text = _as_text(value)
        if text is not None:
            extracted[name] = text

    heuristic = extract_heuristic_fields(schema["schema"]["fields"], user_text)
    extracted.update(heuristic)

    for name in allowed:
        n_lower = name.lower()
        if ("vehicle" in n_lower or "plate" in n_lower) and name not in extracted:
            if NO_VEHICLE_RE.search(user_text):
                extracted[name] = "skip"
            else:
                veh_match = INDIAN_PLATE_EXTRACT_RE.search(user_text)
                if veh_match:
                    extracted[name] = normalize_plate(veh_match.group(1))

    if form_key == "add_notice" and "wing" in allowed and "wing" not in extracted:
        extracted["wing"] = "All"

    log.info("CLASSIFY done | form=%s fields=%s", form_key, extracted)
    return form_key, extracted


def format_filled_message(schema, answers, accepted):
    lines = []
    for field in schema["fields"]:
        if field["name"] not in accepted:
            continue
        shown = answers[field["name"]]
        if shown is None:
            shown = "skipped"
        lines.append(f"- {field['label']}: {shown}")
    return "I filled these from your message:\n" + "\n".join(lines)


def build_final_data(schema, answers):
    final_data = {"form": schema["form_name"]}
    for field in schema["fields"]:
        final_data[field["name"]] = answers.get(field["name"])
    return final_data


def format_completion_message(schema, answers, submission_id=None):
    final_data = build_final_data(schema, answers)
    if submission_id:
        header = f"All done. Submitted as `#{submission_id}`."
    else:
        header = "All done."
    json_str = json.dumps(final_data, indent=2)
    return (
        f"{header}\n\n"
        f"```json\n{json_str}\n```\n\n"
        f"Type another form name or greeting to continue."
    )


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_chats_from_disk():
    """Load all chat sessions from SQLite DB (forms.db)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, title, title_locked, messages_json, answers_json,
                   form_key, current_field_name, completed, submission_id, updated_at
            FROM chats
            ORDER BY updated_at DESC
            """
        ).fetchall()
        chats = []
        for row in rows:
            chats.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "title_locked": bool(row["title_locked"]),
                    "messages": json.loads(row["messages_json"] or "[]"),
                    "answers": json.loads(row["answers_json"] or "{}"),
                    "form_key": row["form_key"],
                    "current_field_name": row["current_field_name"],
                    "completed": bool(row["completed"]),
                    "submission_id": row["submission_id"],
                    "updated_at": row["updated_at"],
                }
            )
        return chats
    except sqlite3.Error as exc:
        log.error("Failed to load chats from DB: %s", exc)
        return []
    finally:
        conn.close()


def save_chats_to_disk(chats):
    """Save chat sessions to SQLite DB (forms.db)."""
    if not chats:
        return
    conn = get_connection()
    try:
        with conn:
            for chat in chats:
                conn.execute(
                    """
                    INSERT INTO chats (
                        id, title, title_locked, messages_json, answers_json,
                        form_key, current_field_name, completed, submission_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title = excluded.title,
                        title_locked = excluded.title_locked,
                        messages_json = excluded.messages_json,
                        answers_json = excluded.answers_json,
                        form_key = excluded.form_key,
                        current_field_name = excluded.current_field_name,
                        completed = excluded.completed,
                        submission_id = excluded.submission_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        chat["id"],
                        chat.get("title") or "Untitled",
                        1 if chat.get("title_locked") else 0,
                        json.dumps(chat.get("messages") or []),
                        json.dumps(chat.get("answers") or {}),
                        chat.get("form_key"),
                        chat.get("current_field_name"),
                        1 if chat.get("completed") else 0,
                        chat.get("submission_id"),
                        chat.get("updated_at") or utc_now(),
                    ),
                )
    except sqlite3.Error as exc:
        log.error("Failed to save chats to DB: %s", exc)
    finally:
        conn.close()


def delete_chat_from_db(chat_id):
    """Delete a chat session from SQLite DB (forms.db)."""
    conn = get_connection()
    try:
        with conn:
            conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
    except sqlite3.Error as exc:
        log.error("Failed to delete chat %s from DB: %s", chat_id, exc)
    finally:
        conn.close()


def make_chat(title="Untitled"):
    return {
        "id": str(uuid.uuid4()),
        "title": title,
        "title_locked": False,
        "messages": [],
        "answers": {},
        "form_key": None,
        "current_field_name": None,
        "completed": False,
        "submission_id": None,
        "updated_at": utc_now(),
    }


def chat_local_date(chat):
    raw = chat.get("updated_at") or ""
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().date()
    except ValueError:
        return date.today()


def group_chats_by_recency(chats):
    """Return ordered sections: Today, Yesterday, Old."""
    today = date.today()
    yesterday = today - timedelta(days=1)
    groups = {"Today": [], "Yesterday": [], "Old": []}
    ordered = sorted(chats, key=lambda c: c.get("updated_at") or "", reverse=True)
    for chat in ordered:
        day = chat_local_date(chat)
        if day == today:
            groups["Today"].append(chat)
        elif day == yesterday:
            groups["Yesterday"].append(chat)
        else:
            groups["Old"].append(chat)
    return [(label, items) for label, items in groups.items() if items]


def rename_chat(chat_id, new_title):
    chat = find_chat(chat_id)
    if chat is None:
        return
    title = (new_title or "").strip() or "Untitled"
    chat["title"] = title[:60]
    chat["title_locked"] = True
    chat["updated_at"] = utc_now()
    save_chats_to_disk(st.session_state.chats)


def delete_chat(chat_id):
    was_active = chat_id == st.session_state.active_chat_id
    st.session_state.chats = [
        chat for chat in st.session_state.chats if chat["id"] != chat_id
    ]
    delete_chat_from_db(chat_id)
    if st.session_state.get("editing_chat_id") == chat_id:
        st.session_state.editing_chat_id = None

    if not st.session_state.chats:
        chat = make_chat()
        st.session_state.chats = [chat]
        apply_chat_to_session(chat)
        save_chats_to_disk(st.session_state.chats)
    elif was_active:
        apply_chat_to_session(st.session_state.chats[0])

    save_chats_to_disk(st.session_state.chats)


def chat_title_from_state():
    form = st.session_state.form
    if form:
        return form["form_name"]
    for msg in st.session_state.messages:
        if msg["role"] == "user":
            text = msg["content"].strip().replace("\n", " ")
            return text[:40] + ("…" if len(text) > 40 else "")
    return "Untitled"


def find_chat(chat_id):
    for chat in st.session_state.chats:
        if chat["id"] == chat_id:
            return chat
    return None


def sync_active_chat():
    chat = find_chat(st.session_state.active_chat_id)
    if chat is None:
        return

    form = st.session_state.form
    field = st.session_state.current_field
    chat["messages"] = list(st.session_state.messages)
    chat["answers"] = dict(st.session_state.answers)
    chat["form_key"] = form["key"] if form else None
    chat["current_field_name"] = field["name"] if field else None
    chat["completed"] = st.session_state.completed
    chat["submission_id"] = st.session_state.submission_id
    chat["updated_at"] = utc_now()
    if not chat.get("title_locked"):
        if chat["title"] in ("New chat", "Untitled", "") or form:
            chat["title"] = chat_title_from_state()
    save_chats_to_disk(st.session_state.chats)


def clear_form_state():
    st.session_state.answers = {}
    st.session_state.current_field = None
    st.session_state.completed = False
    st.session_state.form = None
    st.session_state.submission_id = None


def apply_chat_to_session(chat):
    st.session_state.active_chat_id = chat["id"]
    st.session_state.messages = list(chat.get("messages") or [])
    st.session_state.answers = dict(chat.get("answers") or {})
    st.session_state.completed = bool(chat.get("completed"))
    st.session_state.submission_id = chat.get("submission_id")
    st.session_state.form = None
    st.session_state.current_field = None

    form_key = chat.get("form_key")
    if not form_key:
        return

    form = fetch_form_schema(form_key)
    if form is None:
        return

    if st.session_state.completed:
        # If chat was previously marked completed, ensure the completion card is
        # in the chronological messages and reset active form state.
        has_done = any(
            msg.get("role") == "assistant" and "All done" in msg.get("content", "")
            for msg in st.session_state.messages
        )
        if not has_done:
            completion_msg = format_completion_message(
                form["schema"], st.session_state.answers, st.session_state.submission_id
            )
            st.session_state.messages.append({"role": "assistant", "content": completion_msg})
        clear_form_state()
        sync_active_chat()
        return

    st.session_state.form = form
    field_name = chat.get("current_field_name")
    if field_name:
        for field in form["schema"]["fields"]:
            if field["name"] == field_name:
                st.session_state.current_field = field
                break


def new_chat():
    sync_active_chat()
    chat = make_chat()
    st.session_state.chats.insert(0, chat)
    apply_chat_to_session(chat)
    save_chats_to_disk(st.session_state.chats)


def switch_chat(chat_id):
    if chat_id == st.session_state.active_chat_id:
        return
    sync_active_chat()
    chat = find_chat(chat_id)
    if chat is None:
        return
    apply_chat_to_session(chat)


def form_meta_by_key(form_key):
    for form in list_forms():
        if form["key"] == form_key:
            return form
    return None


def should_switch_to_form(user_text):
    """Return form_key when this message should start/switch forms in the same chat."""
    form_key = detect_form_key(user_text)
    if form_key is None:
        return None

    try:
        current = getattr(st.session_state, "form", None)
        completed = getattr(st.session_state, "completed", False)
    except Exception:
        current = None
        completed = False

    if current is None or completed:
        return form_key

    meta = form_meta_by_key(form_key)
    if meta and message_is_only_form_name(user_text, meta):
        return form_key
    return None


def init_session():
    defaults = {
        "answers": {},
        "messages": [],
        "current_field": None,
        "completed": False,
        "form": None,
        "submission_id": None,
        "chats": None,
        "active_chat_id": None,
        "editing_chat_id": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

    if st.session_state.chats is None:
        chats = load_chats_from_disk()
        if not chats:
            chats = [make_chat()]
            save_chats_to_disk(chats)
        st.session_state.chats = chats

    if st.session_state.active_chat_id is None:
        apply_chat_to_session(st.session_state.chats[0])


def ask_next_question():
    form = st.session_state.form
    if form is None:
        return

    field = get_next_field(form["schema"], st.session_state.answers)
    if field is None:
        final_data = build_final_data(form["schema"], st.session_state.answers)
        sid = submit_form(form["id"], final_data)
        st.session_state.submission_id = sid

        completion_msg = format_completion_message(
            form["schema"], st.session_state.answers, sid
        )
        st.session_state.messages.append({"role": "assistant", "content": completion_msg})
        clear_form_state()
        sync_active_chat()
        return

    with st.spinner("AI is thinking..."):
        question = get_question_for_field(form, field)

    st.session_state.current_field = field
    st.session_state.messages.append({"role": "assistant", "content": question})
    sync_active_chat()


def reset_chat():
    new_chat()


def start_form(form_key, user_text, prefilled=None):
    form = fetch_form_schema(form_key)
    if form is None:
        st.session_state.messages.append(
            {"role": "assistant", "content": "Sorry, that form was not found."}
        )
        sync_active_chat()
        return

    # Keep chat messages; only reset the active form slot.
    st.session_state.form = form
    st.session_state.answers = {}
    st.session_state.current_field = None
    st.session_state.completed = False
    st.session_state.submission_id = None

    accepted = {}
    if prefilled:
        accepted = merge_extracted_fields(
            form["schema"], prefilled, st.session_state.answers
        )
    if get_next_field(form["schema"], st.session_state.answers) is not None and user_text and not message_is_only_form_name(user_text, form) and not is_greeting_or_smalltalk(user_text):
        with st.spinner("Reading your message..."):
            more_accepted = apply_extracted_answers(
                form["schema"], user_text, st.session_state.answers
            )
            accepted.update(more_accepted)

    if form_key == "add_notice" and "wing" not in st.session_state.answers:
        st.session_state.answers["wing"] = "All"

    overview_msg = build_form_overview_message(form, accepted=accepted)
    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": overview_msg,
        }
    )

    ask_next_question()
    sync_active_chat()


def chatbot_help_reply(user_text, forms):
    """Natural, concise chatbot reply when no form is selected yet."""
    short = get_short_reply(user_text)
    if short:
        return short

    if is_greeting(user_text):
        return build_greeting_reply(forms, user_text=user_text)

    names = [form["form_name"] for form in forms] or ["(none yet)"]
    prompt = f"""Available forms: {", ".join(names)}

User message: {user_text}

Reply politely in 1 or 2 SHORT sentences maximum. Do not write essays or long lists."""
    try:
        response = ollama_client.chat(
            model=MODEL,
            messages=[
                {"role": "system", "content": HELP_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        text = (response["message"]["content"] or "").strip()
        if text:
            log.info("HELP reply done | preview=%s", text[:120])
            return text
    except Exception as exc:
        log.error("HELP reply failed | error=%s", exc)

    return "I can help you fill out any of our forms whenever you're ready. Just let me know!"


def handle_form_selection(user_text):
    if is_greeting_or_smalltalk(user_text) or any(w in user_text.lower().split() for w in ("hy", "helo", "hiii", "hey", "hello", "hi")):
        with st.spinner("AI is thinking..."):
            reply = mfp.generate_conversational_reply(user_text)
        st.session_state.messages.append({"role": "assistant", "content": reply})
        sync_active_chat()
        return

    direct_key = detect_form_key(user_text)
    if direct_key:
        start_form(direct_key, user_text)
        return

    with st.spinner("Thinking..."):
        form_key, fields = classify_and_extract(user_text)

    if form_key is None:
        reply = chatbot_help_reply(user_text, list_forms())
        st.session_state.messages.append({"role": "assistant", "content": reply})
        sync_active_chat()
        return

    start_form(form_key, user_text, prefilled=fields)


def render_sidebar():
    with st.sidebar:
        st.markdown(
            f"""
            <div style="padding: 0.35rem 0 0.85rem 0;">
              <div style="font-size: 1.45rem; font-weight: 700; letter-spacing: -0.03em; color: #0f172a;">
                {APP_NAME}
              </div>
              <div style="font-size: 0.85rem; color: #64748b; margin-top: 0.15rem;">
                {APP_TAGLINE}
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if st.button("＋ New chat", use_container_width=True, type="primary"):
            new_chat()
            st.rerun()

        st.markdown(
            "<div style='margin-top: 1rem; font-size: 0.75rem; font-weight: 600; "
            "letter-spacing: 0.06em; text-transform: uppercase; color: #94a3b8;'>"
            "Chats</div>",
            unsafe_allow_html=True,
        )
        for section, section_chats in group_chats_by_recency(st.session_state.chats):
            st.markdown(
                f"<div style='margin-top: 0.65rem; margin-bottom: 0.2rem; "
                f"font-size: 0.7rem; font-weight: 600; letter-spacing: 0.05em; "
                f"text-transform: uppercase; color: #94a3b8;'>{section}</div>",
                unsafe_allow_html=True,
            )
            for chat in section_chats:
                cid = chat["id"]
                is_active = cid == st.session_state.active_chat_id
                title = chat.get("title") or "Untitled"
                if title.strip().lower() in ("new chat", ""):
                    title = "Untitled"

                if st.session_state.get("editing_chat_id") == cid:
                    new_title = st.text_input(
                        "Rename chat",
                        value=title,
                        key=f"rename_input_{cid}",
                        label_visibility="collapsed",
                    )
                    save_col, cancel_col = st.columns(2)
                    with save_col:
                        if st.button(
                            "Save",
                            key=f"rename_save_{cid}",
                            use_container_width=True,
                        ):
                            rename_chat(cid, new_title)
                            st.session_state.editing_chat_id = None
                            st.rerun()
                    with cancel_col:
                        if st.button(
                            "Cancel",
                            key=f"rename_cancel_{cid}",
                            use_container_width=True,
                        ):
                            st.session_state.editing_chat_id = None
                            st.rerun()
                    continue

                label = f"• {title}" if is_active else title
                open_col, edit_col, del_col = st.columns([0.62, 0.19, 0.19])
                with open_col:
                    if st.button(
                        label,
                        key=f"chat_{cid}",
                        use_container_width=True,
                        type="secondary",
                    ):
                        switch_chat(cid)
                        st.rerun()
                with edit_col:
                    if st.button(
                        "",
                        key=f"edit_{cid}",
                        use_container_width=True,
                        help="Rename chat",
                        icon=":material/edit:",
                    ):
                        st.session_state.editing_chat_id = cid
                        st.rerun()
                with del_col:
                    if st.button(
                        "",
                        key=f"del_{cid}",
                        use_container_width=True,
                        help="Delete chat",
                        icon=":material/delete:",
                    ):
                        delete_chat(cid)
                        st.rerun()

        st.divider()
        st.markdown(
            "<div style='font-size: 0.75rem; font-weight: 600; letter-spacing: 0.06em; "
            "text-transform: uppercase; color: #94a3b8;'>Forms</div>",
            unsafe_allow_html=True,
        )
        for form in list_forms():
            st.caption(f"• {form['form_name']}")

        form = st.session_state.form
        if form:
            st.divider()
            st.markdown(f"**{form['form_name']}**")
            total = len(form["schema"]["fields"])
            done = len(st.session_state.answers)
            st.progress(done / total if total else 0)
            st.caption(f"{done} / {total} fields")
            for field in form["schema"]["fields"]:
                mark = "✓" if field["name"] in st.session_state.answers else "○"
                st.caption(f"{mark} {field['label']}")


def render_completion_card():
    # Deprecated: Form completions are stored directly as assistant messages
    # in st.session_state.messages to preserve strict chronological order.
    pass


def handle_user_prompt(prompt):
    st.session_state.messages.append({"role": "user", "content": prompt})

    # Check for cancel / reset command
    if is_cancel_command(prompt):
        if st.session_state.form:
            clear_form_state()
            reply = "Current form filling was cancelled. Let me know whenever you'd like to fill a form or report an issue."
        else:
            reply = "Let me know whenever you'd like to fill a form or report an issue."
        st.session_state.messages.append({"role": "assistant", "content": reply})
        sync_active_chat()
        return

    form = st.session_state.form
    if form is None or st.session_state.completed:
        handle_form_selection(prompt)
        sync_active_chat()
        return

    switch_key = should_switch_to_form(prompt)
    if switch_key and switch_key != form.get("key"):
        start_form(switch_key, prompt)
        sync_active_chat()
        return

    field = st.session_state.current_field

    if field is None:
        handle_form_selection(prompt)
        sync_active_chat()
        return

    # Check if user greeted or sent smalltalk mid-form!
    if is_greeting_or_smalltalk(prompt) or any(w in prompt.lower().split() for w in ("hy", "helo", "hiii", "hey", "hello", "hi")):
        field_hint = f"{field['label']}"
        if field.get("type") == "select" and field.get("options"):
            field_hint += f" (Options: {', '.join(field['options'])})"
        elif not field.get("required", True):
            field_hint += " (Optional - type 'skip' to leave blank)"

        with st.spinner("AI is thinking..."):
            conv_reply = mfp.generate_conversational_reply(prompt)
        reply = (
            f"{conv_reply}\n\n"
            f"We are currently filling {form['form_name']}.\n\n"
            f"Please provide {field_hint} to continue, or type 'cancel' to exit."
        )
        st.session_state.messages.append({"role": "assistant", "content": reply})
        sync_active_chat()
        return

    with st.spinner("Reading your message..."):
        accepted = apply_extracted_answers(
            form["schema"], prompt, st.session_state.answers
        )

    if field["name"] not in st.session_state.answers:
        ok, value = validate_answer(field, prompt)
        if ok:
            st.session_state.answers[field["name"]] = value
            accepted[field["name"]] = value
        elif not accepted:
            st.session_state.messages.append({"role": "assistant", "content": value})
            sync_active_chat()
            return

    if len(accepted) > 1:
        st.session_state.messages.append(
            {
                "role": "assistant",
                "content": format_filled_message(
                    form["schema"], st.session_state.answers, accepted
                ),
            }
        )

    ask_next_question()
    sync_active_chat()


def main():
    st.set_page_config(page_title=APP_NAME, page_icon="💬", layout="centered")
    st.title(APP_NAME)
    st.caption("One chat can fill many forms. History stays in the left sidebar.")

    init_session()
    render_sidebar()

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])


    if st.session_state.form and st.session_state.current_field and not st.session_state.completed:
        field = st.session_state.current_field
        name_lower = field.get("name", "").lower()
        hint = f"Enter {field['label'].lower()} or paste details..."
        if "vehicle" in name_lower or "plate" in name_lower:
            hint = "e.g. MH12AB1234 (or type 'skip')..."
        elif "email" in name_lower:
            hint = "e.g. name@example.com (or type 'skip')..."
        elif field.get("type") == "select" and field.get("options"):
            hint = f"Options: {', '.join(field['options'])}"
        elif not field.get("required", True):
            hint = f'Optional {field["label"].lower()} — type "skip" to leave blank'
    elif st.session_state.completed:
        hint = "Type another form name or greeting to continue..."
    else:
        hint = "Say hello, choose a form, or paste details..."

    prompt = st.chat_input(hint)
    if not prompt:
        return

    handle_user_prompt(prompt)
    st.rerun()

if __name__ == "__main__":
    main()
    