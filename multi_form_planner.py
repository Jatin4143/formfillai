
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


PLANNER_SYSTEM_PROMPT = """You are an intelligent form planner and multi-action data extractor.
You are given a catalog of available forms (each with a key, form name, and list of fields) and a user's message.

Your instructions:
1. Analyze the user's message. Determine whether it describes:
   - A single form request (1 action)
   - Multiple records of the SAME form (batch, e.g. separate complaints for different flats)
   - Multiple records of DIFFERENT forms (compound, e.g. visitor entries + complaint)
2. If different flats or units are mentioned for different problems, assign the respective flat number to that specific action.
3. If an ambient resident name or flat applies to ALL actions, place it in "shared_context".
4. For each distinct action or record:
   - Identify the matching "form_key" from the catalog.
   - Give it a brief "title" (e.g. "Complaint Flat B-204", "Visitor Entry - Car").
   - Extract the specific field values for that action.
   - For category/select fields: pick the best matching option from the options list.
   - For textarea/details fields: put the specific issue/description for that item (not the whole prompt).
   - If a field is not stated or unknown, set its value to null. Never invent details.
5. Return ONLY a valid JSON object.

Examples:

User: "There are two complaints: Flat 101 has tap leakage, and flat 102 has elevator light broken"
{
  "is_multi_action": true,
  "shared_context": {},
  "actions": [
    {
      "form_key": "raise_complaint",
      "title": "Tap Leakage (Flat 101)",
      "fields": {
        "category": "Water",
        "flat_number": "101",
        "details": "Tap leakage in flat 101",
        "urgency": "Medium"
      }
    },
    {
      "form_key": "raise_complaint",
      "title": "Elevator Light Broken (Flat 102)",
      "fields": {
        "category": "Electricity",
        "flat_number": "102",
        "details": "Elevator light broken for flat 102",
        "urgency": "Medium"
      }
    }
  ]
}

User: "My name is Priya from flat A-301. Guest Amit coming by car MH12AB1234, and also complaint about low water pressure"
{
  "is_multi_action": true,
  "shared_context": {
    "flat_number": "A-301",
    "resident_name": "Priya"
  },
  "actions": [
    {
      "form_key": "visitor_entry",
      "title": "Guest Amit (Car MH12AB1234)",
      "fields": {
        "visitor_name": "Amit",
        "flat_number": "A-301",
        "purpose": "Guest",
        "vehicle_number": "MH12AB1234"
      }
    },
    {
      "form_key": "raise_complaint",
      "title": "Low Water Pressure",
      "fields": {
        "category": "Water",
        "flat_number": "A-301",
        "details": "Low water pressure in flat A-301",
        "urgency": "Medium"
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


def apply_shared_context_and_validate(plan_result, catalog, user_text=""):
    shared = plan_result.get("shared_context") or {}
    actions = plan_result.get("actions") or []
    catalog_map = {f["key"]: f for f in catalog}

    processed_actions = []
    all_complete = True

    for idx, act in enumerate(actions, 1):
        form_key = act.get("form_key")
        form_meta = catalog_map.get(form_key)
        if not form_meta:
            continue

        raw_fields = act.get("fields") or {}
        cleaned_fields = {}
        missing_fields = []

        # Context inheritance & normalization
        for fld in form_meta["fields"]:
            fname = fld["name"]
            ftype = fld.get("type", "text")
            options = fld.get("options")
            is_req = fld.get("required", True)

            val = raw_fields.get(fname)

            # 1. Action-specific fallback from its own title or details
            if (val is None or val == "" or str(val).lower() in ("null", "none")):
                act_text = f"{act.get('title', '')} {raw_fields.get('details', '')}"
                if "flat" in fname.lower() and act_text:
                    m = re.search(r'\b([A-Za-z][-]?\d{2,4})\b', act_text, re.IGNORECASE)
                    if m:
                        val = m.group(1).upper()
                    else:
                        m_wing = re.search(r'\b([A-Za-z]\s*wing|wing\s*[A-Za-z])\b', act_text, re.IGNORECASE)
                        if m_wing:
                            val = m_wing.group(1).title()

            # 2. Inherit from shared_context if still empty
            if val is None or val == "" or str(val).lower() in ("null", "none"):
                if fname in shared and shared[fname]:
                    val = shared[fname]
                elif "flat" in fname.lower() and "flat_number" in shared:
                    val = shared["flat_number"]
                elif "name" in fname.lower() and "resident_name" in shared and "visitor" not in fname.lower():
                    val = shared["resident_name"]

            # 3. Urgency fallback if mentioned in details or title
            if fname == "urgency" and (val is None or str(val).lower() in ("null", "none")):
                det_text = f"{raw_fields.get('details', '')} {act.get('title', '')}".lower()
                if any(w in det_text for w in ("urgent", "overflow", "emergency", "immediate", "critical")):
                    val = "High"

            # 4. Dynamic schema options matching (zero hardcoded domain keywords)
            if (val is None or val == "" or str(val).lower() in ("null", "none")) and ftype == "select" and options:
                # For single-action, whole user_text is safe; for multi-action, isolate to item's own context to avoid cross-contamination
                if not plan_result.get("is_multi_action") and len(actions) == 1:
                    item_text = f"{user_text} {act.get('title', '')} {raw_fields.get('details', '')}".lower()
                else:
                    item_text = f"{act.get('title', '')} {raw_fields.get('details', '')}".lower()

                for opt in options:
                    if re.search(r'\b' + re.escape(opt.lower()) + r'\b', item_text):
                        val = opt
                        break
                if val is None:
                    for opt in options:
                        opt_words = [w.lower() for w in re.findall(r'\b[a-zA-Z0-9]+\b', opt) if len(w) >= 3]
                        for w in opt_words:
                            if re.search(r'\b' + re.escape(w) + r'\b', item_text):
                                val = opt
                                break
                        if val is not None:
                            break

            # Format normalization
            if val is not None and str(val).strip():
                clean_str = str(val).strip()
                if ftype == "select" and options:
                    clean_str = _normalize_select_value(clean_str, options)
                cleaned_fields[fname] = clean_str
            else:
                cleaned_fields[fname] = None
                if is_req:
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

