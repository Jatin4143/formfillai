
"""
Server bridge for FieldFlow AI Form Assistant.
Exposes REST API endpoints and serves the static frontend from the /frontend directory.
Uses modular business logic from the core/ package.
"""

import json
import logging
import os
from pathlib import Path
import re
import uuid
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
import uvicorn

from config import BASE_DIR, FRONTEND_DIR, PORT
from core import db, validation, intent, extractor, questions, chat_session
from core.multi_form_planner import (
    plan_and_extract,
    execute_batch_submission,
    generate_conversational_reply,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("server")


class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


def save_chat_interaction(chat_id, user_text, reply, form_key, answers, current_field, completed=False, submission_id=None):
    """Persist conversation step to SQLite DB (chats table) and return (chat_id, title)."""
    if not chat_id:
        chat_id = str(uuid.uuid4())

    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT id, title, title_locked, messages_json, answers_json FROM chats WHERE id = ?",
            (chat_id,)
        ).fetchone()

        if row:
            title = row["title"]
            title_locked = bool(row["title_locked"])
            messages = json.loads(row["messages_json"] or "[]")
        else:
            title = "New Conversation"
            title_locked = False
            messages = []

        if user_text:
            messages.append({"role": "user", "content": user_text})
        if reply:
            messages.append({"role": "assistant", "content": reply})

        if not title_locked:
            if form_key:
                meta = db.form_meta_by_key(form_key)
                if meta:
                    title = meta["form_name"]
            elif user_text and title in ("New Conversation", "Untitled", ""):
                clean_t = user_text.strip().replace("\n", " ")
                title = clean_t[:35] + ("..." if len(clean_t) > 35 else "")

        field_name = current_field["name"] if (isinstance(current_field, dict) and "name" in current_field) else None

        with conn:
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
                    chat_id,
                    title,
                    1 if title_locked else 0,
                    json.dumps(messages),
                    json.dumps(answers or {}),
                    form_key,
                    field_name,
                    1 if completed else 0,
                    submission_id,
                    chat_session.utc_now(),
                )
            )
        return chat_id, title
    except Exception as exc:
        log.error("Failed to save chat interaction: %s", exc)
        return chat_id, "New Conversation"
    finally:
        conn.close()


async def list_chats_endpoint(request):
    """GET /api/chats - returns all chat sessions sorted by recency."""
    conn = db.get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, title, title_locked, form_key, completed, submission_id, updated_at
            FROM chats
            WHERE messages_json != '[]' AND messages_json IS NOT NULL
            ORDER BY updated_at DESC
            """
        ).fetchall()
        chats = [
            {
                "id": r["id"],
                "title": r["title"] or "New Conversation",
                "title_locked": bool(r["title_locked"]),
                "form_key": r["form_key"],
                "completed": bool(r["completed"]),
                "submission_id": r["submission_id"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
        return JSONResponse(chats)
    except Exception as exc:
        log.error("Failed to list chats: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)
    finally:
        conn.close()


async def create_chat_endpoint(request):
    """POST /api/chats - creates a new empty chat session."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    title = (body.get("title") or "New Conversation").strip()
    new_id = str(uuid.uuid4())
    conn = db.get_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO chats (
                    id, title, title_locked, messages_json, answers_json,
                    form_key, current_field_name, completed, submission_id, updated_at
                ) VALUES (?, ?, 0, '[]', '{}', NULL, NULL, 0, NULL, ?)
                """,
                (new_id, title, chat_session.utc_now())
            )
        return JSONResponse({
            "id": new_id,
            "title": title,
            "messages": [],
            "answers": {},
            "form_key": None,
            "form": None,
            "current_field": None,
            "completed": False
        })
    except Exception as exc:
        log.error("Failed to create chat: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)
    finally:
        conn.close()


async def get_chat_endpoint(request):
    """GET /api/chats/{id} - returns a chat session's full messages and state."""
    chat_id = request.path_params.get("id")
    conn = db.get_connection()
    try:
        row = conn.execute(
            """
            SELECT id, title, title_locked, messages_json, answers_json,
                   form_key, current_field_name, completed, submission_id, updated_at
            FROM chats
            WHERE id = ?
            """,
            (chat_id,)
        ).fetchone()
        if not row:
            return JSONResponse({"error": "Chat not found"}, status_code=404)

        form_key = row["form_key"]
        form_data = db.fetch_form_schema(form_key) if form_key else None
        field_name = row["current_field_name"]
        current_field = None
        if form_data and field_name:
            for f in form_data["schema"]["fields"]:
                if f["name"] == field_name:
                    current_field = f
                    break

        chat = {
            "id": row["id"],
            "title": row["title"] or "New Conversation",
            "title_locked": bool(row["title_locked"]),
            "messages": json.loads(row["messages_json"] or "[]"),
            "answers": json.loads(row["answers_json"] or "{}"),
            "form_key": form_key,
            "form": form_data,
            "current_field": current_field,
            "completed": bool(row["completed"]),
            "submission_id": row["submission_id"],
            "updated_at": row["updated_at"],
        }
        return JSONResponse(chat)
    except Exception as exc:
        log.error("Failed to get chat %s: %s", chat_id, exc)
        return JSONResponse({"error": str(exc)}, status_code=500)
    finally:
        conn.close()


