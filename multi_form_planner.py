
"""
Multi-Form Planner & Batch Extraction Engine.

Handles generic, schema-driven multi-record and mixed-form extraction.
Works for ANY form defined in forms.db with zero hardcoded form keys or aliases.
Uses Ollama (MODEL from .env, default llama3.2:1b).
"""

import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import uuid

import ollama
from config import MODEL, OLLAMA_BASE_URL, DB_PATH, ollama_client

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
You analyze the user's message against the available forms catalog and output a clean JSON action plan.

CRITICAL RULES:
1. ONLY EXTRACT WHAT THE USER MENTIONED:
   - Extract ONLY the specific items described in the User Message.
   - NEVER create actions for forms that the user did not ask for.
   - NEVER invent, hallucinate, or output dummy records, fake names, or fake flat numbers.
   - If the user reports 2 maintenance issues, output EXACTLY 2 actions with form_key: "raise_complaint". Do NOT create visitor entries or notices.

2. ONE ACTION PER DISTINCT ISSUE:
   - Separate issues or units into distinct actions (e.g. Flat 101 sink leak, Flat 102 elevator buttons).

3. SCHEMA-GROUNDED OPTIONS:
   - For fields with "options" (e.g. category, purpose): select the best matching option from the schema's options list.

Output Schema:
{
  "is_multi_action": true/false,
  "shared_context": {},
  "actions": [
    {
      "form_key": "<exact key from catalog>",
      "title": "<short descriptive title>",
      "fields": {
        "<field_name>": "<value_or_null>"
      }
    }
  ]
}"""


CONVERSATIONAL_SYSTEM_PROMPT = """You are FieldFlow AI, a courteous and helpful form assistant.
The user sent a greeting, pleasantry, or conversational question (such as 'hy', 'helo', 'hiii', 'how are you', 'can you help me', 'good morning', 'thanks', etc.).

