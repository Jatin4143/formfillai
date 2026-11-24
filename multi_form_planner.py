
"""
Multi-Form Planner & Batch Extraction Engine.

Handles generic, schema-driven multi-record and mixed-form extraction.
Works for ANY form defined in forms.db with zero hardcoded form keys or aliases.
Uses Ollama (MODEL from .env, default llama3.2:1b).
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import uuid

import ollama
from config import MODEL, OLLAMA_BASE_URL, DB_PATH, ollama_client

INDIAN_PLATE_PATTERN = r"^(?:[A-Z]{2}[ -]?\d{1,2}[ -]?[A-Z]{1,3}[ -]?\d{1,4}|\d{2}[ -]?BH[ -]?\d{4}[ -]?[A-HJ-NP-Z]{1,2})$"
INDIAN_PLATE_RE = re.compile(INDIAN_PLATE_PATTERN, re.IGNORECASE)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("multi_form_planner")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_live_catalog():
    """
    Dynamically loads all forms and fields directly from SQLite.
    No aliases needed — the LLM relies purely on form names and field labels.
    """
    conn = get_connection()
    try:
        forms = conn.execute("SELECT id, key, name FROM forms ORDER BY id ASC").fetchall()
        catalog = []
        for f in forms:
            field_rows = conn.execute(
                """
                SELECT name, label, type, required, options
                FROM form_fields
                WHERE form_id = ?
                ORDER BY sort_order ASC, id ASC
                """,
                (f["id"],),
            ).fetchall()

            fields = []
            for row in field_rows:
                field_obj = {
                    "name": row["name"],
                    "label": row["label"],
                    "type": row["type"],
                    "required": bool(row["required"]),
                }
                if row["options"]:
                    try:
                        field_obj["options"] = json.loads(row["options"])
                    except Exception:
                        field_obj["options"] = None
                fields.append(field_obj)

            catalog.append({
                "id": f["id"],
                "key": f["key"],
                "name": f["name"],
                "fields": fields,
            })
        return catalog
    finally:
        conn.close()


def get_form_by_key(form_key: str, catalog=None):
    if catalog is None:
        catalog = get_live_catalog()
    for f in catalog:
        if f["key"] == form_key:
            return f
    return None


PLANNER_SYSTEM_PROMPT = """You are an intelligent, schema-driven multi-form planner and data extractor.
You analyze the user's message against the dynamically provided forms catalog and output a clean JSON action plan.

RULES & CORE PRINCIPLES:

1. SEMANTIC INTENT REASONING (DO NOT BLINDLY MATCH SURFACE KEYWORDS):
   - Analyze the PRIMARY EVENT or TRANSACTION being described for each entity, rather than matching isolated words.
   - For every distinct person, request, or issue, ask: What is the fundamental transaction taking place?
     * VISITOR CHECK-IN / GATE ARRIVAL ("visitor_entry"):
       Whenever any outside person (guest, relative, courier, delivery agent, repair technician, electrician, plumber, cleaner, or contractor) arrives, checks in at the gate, or enters the premises to visit a flat, the primary event is AN ARRIVAL PASS ("visitor_entry").
       Their reason for arriving (e.g., servicing an AC, repairing plumbing, delivering documents or food, visiting family) is strictly captured in the "purpose" field ("Service", "Delivery", "Guest", or "Other"). It is NOT a complaint ticket and NOT a facility booking.
       If an executive or courier is described by company/role without a personal name (e.g. "BlueDart courier executive", "Amazon delivery agent"), use that role/title as their visitor_name.
     * REPORTING AN ISSUE / BREAKDOWN ("raise_complaint"):
       Select ONLY when a resident or user is actively reporting a broken fixture, damage, leak, power failure, or maintenance problem that needs to be logged as a complaint ticket.
       (Crucial distinction: If a technician is already at the gate arriving to fix something, that is a "visitor_entry" pass with purpose "Service", NOT a new complaint ticket).
     * AMENITY RESERVATION ("facility_booking"):
       Select ONLY when someone is explicitly requesting to reserve or book a shared community amenity (Clubhouse, Party Hall, Swimming Pool, Gym, Sports Court).
       Never select facility_booking for visitors, couriers, or deliveries walking in.
     * OFFICIAL CIRCULAR / ANNOUNCEMENT ("add_notice"):
       Select ONLY when an official announcement, notice, or circular is being broadcast to residents.