async def update_chat_endpoint(request):
    """PUT /api/chats/{id} - rename chat title."""
    chat_id = request.path_params.get("id")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    new_title = (body.get("title") or "").strip()
    if not new_title:
        return JSONResponse({"error": "Title cannot be empty"}, status_code=400)

    conn = db.get_connection()
    try:
        with conn:
            cur = conn.execute(
                "UPDATE chats SET title = ?, title_locked = 1, updated_at = ? WHERE id = ?",
                (new_title[:60], chat_session.utc_now(), chat_id)
            )
            if cur.rowcount == 0:
                return JSONResponse({"error": "Chat not found"}, status_code=404)
        return JSONResponse({"id": chat_id, "title": new_title[:60]})
    except Exception as exc:
        log.error("Failed to update chat %s: %s", chat_id, exc)
        return JSONResponse({"error": str(exc)}, status_code=500)
    finally:
        conn.close()


async def delete_chat_endpoint(request):
    """DELETE /api/chats/{id} - deletes a chat session."""
    chat_id = request.path_params.get("id")
    try:
        chat_session.delete_chat_from_db(chat_id)
        return JSONResponse({"success": True, "id": chat_id})
    except Exception as exc:
        log.error("Failed to delete chat %s: %s", chat_id, exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


async def list_forms_endpoint(request):
    """GET /api/forms - returns all available forms."""
    forms = db.list_forms()
    return JSONResponse(forms)


async def get_form_schema_endpoint(request):
    """GET /api/forms/{key}/schema - returns schema and fields for a form."""
    form_key = request.path_params.get("key")
    schema = db.fetch_form_schema(form_key)
    if not schema:
        return JSONResponse({"error": "Form not found"}, status_code=404)
    return JSONResponse(schema)


def is_probable_multi_action(user_text: str) -> bool:
    """Dynamic heuristic to check if a message likely contains multiple distinct records/forms."""
    text_lower = user_text.lower()
    if any(p in text_lower for p in ("two ", "three ", "four ", "2 ", "3 ", "4 ", "both ", "multiple ", "and also ", "also have ", "as well as ")):
        if any(sep in text_lower for sep in (",", ";", "\n", " and ")):
            return True

    try:
        forms = db.list_forms()
        form_matches = 0
        for f in forms:
            f_name = f.get("name", "").lower()
            f_key = f.get("key", "").lower().replace("_", " ")
            if (f_name and f_name in text_lower) or (f_key and f_key in text_lower):
                form_matches += 1
        if form_matches >= 2:
            return True
    except Exception:
        pass

    units = re.findall(r'\b[a-z]?\s*[-]?\s*\d{3,4}\b', text_lower)
    if len(units) >= 2:
        return True

    return False


def should_switch_to_form(user_text, current_form_key):
    """Check if user wants to switch to a different form without relying on streamlit session_state."""
    detected_key = intent.detect_form_key(user_text)
    if not detected_key or detected_key == current_form_key:
        return None
    meta = db.form_meta_by_key(detected_key)
    if meta and intent.message_is_only_form_name(user_text, meta):
        return detected_key
    return None


async def chat_endpoint(request):
    """
    POST /api/chat
    Processes conversational input, manages form state, field extraction,
    and returns whether all fields are collected to trigger review modal.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    user_text = (body.get("message") or "").strip()
    form_key = body.get("form_key")
    answers = dict(body.get("answers") or {})
    current_field = body.get("current_field")
    current_action_id = body.get("current_action_id") or (current_field.get("action_id") if isinstance(current_field, dict) else None)
    chat_id = body.get("chat_id")
    actions = list(body.get("actions") or [])
    is_batch = bool(body.get("is_batch", False))

    def make_response(res_data):
        nonlocal chat_id
        if user_text:
            saved_chat_id, chat_title = save_chat_interaction(
                chat_id=chat_id,
                user_text=user_text,
                reply=res_data.get("reply", ""),
                form_key=res_data.get("form_key"),
                answers=res_data.get("answers"),
                current_field=res_data.get("current_field"),
                completed=(res_data.get("status") == "ready"),
            )
            res_data["chat_id"] = saved_chat_id
            res_data["chat_title"] = chat_title
        return JSONResponse(res_data)

    if not user_text:
        return JSONResponse({"error": "Empty message"}, status_code=400)

    # 1. Handle cancel / reset command
    if intent.is_cancel_command(user_text):
        reply = "Current form filling was cancelled. Let me know whenever you'd like to fill a form."
        return make_response({
            "status": "cancelled",
            "reply": reply,
            "form": None,
            "form_key": None,
            "answers": {},
            "current_field": None,
            "ready_for_review": False
        })

    # 2. If no form is currently active
    if not form_key:
        # Check if user message is purely greeting, pleasantry, or conversational question
        if intent.is_greeting_or_smalltalk(user_text) or any(w in user_text.lower().split() for w in ("hy", "helo", "hiii", "hey", "hello", "hi")):
            # Check first if they also gave actual form details (e.g. "hi, flat B-204 tap leak")
            direct_key = intent.detect_form_key(user_text)
            if not direct_key and len(user_text.split()) < 12:
                model_reply = generate_conversational_reply(user_text)
                return make_response({
                    "status": "greeting",
                    "reply": model_reply,
                    "form": None,
                    "form_key": None,
                    "answers": {},
                    "current_field": None,
                    "ready_for_review": False
                })

        # Fast path: Check if user message directly selects a form from DB (e.g. "add notice", "visitor entry")
        direct_key = intent.detect_form_key(user_text)
        if direct_key:
            form = db.fetch_form_schema(direct_key)
            if form and intent.message_is_only_form_name(user_text, form):
                answers = {}
                if direct_key == "add_notice":
                    answers["wing"] = "All"
                first_field = extractor.get_next_field(form["schema"], answers)
                question = questions.get_question_for_field(form, first_field) if first_field else ""
                reply = f"Let's fill out **{form['form_name']}**.\n\n{question}"
                return make_response({
                    "status": "in_progress",
                    "reply": reply,
                    "form": form,
                    "form_key": direct_key,
                    "answers": answers,
                    "current_field": first_field,
                    "is_batch": False,
                    "actions": [],
                    "ready_for_review": False
                })

        # Check multi-form planner for multi-action batch or paragraph extraction
        plan = plan_and_extract(user_text)
        if plan and plan.get("actions"):
            if plan["is_multi_action"]:
                # Multi-record / compound plan (e.g. 3 complaints or visitors + complaint)
                if plan["all_complete"]:
                    reply = f"🎉 **Identified and extracted {len(plan['actions'])} items!** Please review and confirm in the pop-up modal."
                    return make_response({
                        "status": "ready",
                        "reply": reply,
                        "form": None,
                        "form_key": None,
                        "answers": {},
                        "current_field": None,
                        "current_action_id": None,
                        "is_batch": True,
                        "actions": plan["actions"],
                        "ready_for_review": True
                    })
                else:
                    incomplete = [a for a in plan["actions"] if not a["is_complete"]]
                    first_inc = incomplete[0]
                    missing_label = first_inc["missing_fields"][0] if first_inc.get("missing_fields") else "required details"
                    next_fld = next((f for f in first_inc["schema"]["fields"] if f["label"] == missing_label), None)
                    if next_fld:
                        next_fld = dict(next_fld)
                        next_fld["action_id"] = first_inc["id"]
                    reply = f"I've drafted {len(plan['actions'])} items, but need **{missing_label}** for {first_inc['title']}."
                    return make_response({
                        "status": "in_progress",
                        "reply": reply,
                        "form": db.fetch_form_schema(first_inc["form_key"]),
                        "form_key": first_inc["form_key"],
                        "answers": first_inc["fields"],
                        "current_field": next_fld,
                        "current_action_id": first_inc["id"],
                        "is_batch": True,
                        "actions": plan["actions"],
                        "ready_for_review": False
                    })
            else:
                # Single action identified by the planner
                act = plan["actions"][0]
                single_form = db.fetch_form_schema(act["form_key"]) or {
                    "key": act["form_key"],
                    "form_name": act["form_name"],
                    "schema": act["schema"],
                }
                single_schema = single_form["schema"]
                # Only keep fields that actually have non-null, non-empty values
                single_answers = {
                    k: v for k, v in (act.get("fields") or {}).items()
                    if v is not None and str(v).strip() != "" and str(v).lower() != "null"
                }
                if act.get("is_complete") and extractor.get_next_field(single_schema, single_answers) is None:
                    reply = f"🎉 **All details have been extracted for {act['form_name']}!** Please review and confirm in the pop-up modal."
                    return make_response({
                        "status": "ready",
                        "reply": reply,
                        "form": single_form,
                        "form_key": act["form_key"],
                        "answers": single_answers,
                        "current_field": None,
                        "is_batch": False,
                        "actions": [act],
                        "ready_for_review": True
                    })
                else:
                    next_fld = extractor.get_next_field(single_schema, single_answers)
                    question = questions.get_question_for_field(single_form, next_fld) if next_fld else ""
                    if single_answers:
                        overview_msg = questions.build_form_overview_message(single_form, accepted=single_answers)
                        reply = f"{overview_msg}\n\n{question}"
                    else:
                        reply = f"Let's fill out **{single_form['form_name']}**.\n\n{question}"
                    return make_response({
                        "status": "in_progress",
                        "reply": reply,
                        "form": single_form,
                        "form_key": act["form_key"],
                        "answers": single_answers,
                        "current_field": next_fld,
                        "is_batch": False,
                        "actions": [act],
                        "ready_for_review": False
                    })

        direct_key = intent.detect_form_key(user_text)
        detected_key = direct_key
        prefilled = {}

        if not detected_key:
            detected_key, prefilled = extractor.classify_and_extract(user_text)

        if not detected_key:
            reply = questions.chatbot_help_reply(user_text, db.list_forms())
            return make_response({
                "status": "help",
                "reply": reply,
                "form": None,
                "form_key": None,
                "answers": {},
                "current_field": None,
                "ready_for_review": False
            })

        # Start selected form
        form = db.fetch_form_schema(detected_key)
        if not form:
            return JSONResponse({"error": "Form not found"}, status_code=404)

        answers = {}
        accepted = {}
        if prefilled:
            accepted = extractor.merge_extracted_fields(form["schema"], prefilled, answers)
        if extractor.get_next_field(form["schema"], answers) is not None and not intent.message_is_only_form_name(user_text, form) and not intent.is_greeting_or_smalltalk(user_text):
            more_accepted = extractor.apply_extracted_answers(form["schema"], user_text, answers)
            accepted.update(more_accepted)

        if detected_key == "add_notice" and "wing" not in answers:
            answers["wing"] = "All"

        next_field = extractor.get_next_field(form["schema"], answers)

        if next_field is None:
            # All fields collected on first message!
            overview_msg = questions.build_form_overview_message(form, accepted=accepted)
            reply = f"{overview_msg}\n\n🎉 **All details have been extracted!** Please review and confirm in the pop-up modal."
            return make_response({
                "status": "ready",
                "reply": reply,
                "form": form,
                "form_key": detected_key,
                "answers": answers,
                "current_field": None,
                "ready_for_review": True,
                "accepted": accepted
            })
        else:
            question = questions.get_question_for_field(form, next_field)
            if accepted:
                overview_msg = questions.build_form_overview_message(form, accepted=accepted)
                reply = f"{overview_msg}\n\n{question}"
            else:
                reply = f"Let's fill out **{form['form_name']}**.\n\n{question}"
            return make_response({
                "status": "in_progress",
                "reply": reply,
                "form": form,
                "form_key": detected_key,
                "answers": answers,
                "current_field": next_field,
                "ready_for_review": False,
                "accepted": accepted
            })

    # 3. An active form exists
    form = db.fetch_form_schema(form_key)
    if not form:
        return JSONResponse({"error": "Active form not found"}, status_code=404)

    # If user provided a batch/compound multi-form request mid-way
    if is_probable_multi_action(user_text):
        plan = plan_and_extract(user_text)
        if plan and plan.get("is_multi_action"):
            if plan["all_complete"]:
                reply = f"🎉 **Identified and extracted {len(plan['actions'])} items!** Please review and confirm in the pop-up modal."
                return make_response({
                    "status": "ready",
                    "reply": reply,
                    "form": None,
                    "form_key": None,
                    "answers": {},
                    "current_field": None,
                    "current_action_id": None,
                    "is_batch": True,
                    "actions": plan["actions"],
                    "ready_for_review": True
                })
            else:
                incomplete = [a for a in plan["actions"] if not a["is_complete"]]
                first_inc = incomplete[0]
                missing_label = first_inc["missing_fields"][0] if first_inc.get("missing_fields") else "required details"
                next_fld = next((f for f in first_inc["schema"]["fields"] if f["label"] == missing_label), None)
                if next_fld:
                    next_fld = dict(next_fld)
                    next_fld["action_id"] = first_inc["id"]
                reply = f"I've drafted {len(plan['actions'])} items, but need **{missing_label}** for {first_inc['title']}."
                return make_response({
                    "status": "in_progress",
                    "reply": reply,
                    "form": db.fetch_form_schema(first_inc["form_key"]),
                    "form_key": first_inc["form_key"],
                    "answers": first_inc["fields"],
                    "current_field": next_fld,
                    "current_action_id": first_inc["id"],
                    "is_batch": True,
                    "actions": plan["actions"],
                    "ready_for_review": False
                })

    # Check if user wants to switch form mid-way
    switch_key = should_switch_to_form(user_text, form_key)
    if switch_key and switch_key != form_key:
        new_form = db.fetch_form_schema(switch_key)
        new_answers = {}
        accepted = {}
        if not intent.message_is_only_form_name(user_text, new_form):
            accepted = extractor.apply_extracted_answers(new_form["schema"], user_text, new_answers)
        if switch_key == "add_notice" and "wing" not in new_answers:
            new_answers["wing"] = "All"
        overview_msg = questions.build_form_overview_message(new_form, accepted=accepted)
        next_field = extractor.get_next_field(new_form["schema"], new_answers)
        if next_field is None:
            reply = f"{overview_msg}\n\n🎉 **All details extracted for {new_form['form_name']}!** Please review in the pop-up modal."
            return make_response({
                "status": "ready",
                "reply": reply,
                "form": new_form,
                "form_key": switch_key,
                "answers": new_answers,
                "current_field": None,
                "ready_for_review": True,
                "accepted": accepted
            })
        else:
            question = questions.get_question_for_field(new_form, next_field)
            reply = f"{overview_msg}\n\n{question}"
            return make_response({
                "status": "in_progress",
                "reply": reply,
                "form": new_form,
                "form_key": switch_key,
                "answers": new_answers,
                "current_field": next_field,
                "ready_for_review": False,
                "accepted": accepted
            })

    # Mid-form conversational / smalltalk check
    if intent.is_greeting_or_smalltalk(user_text) or any(w in user_text.lower().split() for w in ("hy", "helo", "hiii", "hey", "hello", "hi")):
        if not (current_field and not current_field.get("required", True) and user_text.strip().lower() in ("no", "none", "skip", "na", "n/a", "nil")):
            label = current_field["label"] if current_field else "the next field"
            conv_reply = generate_conversational_reply(user_text)
            reply = f"{conv_reply}\n\nWe are currently filling out **{form['form_name']}** — please provide {label.lower()} to continue, or type 'cancel' to exit."
            return make_response({
                "status": "in_progress",
                "reply": reply,
                "form": form,
                "form_key": form_key,
                "answers": answers,
                "current_field": current_field,
                "ready_for_review": False
            })

    # Handle field collection
    accepted = {}
    direct_match = False
    f_name = current_field.get("name") if (current_field and isinstance(current_field, dict)) else None
    is_field_unanswered = bool(
        f_name
        and (f_name not in answers or answers[f_name] is None or str(answers[f_name]).strip() == "" or str(answers[f_name]).lower() == "null")
    )

    # 1. If currently waiting for a specific field, test direct validation first
    if is_field_unanswered:
        ok, val = validation.validate_answer(current_field, user_text)
        if ok:
            answers[f_name] = val
            accepted[f_name] = val
            direct_match = True

    # 2. If the user provided a compound message that might contain other fields as well (not just answering current_field)
    if not direct_match or (any(c in user_text for c in (",", "\n", ";")) or " and " in user_text.lower()):
        extracted = extractor.apply_extracted_answers(form["schema"], user_text, answers)
        accepted.update(extracted)

    # 3. If neither direct validation nor extraction succeeded for the current field
    if is_field_unanswered and (f_name not in answers or answers[f_name] is None or str(answers[f_name]).strip() == ""):
        ok, val = validation.validate_answer(current_field, user_text)
        if not ok and not accepted:
            return make_response({
                "status": "validation_error",
                "reply": val,
                "form": form,
                "form_key": form_key,
                "answers": answers,
                "current_field": current_field,
                "is_batch": is_batch,
                "actions": actions,
                "ready_for_review": False
            })

    # 4. If this is an active batch session, sync newly answered field into the actions basket!
    if is_batch and actions:
        target_act = None
        if current_action_id:
            target_act = next((a for a in actions if a.get("id") == current_action_id), None)
        if not target_act:
            target_act = next((a for a in actions if a.get("form_key") == form_key and not a.get("is_complete")), None)
        if not target_act:
            target_act = next((a for a in actions if not a.get("is_complete")), None)

        if target_act:
            target_fields = target_act.setdefault("fields", {})
            if f_name and f_name in answers and answers[f_name] is not None and str(answers[f_name]).strip() != "":
                target_fields[f_name] = answers[f_name]
            for ak, av in accepted.items():
                if av is not None and str(av).strip() != "":
                    target_fields[ak] = av

            # Recalculate missing fields dynamically against target_act schema
            missing = []
            for fld in target_act["schema"]["fields"]:
                if fld.get("required", True):
                    fval = target_fields.get(fld["name"])
                    if fval is None or str(fval).strip() == "" or str(fval).lower() == "null":
                        missing.append(fld["label"])
            target_act["missing_fields"] = missing
            target_act["is_complete"] = len(missing) == 0

        # Check if ALL actions in the batch are now complete
        incomplete = [a for a in actions if not a.get("is_complete")]
        if not incomplete:
            reply = f"🎉 **All details complete for all {len(actions)} forms!** Please review and confirm in the pop-up modal."
            return make_response({
                "status": "ready",
                "reply": reply,
                "form": None,
                "form_key": None,
                "answers": {},
                "current_field": None,
                "current_action_id": None,
                "is_batch": True,
                "actions": actions,
                "ready_for_review": True
            })
        else:
            first_inc = incomplete[0]
            missing_label = first_inc["missing_fields"][0] if first_inc.get("missing_fields") else "required details"
            next_fld = next((f for f in first_inc["schema"]["fields"] if f["label"] == missing_label), None)
            if next_fld:
                next_fld = dict(next_fld)
                next_fld["action_id"] = first_inc["id"]

            reply = f"Saved {current_field['label'] if current_field else 'answer'}. Next, please provide **{missing_label}** for {first_inc['title']}."
            return make_response({
                "status": "in_progress",
                "reply": reply,
                "form": db.fetch_form_schema(first_inc["form_key"]),
                "form_key": first_inc["form_key"],
                "answers": first_inc["fields"],
                "current_field": next_fld,
                "current_action_id": first_inc["id"],
                "is_batch": True,
                "actions": actions,
                "ready_for_review": False
            })

    # Check next missing field (for standalone single form)
    next_field = extractor.get_next_field(form["schema"], answers)
    msg_prefix = ""
    if len(accepted) > 1:
        msg_prefix = questions.format_filled_message(form["schema"], answers, accepted) + "\n\n"

    if next_field is None:
        reply = msg_prefix + f"✨ **All fields for {form['form_name']} are complete!**\nOpening the review dialog so you can verify and submit."
        return make_response({
            "status": "ready",
            "reply": reply,
            "form": form,
            "form_key": form_key,
            "answers": answers,
            "current_field": None,
            "is_batch": False,
            "actions": [],
            "ready_for_review": True,
            "accepted": accepted
        })

    question = questions.get_question_for_field(form, next_field)
    reply = msg_prefix + question
    return make_response({
        "status": "in_progress",
        "reply": reply,
        "form": form,
        "form_key": form_key,
        "answers": answers,
        "current_field": next_field,
        "is_batch": False,
        "actions": [],
        "ready_for_review": False,
        "accepted": accepted
    })


async def submit_form_endpoint(request):
    """
    POST /api/forms/{key}/submit
    Submits finalized/edited answers from modal into SQLite DB.
    """
    form_key = request.path_params.get("key")
    form = db.fetch_form_schema(form_key)
    if not form:
        return JSONResponse({"error": "Form not found"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    answers = dict(body.get("answers") or {})
    for fld in form["schema"].get("fields", []):
        fname = fld.get("name", "")
        ftype = fld.get("type", "")
        val = answers.get(fname)
        if val is not None and str(val).strip():
            clean_str = str(val).strip()
            if "vehicle" in fname.lower() or "plate" in fname.lower():
                if not validation.is_valid_indian_plate(clean_str):
                    return JSONResponse(
                        {"error": f"Please enter a valid Indian vehicle number plate for {fld.get('label', fname)} (e.g. MH 12 AB 1234)."},
                        status_code=400,
                    )
                answers[fname] = validation.normalize_plate(clean_str)
            elif ftype == "date" or "date" in fname.lower():
                clean_date = validation.normalize_date(clean_str)
                if not clean_date:
                    return JSONResponse(
                        {"error": f"Please enter a valid date for {fld.get('label', fname)}."},
                        status_code=400,
                    )
                answers[fname] = clean_date

    final_data = extractor.build_final_data(form["schema"], answers)
    submission_id = db.submit_form(form["id"], final_data)

    if not submission_id:
        return JSONResponse({"error": "Database submission failed"}, status_code=500)

    summary = questions.format_completion_message(form["schema"], answers, submission_id)
    chat_id = body.get("chat_id")
    if chat_id:
        conn = db.get_connection()
        try:
            with conn:
                chat_row = conn.execute(
                    "SELECT messages_json FROM chats WHERE id = ?",
                    (chat_id,)
                ).fetchone()
                if chat_row:
                    try:
                        messages = json.loads(chat_row["messages_json"] or "[]")
                    except Exception:
                        messages = []
                    messages.append({
                        "role": "assistant",
                        "content": f"All done! Your **{form['form_name']}** has been recorded.",
                        "receipt_data": {
                            "submission_id": submission_id,
                            "answers": answers,
                        }
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
                        (json.dumps(messages), submission_id, chat_session.utc_now(), chat_id)
                    )
        except Exception as exc:
            log.warning("Failed to link chat submission: %s", exc)
        finally:
            conn.close()

    return JSONResponse({
        "success": True,
        "submission_id": submission_id,
        "summary": summary,
        "final_data": final_data
    })


async def submit_batch_endpoint(request):
    """
    POST /api/forms/submit-batch
    Submits multiple form records in a single atomic transaction.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    items = body.get("items") or []
    chat_id = body.get("chat_id")
    result = execute_batch_submission(items, chat_id=chat_id)
    return JSONResponse(result)


async def list_submissions_endpoint(request):
    """GET /api/submissions - returns recent submissions from database."""
    conn = db.get_connection()
    try:
        rows = conn.execute("""
            SELECT s.id, s.form_id, f.key as form_key, f.name as form_name, s.answers_json, s.created_at
            FROM form_submissions s
            JOIN forms f ON s.form_id = f.id
            ORDER BY s.id DESC LIMIT 30
        """).fetchall()
        submissions = [
            {
                "id": r["id"],
                "form_key": r["form_key"],
                "form_name": r["form_name"],
                "answers": json.loads(r["answers_json"] or "{}"),
                "created_at": r["created_at"]
            }
            for r in rows
        ]
        return JSONResponse(submissions)
    except Exception as exc:
        log.error("Failed to fetch submissions: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)
    finally:
        conn.close()


