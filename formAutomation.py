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
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import ollama
import streamlit as st

MODEL = "llama3.2:3b"
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://192.168.0.254:11434")
ollama_client = ollama.Client(host=OLLAMA_BASE_URL)
DB_PATH = Path(__file__).resolve().parent / "forms.db"
APP_NAME = "FieldFlow"
APP_TAGLINE = "Fill any form from chat"

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
You are a warm, courteous, and professional digital assistant for a premium residential society.

Your task: Ask ONE natural, polite, and professional question to collect the specified field from the user.

Tone & Style Guidelines:
1. Speak like a courteous front-desk concierge (use polite phrases such as "Could you please share...", "May I know...", "Kindly provide...").
2. Do NOT use dry, mechanical templates like "What is the [field]?" or "Please enter [field]".
3. Context-aware:
   - For visitor details, address them politely as a visitor or guest.
   - For flat numbers, ask which apartment or flat they are visiting or residing in.
   - For complaints, ask with empathy about the issue.
   - For personal info, ask politely for their personal or location details (name, country, state, city).
4. If options are available, present them smoothly (e.g., "Is it for Guest, Delivery, Service, or Other?").
5. If the field is optional, mention politely that they can reply with "skip" if not applicable.
6. Ask ONLY about the specified field. Keep it to one clear sentence.
7. Return ONLY valid JSON: {"question": "Your question here"}

Examples:

Field: visitor_name ("Visitor Name")
{"question": "Could you please share visitor name?"}

Field: flat_number ("Flat Number") in Visitor Entry
{"question": "May I know which flat number you will be visiting today?"}

Field: purpose ("Purpose", options: ["Guest", "Delivery", "Service", "Other"])
{"question": "Could you please let me know the purpose of your visit — is it for Guest, Delivery, Service, or Other?"}

Field: vehicle_number ("Vehicle Number", optional)
{"question": "If you arrived with a vehicle, kindly share its registration number (or feel free to reply 'skip')."}

Field: details ("Details", required) in Raise Complaint
{"question": "Could you please describe the issue you are experiencing so our maintenance team can assist you?"}

Field: urgency ("Urgency", options: ["Low", "Medium", "High"], optional)
{"question": "How urgent would you say this matter is (Low, Medium, or High)? You may also reply 'skip' to use the default."}

Field: name ("Name", required) in Personal Info
{"question": "May I know your full name, please?"}

Field: country ("Country", required) in Personal Info
{"question": "Which country do you currently reside in?"}

Field: state ("State", required) in Personal Info
{"question": "Could you please tell me which state or province you live in?"}

Field: city ("City", required) in Personal Info
{"question": "Which city are you currently located in?"}
"""

PERSONAL_INFO_SYSTEM_PROMPT = """
You are a warm, courteous, and professional digital assistant collecting Personal Information.

Your task: Ask ONE natural, polite, and professional question to collect the specified personal information field from the user.

Tone & Style Guidelines:
1. Speak like a courteous, professional assistant (use polite phrases such as "Could you please share...", "May I know...", "Kindly provide...").
2. Do NOT use dry, mechanical templates like "What is the [field]?" or "Please enter [field]".
3. Context-aware field instructions:
   - For name: Ask for their full name politely (e.g. "May I have your full name, please?").
   - For country: Ask which country they currently live or reside in (e.g. "Which country do you currently reside in?").
   - For state: Ask which state or province they are located in (e.g. "Could you please tell me which state or region you live in?").
   - For city: Ask which city or town they currently reside in (e.g. "Which city are you currently located in?").
4. If options are available, present them smoothly.
5. If the field is optional, mention politely that they can reply with "skip" if not applicable.
6. Ask ONLY about the specified field. Keep it to one clear sentence.
7. Return ONLY valid JSON: {"question": "Your question here"}

Examples:

Field: name ("Name", required) in Personal Info
{"question": "May I know your full name, please?"}

Field: country ("Country", required) in Personal Info
{"question": "Which country do you currently reside in?"}

Field: state ("State", required) in Personal Info
{"question": "Could you please tell me which state or province you live in?"}

Field: city ("City", required) in Personal Info
{"question": "Which city are you currently located in?"}
"""

FORM_SYSTEM_PROMPTS = {
    "personal_info": PERSONAL_INFO_SYSTEM_PROMPT,
}



EXTRACT_SYSTEM_PROMPT = """
You extract form fields from the user's message.

RULES:
1. Return ONLY valid JSON. No markdown. No extra text.
2. Always wrap answers under the key "fields".
3. Use ONLY the field names given in the schema.
4. Extract ONLY information explicitly provided by the user
   in the current message. Never infer, invent, expand,
   rewrite, or generate missing field values.
5. If a field is missing or unclear, use null.
6. For select fields, return exactly one option string from the schema list, or null.
   Map natural language (e.g. urgent -> High, courier/amazon/package -> Delivery, friend -> Guest, plumber/pipe/leak/tap -> Water, lift/elevator -> Lift, electric/power/light -> Electricity).
