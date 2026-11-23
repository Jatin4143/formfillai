"""
Extraction engine for FieldFlow.
Combines fast regex/heuristics with LLM extraction for slot filling.
"""

import json
import logging
import re

from config import MODEL, ollama_client
from core.db import list_forms, fetch_form_schema
from core.intent import is_greeting_or_smalltalk
from core.prompts import EXTRACT_SYSTEM_PROMPT, CLASSIFY_SYSTEM_PROMPT
from core.validation import (
    validate_answer,
    normalize_plate,
    INDIAN_PLATE_EXTRACT_RE,
    NO_VEHICLE_RE,
)

log = logging.getLogger("core.extractor")


def get_next_field(schema: dict, answers: dict) -> dict | None:
    """Return the next unfilled (or missing required) field definition."""
    answers_dict = answers or {}
    for field in schema["fields"]:
        fname = field["name"]
        if fname not in answers_dict:
            return field
        val = answers_dict[fname]
        if field.get("required", True) and (val is None or str(val).strip() == ""):
            return field
    return None


def is_all_filled(schema: dict, answers: dict) -> bool:
    """True if all required fields are filled."""
    return get_next_field(schema, answers) is None


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


def format_bullet_spec(fields: list[dict]) -> str:
    """Render field definitions into bullet points for LLM prompts."""
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


def extract_heuristic_fields(fields: list[dict], user_text: str) -> dict:
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

        # Textarea / multi-line fields
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


def extract_fields_from_text(form_name: str, user_text: str, fields: list[dict]) -> dict:
    """Return {field_name: text} for values found in message via hybrid (heuristic + LLM) extraction."""
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


def accept_extracted_value(field: dict, raw: str) -> tuple[bool, str | None]:
    """Accept model extraction when it passes basic field validation."""
    if raw is None or not isinstance(raw, str):
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


def merge_extracted_fields(schema: dict, extracted: dict, answers: dict) -> dict:
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


def apply_extracted_answers(schema: dict, user_text: str, answers: dict) -> dict:
    """Extract from the message, then keep values that pass validation."""
    pending = [field for field in schema["fields"] if field["name"] not in answers]
    extracted = extract_fields_from_text(schema["form_name"], user_text, pending)
    return merge_extracted_fields(schema, extracted, answers)


def classify_and_extract(user_text: str) -> tuple[str | None, dict]:
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


def build_final_data(schema: dict, answers: dict) -> dict:
    """Format final dictionary ready for submission."""
    final_data = {"form": schema["form_name"]}
    for field in schema["fields"]:
        final_data[field["name"]] = answers.get(field["name"])
    return final_data