2. ATOMIC ACTIONS PER UNIT / PERSON (NO MERGING INTO COMMA-SEPARATED LISTS):
   - Every distinct unit, flat, person, or issue MUST have its OWN separate action.
   - A field like 'flat_number' or 'visitor_name' represents an INDIVIDUAL entity identifier.
   - NEVER merge, concatenate, or comma-separate multiple flats into one field (e.g. NEVER output 'B-301, B-302, B-305' or 'Flat 101 and 102').
   - If a message mentions 3 different flats with problems (e.g. B-301, B-302, B-305), you MUST output 3 separate 'raise_complaint' actions, one for each flat number, with that flat's specific details.
   - If multiple visitors arrive, you MUST output a separate 'visitor_entry' action for each visitor.
   - Multiple actions can and should share the SAME form_key. Do NOT group them into one just because they share the same topic.

3. ACCURATE URGENCY & OPTION EXTRACTION:
   - For forms with an 'urgency' field (e.g. 'raise_complaint'):
     * Words like 'immediate', 'urgent', 'emergency', 'severe failure', 'critical', 'danger', 'hazard' MUST set urgency to 'High'.
     * Words like 'moderate', 'medium', 'regular', 'soon' set urgency to 'Medium'.
     * Words like 'minor', 'low', 'whenever possible', 'cosmetic' set urgency to 'Low'.
     * Do not leave urgency null if severity/urgency words appear in the user message.

4. CHAIN-OF-THOUGHT INTENT REASONING:
   - For every action, write a "reasoning" string FIRST before specifying "form_key".
   - In "reasoning", state:
     1) Who or what is the subject/unit?
     2) What is the primary event or transaction?
     3) Why this specific form_key is the correct semantic match.

5. ACCURATE FIELD EXTRACTION (REQUIRED & OPTIONAL):
   - Extract both required and optional fields directly from the text whenever mentioned.
   - For fields with predefined options (select fields), choose the best matching exact option from the schema.
   - For "add_notice", if no specific wing is mentioned, set wing to "All".
   - If a field is not mentioned or unknown, set its value to null. Never invent values.

6. SHARED CONTEXT:
   - Put a field in shared_context ONLY if it strictly applies globally to ALL extracted actions. If different items mention different values (e.g., different flats), keep each value inside its own action's fields and leave shared_context empty.

Output Schema:
{
  "is_multi_action": true,
  "shared_context": {},
  "actions": [
    {
      "reasoning": "Explain the entity's primary transaction and why this form_key applies",
      "form_key": "matching_key_from_catalog",
      "title": "Descriptive title for this action",
      "fields": {
        "field_name": "extracted_value"
      }
    }
  ]
}"""


CONVERSATIONAL_SYSTEM_PROMPT = """You are FieldFlow AI, a courteous and helpful form assistant.
The user sent a greeting, pleasantry, or conversational question (such as 'hy', 'helo', 'hiii', 'how are you', 'can you help me', 'good morning', 'thanks', etc.).