7. For textarea, details, and description fields:
   Return the user's explicitly provided description only.
   If the user has not provided a description, return null.
   Never create a description from the subject or context.
8. For subject fields:
   Return only the subject explicitly provided by the user.
   Do not create, improve, or expand the subject.
   Return null if no subject was provided.
9. Never return "skip". Use null instead.
10. Do not add extra keys outside "fields".
11. Never put a full paragraph into flat_number or visitor_name.
    flat_number must look like A-110 or B-204, or null.
    visitor_name must be a short person/service name (e.g. Amazon courier), or null.
12. Meeting / announcement / "please come" / all wings → notice subject + description, not a complaint.
13. Words like issue/problem do not force a complaint if the message is mainly a meeting or announcement.
14. For vehicle_number, extract registration plate numbers or bike/car numbers (e.g. MH12 AB 1234), or null.
15. For Personal Info fields (name, country, state, city):
    - name: extract person's full name (e.g. John Doe, Priya Sharma), or null.
    - country: extract country name (e.g. India, United States), or null.
    - state: extract state or province name (e.g. Maharashtra, California), or null.
    - city: extract city or town name (e.g. Mumbai, New York), or null.

Correct example for Visitor Entry:
{"fields": {"visitor_name": "Amazon courier", "flat_number": "A-110", "purpose": "Delivery", "vehicle_number": "MH12 AB 1234"}}

Correct example for Personal Info:
{"fields": {"name": "Priya Sharma", "country": "India", "state": "Maharashtra", "city": "Pune"}}

Wrong (do not do this):
{"visitor_name": "Amazon courier"}
{"fields": {}}

Response format:
{"fields": {"field_name": "value or null"}}
"""

CLASSIFY_SYSTEM_PROMPT = """
You choose one form and extract its fields from the user's message.
Do not look for a form name in the message. Decide from what the message is about.

RULES:
1. Return ONLY valid JSON. No markdown. No extra text.
2. "form" must be exactly one form key from the list, or null if the message is not clearly one of those forms.
3. "fields" uses ONLY field names from the chosen form. Use null when a value is missing or unclear.
4. Use only information from the user's message. Never invent facts.
5. For select fields, return exactly one option string from that field's options, or null.
   Map natural language (e.g. urgent -> High, b side / b wing -> B Wing, courier/amazon/package -> Delivery, friend -> Guest, plumber/pipe/leak/tap -> Water, lift/elevator -> Lift, electric/power/light -> Electricity).
6. For textarea / details / description, write a short factual summary, or null.
7. For subject, write a short title (3-8 words) when clear, or null.
8. Never return "skip". Use null instead.
9. Never put a full paragraph into flat_number or visitor_name.
   flat_number must look like A-110 or B-204, or null.
   visitor_name must be a short person/service name (e.g. Amazon courier), or null.
10. A gathering, invitation, meeting, or announcement for residents or wings is add_notice.
11. A person or service coming to a flat is visitor_entry.
12. Something broken or needing repair in a flat or building (e.g. lift stuck, water leaking, electrical issue) is raise_complaint.
    Words like issue or problem do not force a complaint when the message is mainly a meeting or announcement.
13. Greetings and unclear messages use form null and fields {}.
14. For vehicle_number, extract vehicle registration plate numbers or bike/scooter/car numbers (e.g. MH14 DE 5678, MH12 AB 1234), or null.
15. Personal details, registration, onboarding, or bio information (e.g. name, country, state, city) is personal_info.

Example (visitor / delivery with vehicle):
{"form": "visitor_entry", "fields": {"visitor_name": "Mehul Sharma", "flat_number": "C-502", "purpose": "Delivery", "vehicle_number": "MH14 DE 5678"}}

Example (invitation, no form name in the message):
{"form": "add_notice", "fields": {"wing": "B Wing", "subject": "Diwali Get Together", "description": "Diwali get-together this Sunday evening in the clubhouse for B side residents. Please join with family."}}

Example (personal info):
{"form": "personal_info", "fields": {"name": "Rohan Mehta", "country": "India", "state": "Gujarat", "city": "Ahmedabad"}}

Example (not a form):
{"form": null, "fields": {}}

Response format:
{"form": "<form key or null>", "fields": {"field_name": "value or null"}}
"""

HELP_SYSTEM_PROMPT = f"""
You are {APP_NAME}, a friendly and helpful AI form-filling assistant.