Instructions:
1. Reply warmly and naturally in ONE short, friendly sentence.
2. Let them know you can help them fill out forms, register visitors, or raise maintenance complaints.
3. Plain text only. Never write bulleted lists, numbered menus, or multiple steps."""


def generate_conversational_reply(user_text: str) -> str:
    """Uses the model to generate a warm, natural 1-sentence reply for greetings and pleasantries."""
    try:
        response = ollama_client.chat(
            model=MODEL,
            options={"temperature": 0.3, "num_predict": 100},
            messages=[
                {"role": "system", "content": CONVERSATIONAL_SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
        )
        reply = (response.get("message", {}).get("content") or "").strip()
        if reply:
            return reply
    except Exception as exc:
        log.warning("Conversational reply failed: %s", exc)

    return "Hello! I am here to help you fill out forms, register visitors, or report maintenance requests. How can I assist you today?"


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


def is_grounded_in_user_text(act, user_text: str) -> bool:
    """
    Strict anti-hallucination guardrail:
    Filters out any dummy/example actions hallucinated by small models
    (such as fake visitors, dummy notices, or unmentioned flats/issues).
    """
    if not user_text or not user_text.strip():
        return True

    text_lower = user_text.lower()
    form_key = act.get("form_key")
    raw_fields = act.get("fields") or {}

    # 1. Intent check for specific form domains
    if form_key == "visitor_entry":
        if not any(w in text_lower for w in ("visitor", "guest", "delivery", "vehicle", "car", "bike", "cab", "courier", "swiggy", "zomato", "uber", "visit", "arriving", "arrive")):
            return False

    if form_key == "add_notice":
        if not any(w in text_lower for w in ("notice", "announcement", "circular", "broadcast", "post a notice")):
            return False

    user_words = set(re.findall(r'\b[a-zA-Z0-9]{2,}\b', text_lower))

    # 2. Check identifying entity details (flat number, visitor name, vehicle number)
    specific_identifiers = [
        str(raw_fields.get("visitor_name") or ""),
        str(raw_fields.get("flat_number") or ""),
        str(raw_fields.get("vehicle_number") or ""),
    ]
    specific_str = " ".join(specific_identifiers).strip()
    if specific_str:
        specific_words = [
            w.lower() for w in re.findall(r'\b[a-zA-Z0-9]{2,}\b', specific_str)
            if w.lower() not in ("flat", "wing", "room", "number", "all", "none", "null")
        ]
        if specific_words and not any(w in user_words for w in specific_words):
            return False

    # 3. Check details/description text overlap
    details_str = str(raw_fields.get("details") or raw_fields.get("description") or act.get("title") or "")
    details_words = [
        w.lower() for w in re.findall(r'\b[a-zA-Z0-9]{3,}\b', details_str)
        if w.lower() not in ("flat", "wing", "room", "number", "issue", "issues", "maintenance", "reported", "report", "complaint", "other")
    ]
    if details_words:
        overlap = sum(1 for w in details_words if w in user_words)
        if overlap == 0:
            return False

    return True


def apply_shared_context_and_validate(plan_result, catalog, user_text=""):
    shared = plan_result.get("shared_context") or {}
    raw_actions = plan_result.get("actions") or []
    # Filter out dummy or ungrounded actions that were not in user message
    actions = [a for a in raw_actions if is_grounded_in_user_text(a, user_text)]
    if not actions and raw_actions:
        actions = raw_actions  # Fallback if filter was overly aggressive

    catalog_map = {f["key"]: f for f in catalog}

    processed_actions = []
    all_complete = True

    for idx, act in enumerate(actions, 1):
        form_key = act.get("form_key")
        raw_fields = act.get("fields") or {}

        # Guardrail against 1B LLM form confusion (e.g. labeling a complaint as visitor_entry)
        text_content = f"{act.get('title', '')} {raw_fields.get('details', '')} {raw_fields.get('description', '')} {raw_fields.get('subject', '')}".lower()
        has_visitor_intent = any(w in text_content for w in ("visitor", "guest", "delivery", "vehicle", "car", "bike", "visit", "arriving", "arrive"))
        has_complaint_intent = any(w in text_content or w in user_text.lower() for w in ("leak", "pipe", "water", "spark", "electric", "light", "lift", "elevator", "complaint", "broken", "issue", "repair", "maintenance"))

        if form_key == "visitor_entry" and not has_visitor_intent and has_complaint_intent:
            form_key = "raise_complaint"
            if not raw_fields.get("details"):
                for candidate in ("description", "subject", "issue", "problem"):
                    if raw_fields.get(candidate):
                        raw_fields["details"] = raw_fields[candidate]
                        break
            if not raw_fields.get("details") and act.get("title"):
                raw_fields["details"] = act.get("title")

        form_meta = catalog_map.get(form_key)
        if not form_meta:
            continue

        cleaned_fields = {}
        missing_fields = []

        # Context inheritance & normalization
        for fld in form_meta["fields"]:
            fname = fld["name"]
            ftype = fld.get("type", "text")
            options = fld.get("options")
            is_req = fld.get("required", True)

            val = raw_fields.get(fname)

            # 1. Action-specific fallback from item title or details (e.g. flat number)
            if (val is None or val == "" or str(val).lower() in ("null", "none")):
                if "flat" in fname.lower():
                    item_text = f"{act.get('title', '')} {raw_fields.get('details', '')} {raw_fields.get('description', '')}"
                    m = re.search(r'\b([A-Za-z]?\s*[-]?\s*\d{2,4})\b', item_text, re.IGNORECASE)
                    if m:
                        val = m.group(1).replace(" ", "").upper()

            # 2. Dynamic Shared Context Inheritance
            if val is None or val == "" or str(val).lower() in ("null", "none"):
                if fname in shared and shared[fname]:
                    val = shared[fname]
                else:
                    # Dynamic fuzzy key matching across shared_context keys
                    fname_lower = fname.lower()
                    for sk, sv in shared.items():
                        if not sv:
                            continue
                        sk_lower = sk.lower()
                        if ("visitor" in fname_lower and "resident" in sk_lower) or ("resident" in fname_lower and "visitor" in sk_lower):
                            continue
                        if sk_lower == fname_lower or sk_lower in fname_lower or fname_lower in sk_lower:
                            val = sv
                            break

            # 3. Dynamic Schema Options Matching & Normalization
            if ftype == "select" and options:
                # If model left option null, check if any allowed option is mentioned in the item's details/title
                if val is None or val == "" or str(val).lower() in ("null", "none"):
                    item_text = f"{act.get('title', '')} {raw_fields.get('details', '')} {raw_fields.get('subject', '')} {raw_fields.get('description', '')}".lower()
                    if not item_text.strip():
                        item_text = user_text.lower()
                    for opt in options:
                        if re.search(r'\b' + re.escape(opt.lower()) + r'\b', item_text):
                            val = opt
                            break

                if val is not None and str(val).strip() and str(val).lower() not in ("null", "none"):
                    norm_val = _normalize_select_value(str(val).strip(), options)
                    matched_opt = None
                    for opt in options:
                        if norm_val.lower() == opt.lower():
                            matched_opt = opt
                            break
                    if matched_opt:
                        cleaned_fields[fname] = matched_opt
                    else:
                        cleaned_fields[fname] = None
                else:
                    cleaned_fields[fname] = None
            else:
                if val is not None and str(val).strip() and str(val).lower() not in ("null", "none"):
                    cleaned_fields[fname] = str(val).strip()
                elif fname in ("details", "description") and act.get("title"):
                    cleaned_fields[fname] = str(act.get("title")).strip()
                else:
                    cleaned_fields[fname] = None

            # 4. Dynamic Required Fields Check
            if is_req and (cleaned_fields[fname] is None or cleaned_fields[fname] == ""):
                missing_fields.append(fld["label"])

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
            options={"temperature": 0.0},
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
                answers = item.get("answers") or {}

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
                conn.execute(
                    """
                    UPDATE chats 
                    SET completed = 1, submission_id = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (submission_ids[-1]["submission_id"], chat_id),
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

