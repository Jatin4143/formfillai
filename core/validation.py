"""
Validation and normalization for field values.
Includes date normalization, license plate checks, and type-based validations.
"""

from datetime import date, datetime, timedelta
import logging
import re

from core.intent import is_greeting_or_smalltalk

log = logging.getLogger("core.validation")

INDIAN_PLATE_PATTERN = r"^(?:[A-Z]{2}[ -]?\d{1,2}[ -]?[A-Z]{1,3}[ -]?\d{1,4}|\d{2}[ -]?BH[ -]?\d{4}[ -]?[A-HJ-NP-Z]{1,2})$"
INDIAN_PLATE_RE = re.compile(INDIAN_PLATE_PATTERN, re.IGNORECASE)
INDIAN_PLATE_EXTRACT_RE = re.compile(
    r"\b([A-Z]{2}[ -]?\d{1,2}[ -]?[A-Z]{1,3}[ -]?\d{1,4}|\d{2}[ -]?BH[ -]?\d{4}[ -]?[A-HJ-NP-Z]{1,2})\b",
    re.IGNORECASE,
)

EMAIL_RE = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
PHONE_RE = re.compile(r"^\+?[0-9\s\-()]{7,20}$")
NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")

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
    if s in ("today", "tod"):
        return today.isoformat()
    if s in ("tomorrow", "tmrw", "tom"):
        return (today + timedelta(days=1)).isoformat()
    if s in ("day after tomorrow", "day after tmrw"):
        return (today + timedelta(days=2)).isoformat()
    if s == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    if not re.search(r"\d", s):
        return None
    try:
        from dateutil import parser as dt_parser
        is_year_first = bool(re.match(r"^\d{4}[-/.]", s))
        def1 = datetime(today.year, 1, 1)
        def2 = datetime(today.year, 1, 2)
        dt1 = dt_parser.parse(s, default=def1, yearfirst=is_year_first, dayfirst=not is_year_first)
        dt2 = dt_parser.parse(s, default=def2, yearfirst=is_year_first, dayfirst=not is_year_first)
        if dt1.day != dt2.day:
            return None
        if dt1.year < 1900 or dt1.year > 2100:
            return None
        return dt1.date().isoformat()
    except Exception:
        return None


def is_valid_date(text: str) -> bool:
    """Check if the provided text is a valid date (rejects random text)."""
    return normalize_date(text) is not None


def normalize_select_answer(user_input: str, options: list[str]) -> str | None:
    """Match user answer to select options (exact, prefix, word boundary)."""
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


def validate_answer(field: dict, user_input: str) -> tuple[bool, str | None]:
    """
    Validate user input against field schema.
    Returns (is_valid, normalized_value_or_error_message).
    """
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

    # 3. Vehicle registration plate
    if "vehicle" in field_name or "plate" in field_name:
        if not required and (NO_VEHICLE_RE.search(cleaned) or cleaned.lower() in ("no", "none", "na", "n/a", "nil", "skip")):
            return True, None
        if is_valid_indian_plate(cleaned):
            return True, normalize_plate(cleaned)
        if not required:
            return False, "Please enter a valid Indian vehicle number plate (e.g. MH 12 AB 1234 or 22 BH 1234 AA), or type 'skip'."
        return False, "Please enter a valid Indian vehicle number plate (e.g. MH 12 AB 1234 or 22 BH 1234 AA)."

    # 4. Email address
    if field_type == "email" or "email" in field_name:
        if not EMAIL_RE.match(cleaned):
            return False, "Please enter a valid email address (e.g. name@example.com)."
        return True, cleaned.lower()

    # 5. Phone number
    if field_type in ("phone", "tel") or any(k in field_name for k in ("phone", "mobile", "contact")):
        if not PHONE_RE.match(cleaned):
            return False, "Please enter a valid phone number."
        return True, cleaned

    # 6. Number / numeric
    if field_type in ("number", "integer", "numeric") or any(k in field_name for k in ("age", "count", "quantity", "amount", "years")):
        if not NUMBER_RE.match(cleaned):
            return False, f"Please enter a valid numeric value for {field['label'].lower()}."
        return True, cleaned

    return True, cleaned