Instructions:
1. Reply warmly and naturally in ONE or TWO short, friendly sentences.
2. Mention the exact available forms provided below and ask which form they need to fill.
3. Plain text only. Never write bulleted lists, numbered menus, or multiple steps."""


def generate_conversational_reply(user_text: str) -> str:
    """Uses the model to generate a warm, natural reply listing the live available forms."""
    try:
        catalog = get_live_catalog()
        form_names = [f["name"] for f in catalog if f.get("name")]
    except Exception:
        form_names = []

    if form_names:
        if len(form_names) == 1:
            forms_str = form_names[0]
        elif len(form_names) == 2:
            forms_str = f"{form_names[0]} and {form_names[1]}"
        else:
            forms_str = ", ".join(form_names[:-1]) + f", and {form_names[-1]}"
        form_word = "form" if len(form_names) == 1 else "forms"
        fallback = f"Hello! For now we provide {len(form_names)} {form_word}: {forms_str}. Which form do you need to fill?"
        system_prompt = f"{CONVERSATIONAL_SYSTEM_PROMPT}\n\nAvailable forms ({len(form_names)}): {forms_str}"
    else:
        fallback = "Hello! I am here to help you fill out forms. Which form do you need to fill?"
        system_prompt = CONVERSATIONAL_SYSTEM_PROMPT

    try:
        response = ollama_client.chat(
            model=MODEL,
            options={"temperature": 0.3, "num_predict": 100},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
        )
        reply = (response.get("message", {}).get("content") or "").strip()
        if reply:
            return reply
    except Exception as exc:
        log.warning("Conversational reply failed: %s", exc)

    return fallback


def _normalize_select_value(raw_val, options):
    if not raw_val or not options:
        return raw_val
    clean = str(raw_val).strip().lower()
    for opt in options:
        if clean == opt.lower():
            return opt
    for opt in options:
        if clean in opt.lower() or opt.lower() in clean:
            return opt
    return raw_val


def _clean_val(val):
    if val is None:
        return None
    s = str(val).strip()
    if ("<" in s and ">" in s) or s.lower() in ("null", "none", "nil", "n/a", "undefined", "<null>"):
        return None
    return s


def is_grounded_in_user_text(act, user_text: str, catalog_map=None) -> bool:
    """
    Schema-driven anti-hallucination guardrail:
    Checks if the model-extracted action corresponds to something in the user text.
    Uses purely dynamic SQLite form metadata (form name, fields, and options).
    """
    if not user_text or not user_text.strip():
        return True

    text_lower = user_text.lower()
    form_key = act.get("form_key")
    raw_fields = act.get("fields")
    if not isinstance(raw_fields, dict):
        raw_fields = {}

    # 1. If the action extracted any concrete field value that appears in user_text, it is grounded
    user_words = set(re.findall(r'\b[a-zA-Z0-9]{2,}\b', text_lower))
    for fv in raw_fields.values():
        cleaned = _clean_val(fv)
        if not cleaned:
            continue
        val_words = [w.lower() for w in re.findall(r'\b[a-zA-Z0-9]{2,}\b', cleaned)]
        if val_words and any(w in user_words for w in val_words):
            return True

    # 2. Otherwise check against form metadata from SQLite dynamically
    if catalog_map and form_key in catalog_map:
        form_meta = catalog_map[form_key]
        domain_tokens = set(re.findall(r'\b[a-zA-Z]{3,}\b', form_meta["name"].lower()))
        for fld in form_meta["fields"]:
            domain_tokens.update(re.findall(r'\b[a-zA-Z]{3,}\b', fld["name"].lower()))
            domain_tokens.update(re.findall(r'\b[a-zA-Z]{3,}\b', fld["label"].lower()))
            if fld.get("options"):
                for opt in fld["options"]:
                    domain_tokens.update(re.findall(r'\b[a-zA-Z]{3,}\b', str(opt).lower()))

        if not (domain_tokens & user_words):
            return False

    return True


def apply_shared_context_and_validate(plan_result, catalog, user_text=""):
    shared_raw = plan_result.get("shared_context") or {}
    shared = {k: _clean_val(v) for k, v in shared_raw.items() if _clean_val(v) is not None}
    raw_actions = plan_result.get("actions") or []
    catalog_map = {f["key"]: f for f in catalog}

    # Clean any bracketed placeholders and normalize raw action fields
    for a in raw_actions:
        flds = a.get("fields")
        if not isinstance(flds, dict):
            a["fields"] = {}
        for fk, fv in list(a["fields"].items()):
            a["fields"][fk] = _clean_val(fv)

    # Filter out dummy or ungrounded actions that were not in user message
    actions = [a for a in raw_actions if is_grounded_in_user_text(a, user_text, catalog_map=catalog_map)]
    if not actions and raw_actions:
        actions = raw_actions

    # Deduplicate ONLY true duplicates: same form_key, zero conflicting non-empty fields,
    # and either identical fields or matching non-select (identifying text) fields.
    deduped = []
    for act in actions:
        fkey = act.get("form_key")
        fields = act.get("fields") or {}
        form_meta = catalog_map.get(fkey)
        select_field_names = {
            fld["name"] for fld in (form_meta["fields"] if form_meta else [])
            if fld.get("type") == "select"
        }

        is_dup = False
        for ex in deduped:
            if ex.get("form_key") != fkey:
                continue
            ex_fields = ex.get("fields") or {}

            has_conflict = False
            has_non_select_match = False
            all_keys = set(fields.keys()) | set(ex_fields.keys())
            for k in all_keys:
                v1 = _clean_val(fields.get(k))
                v2 = _clean_val(ex_fields.get(k))
                if v1 and v2:
                    if v1.lower() != v2.lower():
                        has_conflict = True
                        break
                    elif k not in select_field_names:
                        has_non_select_match = True

            if not has_conflict and (has_non_select_match or fields == ex_fields):
                is_dup = True
                for k, v in fields.items():
                    if v and not ex_fields.get(k):
                        ex_fields[k] = v
                break
        if not is_dup:
            deduped.append(act)
    actions = deduped

    processed_actions = []
    all_complete = True

    for idx, act in enumerate(actions, 1):
        form_key = act.get("form_key")
        raw_fields = act.get("fields") or {}
        form_meta = catalog_map.get(form_key)
        if not form_meta:
            continue

        cleaned_fields = {}
        missing_fields = []

        for fld in form_meta["fields"]:
            fname = fld["name"]
            ftype = fld.get("type", "text")
            options = fld.get("options")
            is_req = fld.get("required", True)

            val = _clean_val(raw_fields.get(fname))

            # 1. Shared Context Inheritance
            if val is None:
                if fname in shared and shared[fname]:
                    val = shared[fname]
                else:
                    for sk, sv in shared.items():
                        if sv and (sk.lower() == fname.lower() or sk.lower() in fname.lower() or fname.lower() in sk.lower()):
                            val = sv
                            break

            # 2. Dynamic Schema Options Matching & Normalization
            if ftype == "select" and options:
                if val is not None and str(val).strip() and str(val).lower() not in ("null", "none"):
                    norm_val = _normalize_select_value(str(val).strip(), options)
                    matched_opt = None
                    for opt in options:
                        if norm_val.lower() == opt.lower():
                            matched_opt = opt
                            break
                    cleaned_fields[fname] = matched_opt
                else:
                    cleaned_fields[fname] = None
            else:
                if val is not None and str(val).strip() and str(val).lower() not in ("null", "none"):
                    clean_str = str(val).strip()
                    if "vehicle" in fname.lower() or "plate" in fname.lower():
                        if INDIAN_PLATE_RE.match(clean_str):
                            cleaned_fields[fname] = re.sub(r"\s+", " ", clean_str.upper())
                        else:
                            cleaned_fields[fname] = None
                    elif ftype == "date" or "date" in fname.lower():
                        import demo_paragraph as dp
                        cleaned_fields[fname] = dp.normalize_date(clean_str)
                    else:
                        cleaned_fields[fname] = clean_str
                else:
                    cleaned_fields[fname] = None

            # 3. Dynamic Required Fields Check
            if is_req and (cleaned_fields[fname] is None or cleaned_fields[fname] == ""):
                missing_fields.append(fld["label"])

        # Default general notice to 'All' wings if no specific wing is strictly mentioned
        if form_key == "add_notice" and not cleaned_fields.get("wing"):
            cleaned_fields["wing"] = "All"

        is_action_complete = len(missing_fields) == 0
        if not is_action_complete:
            all_complete = False

        title = act.get("title") or f"{form_meta['name']} #{idx}"

        processed_actions.append({
            "id": f"act_{idx}_{uuid.uuid4().hex[:6]}",
            "form_key": form_key,
            "form_name": form_meta["name"],
            "title": title,
            "reasoning": act.get("reasoning", ""),
            "fields": cleaned_fields,
            "schema": {
                "form_name": form_meta["name"],
                "fields": form_meta["fields"],
            },
            "is_complete": is_action_complete,
            "missing_fields": missing_fields,
        })

    return {
        "is_multi_action": len(processed_actions) > 1,
        "shared_context": shared,
        "actions": processed_actions,
        "all_complete": (len(processed_actions) > 0 and all_complete),
    }


def plan_and_extract(user_text: str):
    """
    Universal planner: takes any user prompt, inspects live forms.db catalog,
    and returns an action plan with 1 or more extracted form instances.
    """
    if not user_text or not user_text.strip():
        return None

    catalog = get_live_catalog()
    if not catalog:
        log.warning("No forms found in forms.db catalog.")
        return None

    catalog_summary = []
    for f in catalog:
        catalog_summary.append({
            "key": f["key"],
            "name": f["name"],
            "fields": [
                {
                    "name": fld["name"],
                    "label": fld["label"],
                    "type": fld["type"],
                    "required": fld["required"],
                    "options": fld.get("options"),
                }
                for fld in f["fields"]
            ],
        })

    prompt = f"""Available forms catalog:
{json.dumps(catalog_summary, indent=2)}