RULES:
1. Greet politely and naturally.
2. Clearly explain that you can help them fill out community forms.
3. Guide the user to select one of the available forms or provide details.
4. Keep replies clear, warm, and concise. Plain text only.
"""

FORM_METADATA_HELP = {
    "visitor_entry": {
        "desc": "Guests, deliveries & service",
    },
    "raise_complaint": {
        "desc": "Maintenance & repairs",
    },
    "add_notice": {
        "desc": "Society announcements",
    },
    "personal_info": {
        "desc": "Personal profile & contact details",
    },
}

INDIAN_PLATE_PATTERN = r"^[A-Z]{2}[ -]?\d{1,2}[ -]?[A-Z]{1,3}[ -]?\d{1,4}$"
INDIAN_PLATE_RE = re.compile(INDIAN_PLATE_PATTERN, re.IGNORECASE)
INDIAN_PLATE_EXTRACT_RE = re.compile(r"\b([A-Z]{2}[ -]?\d{1,2}[ -]?[A-Z]{1,3}[ -]?\d{1,4})\b", re.IGNORECASE)

EMAIL_PATTERN = r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"
EMAIL_RE = re.compile(EMAIL_PATTERN)
EMAIL_EXTRACT_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

MOBILE_PATTERN = r"^(?:\+?\d{1,3}[\s-]?)?(?:\(?\d{2,4}\)?[\s-]?)?\d{3,4}[\s-]?\d{3,4}$"
MOBILE_EXTRACT_RE = re.compile(r"(?:(?:\+|00)\d{1,3}[\s-]?)?(?:\(?\d{2,4}\)?[\s-]?)?\d{3,4}[\s-]?\d{3,4}\b")

# ---------------------------------------------------------------------------
# Free API Integration: Countries, States & Cities (CountriesNow API)
# ---------------------------------------------------------------------------
COUNTRIES_API_BASE = "https://countriesnow.space/api/v0.1/countries"

FALLBACK_COUNTRIES = [
    "Afghanistan", "Albania", "Algeria", "Argentina", "Australia", "Austria",
    "Bangladesh", "Belgium", "Brazil", "Canada", "China", "Colombia",
    "Denmark", "Egypt", "Finland", "France", "Germany", "Greece",
    "Hong Kong", "India", "Indonesia", "Ireland", "Israel", "Italy",
    "Japan", "Kenya", "Malaysia", "Mexico", "Nepal", "Netherlands",
    "New Zealand", "Nigeria", "Norway", "Pakistan", "Philippines", "Poland",
    "Portugal", "Qatar", "Russia", "Saudi Arabia", "Singapore", "South Africa",
    "South Korea", "Spain", "Sri Lanka", "Sweden", "Switzerland", "Thailand",
    "Turkey", "Ukraine", "United Arab Emirates", "United Kingdom", "United States", "Vietnam"
]

FALLBACK_STATES = {
    "India": [
        "Andaman and Nicobar Islands", "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar",
        "Chandigarh", "Chhattisgarh", "Delhi", "Goa", "Gujarat", "Haryana",
        "Himachal Pradesh", "Jammu and Kashmir", "Jharkhand", "Karnataka", "Kerala",
        "Ladakh", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya", "Mizoram",
        "Nagaland", "Odisha", "Puducherry", "Punjab", "Rajasthan", "Sikkim",
        "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal"
    ],
    "United States": [
        "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
        "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
        "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
        "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
        "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey",
        "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio", "Oklahoma",
        "Oregon", "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
        "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
        "West Virginia", "Wisconsin", "Wyoming"
    ],
}

COUNTRY_ALIASES = {
    "us": "United States",
    "usa": "United States",
    "u.s.": "United States",
    "u.s.a.": "United States",
    "america": "United States",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "britain": "United Kingdom",
    "great britain": "United Kingdom",
    "uae": "United Arab Emirates",
    "u.a.e.": "United Arab Emirates",
    "emirates": "United Arab Emirates",
    "in": "India",
    "bharat": "India",
}


@st.cache_data(ttl=86400, show_spinner=False)
def get_countries():
    """Fetch all countries from free CountriesNow API with fallback."""
    url = f"{COUNTRIES_API_BASE}/iso"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode())
            if not data.get("error") and data.get("data"):
                return sorted([item["name"] for item in data["data"] if item.get("name")])
    except Exception as exc:
        log.warning("Countries API failed (%s), using fallback list.", exc)
    return sorted(FALLBACK_COUNTRIES)


@st.cache_data(ttl=86400, show_spinner=False)
def get_states(country):
    """Fetch state list for a country from free CountriesNow API with fallback."""
    if not country:
        return []
    country_resolved = COUNTRY_ALIASES.get(country.strip().lower(), country.strip())
    url = f"{COUNTRIES_API_BASE}/states/q?country={urllib.parse.quote(country_resolved)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode())
            if not data.get("error") and data.get("data") and "states" in data["data"]:
                states = [s["name"] for s in data["data"]["states"] if s.get("name")]
                if states:
                    return sorted(states)
    except Exception as exc:
        log.warning("States API for %s failed (%s), using fallback.", country_resolved, exc)
    return sorted(FALLBACK_STATES.get(country_resolved, []))


@st.cache_data(ttl=86400, show_spinner=False)
def get_cities(country, state):
    """Fetch city list for a state and country from free CountriesNow API."""
    if not country or not state:
        return []
    country_resolved = COUNTRY_ALIASES.get(country.strip().lower(), country.strip())
    url = f"{COUNTRIES_API_BASE}/state/cities/q?country={urllib.parse.quote(country_resolved)}&state={urllib.parse.quote(state.strip())}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode())
            if not data.get("error") and data.get("data"):
                cities = [c for c in data["data"] if isinstance(c, str) and c.strip()]
                if cities:
                    return sorted(cities)
    except Exception as exc:
        log.warning("Cities API for %s, %s failed (%s).", state, country_resolved, exc)
    return []


def normalize_location_answer(user_input, valid_options):
    """Normalize user input against valid API options (case-insensitive & prefix match)."""
    if not user_input or not valid_options:
        return None
    cleaned = user_input.strip()
    if not cleaned:
        return None

    for opt in valid_options:
        if cleaned.lower() == opt.lower():
            return opt

    for opt in valid_options:
        if opt.lower().startswith(cleaned.lower()):
            return opt

    return None




def normalize_plate(text: str) -> str:
    """Normalize and format Indian vehicle number plate to uppercase."""
    return re.sub(r"\s+", " ", text.strip().upper())


def is_valid_indian_plate(text: str) -> bool:
    """Validate vehicle registration plate against Indian number plate format."""
    if not text:
        return False
    return bool(INDIAN_PLATE_RE.match(text.strip()))

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

DOMAIN_INTENT_WORDS = {
    "flat", "wing", "room", "floor", "water", "leak", "tap", "pipe", "plumber",
    "electric", "power", "light", "lift", "elevator", "parking", "slot", "car", "van",
    "visitor", "guest", "delivery", "parcel", "courier", "service", "technician",
    "complaint", "issue", "problem", "broken", "repair", "notice", "meeting", "announcement",
    "gathering", "urgent", "emergency",
    "personal", "profile", "contact", "email", "mobile", "phone", "country", "state", "city",
    "name", "address"
}


def is_greeting_or_smalltalk(text: str) -> bool:
    """Return True if the text is purely a greeting or smalltalk, without domain keywords."""
    if not text:
        return False
    cleaned = text.strip().lower()
    if not cleaned:
        return False

    words = set(re.findall(r"\b[a-z0-9_-]+\b", cleaned))
    if any(w in DOMAIN_INTENT_WORDS for w in words):
        return False

    if GREETING_RE.search(cleaned) or SMALLTALK_RE.search(cleaned):
        return True

    if re.fullmatch(r"(ok|okay|cool|nice|great|fine|thanks|thank\s*you|thx|sure)[!.]*", cleaned):
        return True

    return False


def is_cancel_command(text: str) -> bool:
    """Return True if the user asks to cancel the active form."""
    if not text:
        return False
    return bool(CANCEL_RE.match(text.strip().lower()))


def build_greeting_reply(forms, user_text=None):
    """Return a short, clean greeting with step-by-step form options (no icons)."""
    greeting_prefix = "Hello! I can help you fill out the following forms:\n"
    if user_text:
        lower = user_text.lower()
        if "morning" in lower:
            greeting_prefix = "Good morning! I can help you fill out the following forms:\n"
        elif "evening" in lower:
            greeting_prefix = "Good evening! I can help you fill out the following forms:\n"
        elif "afternoon" in lower:
            greeting_prefix = "Good afternoon! I can help you fill out the following forms:\n"

    lines = [greeting_prefix]
    for idx, f in enumerate(forms, 1):
        meta = FORM_METADATA_HELP.get(f["key"], {})
        desc = meta.get("desc", "")
        desc_str = f" - {desc}" if desc else ""
        lines.append(f"{idx}. {f['form_name']}{desc_str}")

    lines.append("\nPlease reply with the form name you want to fill.")
    return "\n".join(lines)


def build_form_overview_message(form, accepted=None):
    """Produce a short, clean overview of the form fields (no icons)."""
    schema = form["schema"]
    form_name = form["form_name"]

    lines = [
        f"Let's fill out {form_name}.\n",
        "Details needed:",
    ]

    for idx, field in enumerate(schema["fields"], 1):
        req_tag = "Required" if field.get("required", True) else "Optional"
        options = field.get("options")
        if options and field.get("type") == "select":
            opts_str = f" ({', '.join(options)})"
        elif field.get("name") == "vehicle_number":
            opts_str = " (e.g. MH12AB1234)"
        else:
            opts_str = ""
        lines.append(f"{idx}. {field['label']} [{req_tag}]{opts_str}")

    lines.append("")
    lines.append("You can provide all these details in one message, or answer them one by one.")

    if accepted:
        lines.append("")
        lines.append("---")
        lines.append("Captured from your message:")
        for field in schema["fields"]:
            if field["name"] in accepted:
                val = accepted[field["name"]]
                if val is None:
                    val = "Skipped"
                lines.append(f"- {field['label']}: {val}")

        remaining = [f['label'] for f in schema['fields'] if f['name'] not in accepted]
        if remaining:
            lines.append("")
            lines.append(f"Remaining: {', '.join(remaining)}")

    return "\n".join(lines)


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
            conn.executescript("""
                INSERT OR IGNORE INTO forms (key, name, aliases) VALUES
                ('add_notice', 'Add Notice',
                 '["add notice", "notice", "new notice", "create notice"]'),
                ('visitor_entry', 'Visitor Entry',
                 '["visitor entry", "visitor", "guest", "add visitor", "visitor pass"]'),
                ('raise_complaint', 'Raise Complaint',
                 '["raise complaint", "complaint", "issue", "report issue", "problem"]'),
                ('personal_info', 'Personal Info',
                 '["personal info", "personal information", "personal details", "profile", "contact info", "contact details"]');

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

                -- Personal Info
                DELETE FROM form_fields
                WHERE form_id = (SELECT id FROM forms WHERE key = 'personal_info')
                  AND name IN ('email', 'mobile');

                INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                SELECT id, 'name', 'Name', 'text', 1, NULL, 1
                FROM forms WHERE key = 'personal_info';

                INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                SELECT id, 'country', 'Country', 'text', 1, NULL, 2
                FROM forms WHERE key = 'personal_info';

                INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                SELECT id, 'state', 'State', 'text', 1, NULL, 3
                FROM forms WHERE key = 'personal_info';

                INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                SELECT id, 'city', 'City', 'text', 1, NULL, 4
                FROM forms WHERE key = 'personal_info';
            """)
    except sqlite3.Error as exc:
        log.error("Failed to initialize database: %s", exc)
    finally:
        conn.close()


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
        "system_prompt": FORM_SYSTEM_PROMPTS.get(form_key, GENERIC_SYSTEM_PROMPT),
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

def detect_form_key(user_text):
    if is_greeting_or_smalltalk(user_text):
        return None
    text = user_text.strip().lower()
    forms = list_forms()

    for form in forms:
        if text == form["form_name"].lower() or text == form["key"]:
            return form["key"]

    for form in forms:
        for alias in form["aliases"]:
            pattern = r'\b' + re.escape(alias.lower()) + r'\b'
            if re.search(pattern, text):
                return form["key"]

    return None


def message_is_only_form_name(user_text, form):
    """True when the message only picks a form and has no field values."""
    text = user_text.strip().lower().strip(" .,!?:;\"'")
    names = [form["form_name"], form["key"], *form.get("aliases", [])]
    return any(text == str(name).strip().lower() for name in names)


def get_next_field(schema, answers):
    for field in schema["fields"]:
        if field["name"] not in answers:
            return field
    return None


FALLBACK_QUESTIONS = {
    "name": "What is your full name?",
    "country": "Which country do you live in?",
    "state": "Which state do you live in?",
    "city": "Which city do you live in?",
}


def build_fallback_question(field):
    name = field.get("name")
    if name == "country":
        return "Which country do you currently reside in? (Select from the options above or type below)"
    if name == "state":
        country = st.session_state.answers.get("country")
        if country:
            return f"Which state or province in {country} do you live in? (Select above or type below)"
        return "Which state or province do you live in? (Select above or type below)"
    if name == "city":
        state = st.session_state.answers.get("state")
        if state:
            return f"Which city in {state} do you live in? (Select above or type below)"
        return "Which city do you live in? (Select above or type below)"

    if name in FALLBACK_QUESTIONS and not field.get("options"):
        q = FALLBACK_QUESTIONS[name]
        if not field.get("required", True):
            q += ' (or type "skip")'
        return q

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
    field_copy = dict(field)
    fname = field.get("name")
    if fname == "state":
        country = st.session_state.answers.get("country")
        if country:
            field_copy["context"] = f"User is located in country: {country}"
    elif fname == "city":
        country = st.session_state.answers.get("country")
        state = st.session_state.answers.get("state")
        ctx = []
        if country:
            ctx.append(f"country: {country}")
        if state:
            ctx.append(f"state: {state}")
        if ctx:
            field_copy["context"] = f"User is located in {', '.join(ctx)}"

    prompt = f"""
{form['form_name']} form.