User Message:
"{user_text}"

Extract the action plan according to instructions as JSON:"""

    try:
        response = ollama_client.chat(
            model=MODEL,
            format="json",
            options={"temperature": 0.0, "num_ctx": 8192, "num_predict": 4096},
            messages=[
                {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        content = (response.get("message", {}).get("content") or "").strip()
        parsed = json.loads(content)
    except Exception as exc:
        log.error("Ollama plan_and_extract failed: %s", exc)
        return None

    if not isinstance(parsed, dict) or not parsed.get("actions"):
        log.info("Planner returned no actions for text: %s", user_text[:80])
        return None

    result = apply_shared_context_and_validate(parsed, catalog, user_text=user_text)
    log.info(
        "Planner success | is_multi=%s actions_count=%s all_complete=%s",
        result["is_multi_action"],
        len(result["actions"]),
        result["all_complete"],
    )
    return result


def execute_batch_submission(items, chat_id=None):
    """
    Saves multiple form actions atomically into form_submissions.
    items: list of dicts with {"form_key": str, "answers": dict}
    """
    if not items:
        return {"success": False, "error": "No items to submit"}

    conn = get_connection()
    try:
        submission_ids = []
        with conn:
            for item in items:
                fkey = item.get("form_key")
                answers = dict(item.get("answers") or {})

                for k, v in list(answers.items()):
                    if v is not None and str(v).strip() and ("vehicle" in k.lower() or "plate" in k.lower()):
                        clean_plate = str(v).strip()
                        if not INDIAN_PLATE_RE.match(clean_plate):
                            return {
                                "success": False,
                                "error": f"Invalid Indian vehicle number plate: '{clean_plate}' (e.g. MH 12 AB 1234).",
                            }
                        answers[k] = re.sub(r"\s+", " ", clean_plate.upper())

                row = conn.execute("SELECT id, name FROM forms WHERE key = ?", (fkey,)).fetchone()
                if not row:
                    continue
                form_id = row["id"]

                cur = conn.execute(
                    "INSERT INTO form_submissions (form_id, answers_json) VALUES (?, ?)",
                    (form_id, json.dumps(answers)),
                )
                submission_ids.append({
                    "submission_id": cur.lastrowid,
                    "form_key": fkey,
                    "form_name": row["name"],
                    "answers": answers,
                })

            if chat_id and submission_ids:
                chat_row = conn.execute(
                    "SELECT messages_json FROM chats WHERE id = ?",
                    (chat_id,),
                ).fetchone()
                if chat_row:
                    try:
                        messages = json.loads(chat_row["messages_json"] or "[]")
                    except Exception:
                        messages = []
                    messages.append({
                        "role": "assistant",
                        "content": f"All set! **{len(submission_ids)} submissions** recorded successfully:",
                        "receipt_data": {
                            "is_multi": True,
                            "records": submission_ids,
                        },
                    })
                    conn.execute(
                        """
                        UPDATE chats
                        SET messages_json = ?,
                            answers_json = '{}',
                            form_key = NULL,
                            current_field_name = NULL,
                            completed = 1,
                            submission_id = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            json.dumps(messages),
                            submission_ids[-1]["submission_id"],
                            datetime.now(timezone.utc).isoformat(),
                            chat_id,
                        ),
                    )

        return {
            "success": True,
            "submission_ids": [s["submission_id"] for s in submission_ids],
            "records": submission_ids,
            "count": len(submission_ids),
        }
    except Exception as exc:
        log.error("Batch submission failed: %s", exc)
        return {"success": False, "error": str(exc)}
    finally:
        conn.close()