Ask ONE simple question for this field only:

{json.dumps(field_copy, indent=2)}

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
    cleaned = user_input.strip()
    if not cleaned:
        return None

    for opt in options:
        if cleaned.lower() == opt.lower():
            return opt

    for opt in options:
        short = opt.lower().replace(" wing", "")
        if cleaned.lower() == short:
            return opt

    return None


def validate_answer(field, user_input):
    options = field.get("options") or []
    required = field.get("required", True)
    cleaned = user_input.strip()

    if not cleaned:
        return False, "Please enter a value."

    if not required and cleaned.lower() == "skip":
        return True, None

    if cleaned.lower() in ("null", "none", "nil", "undefined"):
        return False, f"Please enter a valid {field['label'].lower()}."

    # Guard against treating greetings or smalltalk as field answers
    if is_greeting_or_smalltalk(cleaned):
        return False, f"That looks like a greeting. We are currently filling out **{field['label']}**. Please provide your {field['label'].lower()} (or type 'cancel' to exit)."

    # Indian number plate validation for vehicle_number
    if field.get("name") == "vehicle_number":
        if not required and (NO_VEHICLE_RE.search(cleaned) or cleaned.lower() in ("no", "none", "na", "n/a", "nil", "skip")):
            return True, None
        if not is_valid_indian_plate(cleaned):
            return False, "Please enter a valid Indian number plate (e.g. MH12AB1234 or GJ-01-CD-5678), or type 'skip'."
        return True, normalize_plate(cleaned)

    # Email validation for email field
    if field.get("name") == "email":
        if not EMAIL_RE.match(cleaned):
            return False, "Please enter a valid email address (e.g. name@example.com)."
        return True, cleaned.lower()

    # Mobile number validation for mobile field
    if field.get("name") == "mobile":
        digits = re.sub(r"\D", "", cleaned)
        if len(digits) < 7 or len(digits) > 15:
            return False, "Please enter a valid mobile number (e.g. +91 9876543210 or 9876543210)."
        return True, cleaned

    # Country validation with API data
    if field.get("name") == "country":
        alias = COUNTRY_ALIASES.get(cleaned.lower())
        if alias:
            return True, alias
        countries = get_countries()
        matched = normalize_location_answer(cleaned, countries)
        if matched:
            return True, matched
        if len(cleaned) >= 2 and re.match(r"^[A-Za-z\s\.\-']+$", cleaned):
            return True, cleaned.title()
        return False, "Please enter or select a valid country name."

    # State validation with API data
    if field.get("name") == "state":
        country = st.session_state.answers.get("country", "")
        states = get_states(country) if country else []
        if states:
            matched = normalize_location_answer(cleaned, states)
            if matched:
                return True, matched
        if len(cleaned) >= 2 and re.match(r"^[A-Za-z0-9\s\.\-']+$", cleaned):
            return True, cleaned.title()
        return False, f"Please enter or select a valid state{' in ' + country if country else ''}."

    # City validation with API data
    if field.get("name") == "city":
        country = st.session_state.answers.get("country", "")
        state = st.session_state.answers.get("state", "")
        cities = get_cities(country, state) if (country and state) else []
        if cities:
            matched = normalize_location_answer(cleaned, cities)
            if matched:
                return True, matched
        if len(cleaned) >= 2 and re.match(r"^[A-Za-z0-9\s\.\-']+$", cleaned):
            return True, cleaned.title()
        return False, f"Please enter or select a valid city{' in ' + state if state else ''}."

    if field.get("type") == "select" and options:
        matched = normalize_select_answer(cleaned, options)
        if matched is None:
            return False, f"Please choose one of: {', '.join(options)}"
        return True, matched

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


def extract_fields_from_text(form_name, user_text, fields):
    """Return {field_name: text} for values the model found in the message."""
    if not fields or not user_text.strip():
        return {}

    spec = _field_spec(fields)

    prompt = f"""
Form: {form_name}

Fields:
{json.dumps(spec, indent=2)}

User message:
{user_text}

Return JSON:
{{"fields": {{"<field name>": "<value or null>"}}}}
"""

    try:
        response = ollama_client.chat(
            model=MODEL,
            format="json",
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        result = json.loads(response["message"]["content"])
        raw = result.get("fields") or {}
    except Exception as exc:
        log.error("EXTRACT failed | form=%s error=%s", form_name, exc)
        return {}

    if not isinstance(raw, dict):
        log.warning("EXTRACT done | form=%s invalid payload=%r", form_name, raw)
        return {}

    allowed = {field["name"] for field in fields}
    extracted = {}
    for name, value in raw.items():
        if name not in allowed:
            continue
        text = _as_text(value)
        if text is not None:
            extracted[name] = text

    if "vehicle_number" in allowed:
        if NO_VEHICLE_RE.search(user_text):
            extracted["vehicle_number"] = "skip"
        elif not extracted.get("vehicle_number"):
            veh_match = INDIAN_PLATE_EXTRACT_RE.search(user_text)
            if veh_match:
                extracted["vehicle_number"] = normalize_plate(veh_match.group(1))

    if "email" in allowed and not extracted.get("email"):
        em_match = EMAIL_EXTRACT_RE.search(user_text)
        if em_match:
            extracted["email"] = em_match.group(0).lower()

    if "mobile" in allowed and not extracted.get("mobile"):
        mob_matches = MOBILE_EXTRACT_RE.findall(user_text)
        for mob in mob_matches:
            if len(re.sub(r"\D", "", mob)) >= 7:
                extracted["mobile"] = mob.strip()
                break

    log.info("EXTRACT done | form=%s raw=%s", form_name, extracted)
    return extracted


def accept_extracted_value(field, raw):
    """Accept model extraction when it passes basic field validation."""
    if raw is None:
        return False, None
    if not isinstance(raw, str):
        return False, None
    cleaned = raw.strip()

    if field.get("name") == "vehicle_number":
        if not field.get("required", True) and (
            cleaned.lower() in ("skip", "none", "nil", "n/a")
            or NO_VEHICLE_RE.search(cleaned)
        ):
            return True, None
        if is_valid_indian_plate(cleaned):
            return True, normalize_plate(cleaned)
        return False, None

    if cleaned.lower() in ("skip", "null", "none", "nil", "n/a", "undefined"):
        if not field.get("required", True):
            return True, None
        return False, None

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

Return JSON:
{{"form": "<form key or null>", "fields": {{"<field name>": "<value or null>"}}}}
"""

    try:
        response = ollama_client.chat(
            model=MODEL,
            format="json",
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

    if "vehicle_number" in allowed:
        if NO_VEHICLE_RE.search(user_text):
            extracted["vehicle_number"] = "skip"
        elif not extracted.get("vehicle_number"):
            veh_match = INDIAN_PLATE_EXTRACT_RE.search(user_text)
            if veh_match:
                extracted["vehicle_number"] = normalize_plate(veh_match.group(1))

    if "email" in allowed and not extracted.get("email"):
        em_match = EMAIL_EXTRACT_RE.search(user_text)
        if em_match:
            extracted["email"] = em_match.group(0).lower()

    if "mobile" in allowed and not extracted.get("mobile"):
        mob_matches = MOBILE_EXTRACT_RE.findall(user_text)
        for mob in mob_matches:
            if len(re.sub(r"\D", "", mob)) >= 7:
                extracted["mobile"] = mob.strip()
                break

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

    current = st.session_state.form
    if current is None or st.session_state.completed:
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
    elif user_text and not message_is_only_form_name(user_text, form) and not is_greeting_or_smalltalk(user_text):
        with st.spinner("Reading your message..."):
            accepted = apply_extracted_answers(
                form["schema"], user_text, st.session_state.answers
            )

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
    """Natural chatbot reply when no form is selected yet."""
    if is_greeting_or_smalltalk(user_text):
        return build_greeting_reply(forms, user_text=user_text)

    names = [form["form_name"] for form in forms] or ["(none yet)"]
    prompt = f"""
Available forms: {", ".join(names)}

User said: {user_text}

Reply politely, acknowledge what they asked, and guide them to pick one of the available forms or provide details.
"""
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
            if not any(f["form_name"].lower() in text.lower() for f in forms):
                text += "\n\n" + build_greeting_reply(forms)
            return text
    except Exception as exc:
        log.error("HELP reply failed | error=%s", exc)

    return build_greeting_reply(forms, user_text=user_text)


def handle_form_selection(user_text):
    if is_greeting_or_smalltalk(user_text):
        reply = build_greeting_reply(list_forms(), user_text=user_text)
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
            reply = (
                "Current form has been cancelled.\n\n"
                + build_greeting_reply(list_forms())
            )
        else:
            reply = build_greeting_reply(list_forms())
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
    if is_greeting_or_smalltalk(prompt):
        field_hint = f"{field['label']}"
        if field.get("type") == "select" and field.get("options"):
            field_hint += f" (Options: {', '.join(field['options'])})"
        elif not field.get("required", True):
            field_hint += " (Optional - type 'skip' to leave blank)"

        reply = (
            f"Hello! We are currently filling {form['form_name']}.\n\n"
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


def render_field_selector(field):
    """Render interactive selection UI for country, state, city, or select fields."""
    fname = field.get("name")

    if fname == "country":
        countries = get_countries()
        quick_countries = ["India", "United States", "United Kingdom", "Canada", "Australia", "UAE", "Germany"]

        st.markdown(
            "<div style='margin-top: 0.6rem; margin-bottom: 0.4rem; font-size: 0.85rem; font-weight: 600; color: #334155;'>"
            "🌍 Select Country (Free API):"
            "</div>",
            unsafe_allow_html=True,
        )
        q_cols = st.columns(len(quick_countries))
        for idx, qc in enumerate(quick_countries):
            with q_cols[idx]:
                if st.button(qc, key=f"quick_c_{qc}", use_container_width=True):
                    resolved = COUNTRY_ALIASES.get(qc.lower(), qc)
                    handle_user_prompt(resolved)
                    st.rerun()

        sel_col, btn_col = st.columns([0.76, 0.24])
        with sel_col:
            chosen = st.selectbox(
                "Search or select country",
                options=[""] + countries,
                format_func=lambda x: "🔍 Search all 220+ countries..." if not x else x,
                key="sel_country_dropdown",
                label_visibility="collapsed",
            )
        with btn_col:
            if st.button("Confirm", key="btn_confirm_country", type="primary", use_container_width=True, disabled=not chosen):
                handle_user_prompt(chosen)
                st.rerun()

    elif fname == "state":
        country = st.session_state.answers.get("country", "")
        states = get_states(country) if country else []
        country_label = f" in {country}" if country else ""

        st.markdown(
            f"<div style='margin-top: 0.6rem; margin-bottom: 0.4rem; font-size: 0.85rem; font-weight: 600; color: #334155;'>"
            f"📍 Select State/Province{country_label}:"
            f"</div>",
            unsafe_allow_html=True,
        )

        if states:
            if len(states) <= 8:
                q_cols = st.columns(min(len(states), 4))
                for idx, qs in enumerate(states):
                    with q_cols[idx % 4]:
                        if st.button(qs, key=f"quick_s_{qs}_{idx}", use_container_width=True):
                            handle_user_prompt(qs)
                            st.rerun()

            sel_col, btn_col = st.columns([0.76, 0.24])
            with sel_col:
                chosen = st.selectbox(
                    "Search or select state",
                    options=[""] + states,
                    format_func=lambda x: f"🔍 Search states{country_label}..." if not x else x,
                    key="sel_state_dropdown",
                    label_visibility="collapsed",
                )
            with btn_col:
                if st.button("Confirm", key="btn_confirm_state", type="primary", use_container_width=True, disabled=not chosen):
                    handle_user_prompt(chosen)
                    st.rerun()
        else:
            st.caption(f"Tip: Enter your state or province name in the chat below.")

    elif fname == "city":
        country = st.session_state.answers.get("country", "")
        state = st.session_state.answers.get("state", "")
        cities = get_cities(country, state) if (country and state) else []
        state_label = f" in {state}" if state else ""

        st.markdown(
            f"<div style='margin-top: 0.6rem; margin-bottom: 0.4rem; font-size: 0.85rem; font-weight: 600; color: #334155;'>"
            f"🏙️ Select City{state_label}:"
            f"</div>",
            unsafe_allow_html=True,
        )

        if cities:
            sel_col, btn_col = st.columns([0.76, 0.24])
            with sel_col:
                chosen = st.selectbox(
                    "Search or select city",
                    options=[""] + cities,
                    format_func=lambda x: f"🔍 Search cities{state_label}..." if not x else x,
                    key="sel_city_dropdown",
                    label_visibility="collapsed",
                )
            with btn_col:
                if st.button("Confirm", key="btn_confirm_city", type="primary", use_container_width=True, disabled=not chosen):
                    handle_user_prompt(chosen)
                    st.rerun()
        else:
            st.caption(f"Tip: Enter your city name in the chat below.")

    elif field.get("type") == "select" and field.get("options"):
        opts = field["options"]
        st.markdown(
            f"<div style='margin-top: 0.6rem; margin-bottom: 0.4rem; font-size: 0.85rem; font-weight: 600; color: #334155;'>"
            f"Choose {field['label']}:"
            f"</div>",
            unsafe_allow_html=True,
        )
        cols = st.columns(min(len(opts), 5))
        for idx, opt in enumerate(opts):
            with cols[idx % min(len(opts), 5)]:
                if st.button(opt, key=f"quick_opt_{opt}_{idx}", use_container_width=True):
                    handle_user_prompt(opt)
                    st.rerun()


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
        render_field_selector(field)

        hint = "Your answer, or paste the remaining details..."
        if field.get("name") == "vehicle_number":
            hint = "e.g. MH12AB1234 or GJ-01-CD-5678 (or type 'skip')..."
        elif field.get("name") == "country":
            hint = "Select from above or type country (e.g. India, United States)..."
        elif field.get("name") == "state":
            hint = "Select from above or type state (e.g. Maharashtra, California)..."
        elif field.get("name") == "city":
            hint = "Select from above or type city (e.g. Mumbai, New York)..."
        elif field.get("type") == "select" and field.get("options"):
            hint = f"Options: {', '.join(field['options'])}"
        elif not field.get("required", True):
            hint = 'Optional — type "skip" to leave blank'
    elif st.session_state.completed:
        hint = "Type another form name or greeting to continue..."
    else:
        hint = "Say hello, choose a form, or paste details (e.g. 'water leak in B-204')..."

    prompt = st.chat_input(hint)
    if not prompt:
        return

    handle_user_prompt(prompt)
    st.rerun()


if __name__ == "__main__":
    main()