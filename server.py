
"""
Server bridge for FieldFlow AI Form Assistant.
Exposes REST API endpoints and serves the static frontend from the /frontend directory.
Wraps core logic from demo_paragraph.py without modifying or deleting existing files.
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

import demo_paragraph as dp

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR / "frontend"

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

    conn = dp.get_connection()
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
                meta = dp.form_meta_by_key(form_key)
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
                    dp.utc_now(),
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
    conn = dp.get_connection()
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
    conn = dp.get_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO chats (
                    id, title, title_locked, messages_json, answers_json,
                    form_key, current_field_name, completed, submission_id, updated_at
                ) VALUES (?, ?, 0, '[]', '{}', NULL, NULL, 0, NULL, ?)
                """,
                (new_id, title, dp.utc_now())
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
    conn = dp.get_connection()
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
        form_data = dp.fetch_form_schema(form_key) if form_key else None
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

    conn = dp.get_connection()
    try:
        with conn:
            cur = conn.execute(
                "UPDATE chats SET title = ?, title_locked = 1, updated_at = ? WHERE id = ?",
                (new_title[:60], dp.utc_now(), chat_id)
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
        dp.delete_chat_from_db(chat_id)
        return JSONResponse({"success": True, "id": chat_id})
    except Exception as exc:
        log.error("Failed to delete chat %s: %s", chat_id, exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


async def list_forms_endpoint(request):
    """GET /api/forms - returns all available forms."""
    forms = dp.list_forms()
    return JSONResponse(forms)


async def get_form_schema_endpoint(request):
    """GET /api/forms/{key}/schema - returns schema and fields for a form."""
    form_key = request.path_params.get("key")
    schema = dp.fetch_form_schema(form_key)
    if not schema:
        return JSONResponse({"error": "Form not found"}, status_code=404)
    return JSONResponse(schema)


def should_switch_to_form(user_text, current_form_key):
    """Check if user wants to switch to a different form without relying on streamlit session_state."""
    detected_key = dp.detect_form_key(user_text)
    if not detected_key or detected_key == current_form_key:
        return None
    meta = dp.form_meta_by_key(detected_key)
    if meta and dp.message_is_only_form_name(user_text, meta):
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
    chat_id = body.get("chat_id")

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
    if dp.is_cancel_command(user_text):
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
        short_reply = dp.get_short_reply(user_text)
        if short_reply:
            return make_response({
                "status": "idle",
                "reply": short_reply,
                "form": None,
                "form_key": None,
                "answers": {},
                "current_field": None,
                "ready_for_review": False
            })

        if dp.is_greeting(user_text) or dp.is_greeting_or_smalltalk(user_text):
            reply = dp.build_greeting_reply(dp.list_forms(), user_text=user_text)
            return make_response({
                "status": "greeting",
                "reply": reply,
                "form": None,
                "form_key": None,
                "answers": {},
                "current_field": None,
                "ready_for_review": False
            })

        direct_key = dp.detect_form_key(user_text)
        detected_key = direct_key
        prefilled = {}

        if not detected_key:
            detected_key, prefilled = dp.classify_and_extract(user_text)

        if not detected_key:
            reply = dp.chatbot_help_reply(user_text, dp.list_forms())
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
        form = dp.fetch_form_schema(detected_key)
        if not form:
            return JSONResponse({"error": "Form not found"}, status_code=404)

        answers = {}
        accepted = {}
        if prefilled:
            accepted = dp.merge_extracted_fields(form["schema"], prefilled, answers)
        if dp.get_next_field(form["schema"], answers) is not None and not dp.message_is_only_form_name(user_text, form) and not dp.is_greeting_or_smalltalk(user_text):
            more_accepted = dp.apply_extracted_answers(form["schema"], user_text, answers)
            accepted.update(more_accepted)

        overview_msg = dp.build_form_overview_message(form, accepted=accepted)
        next_field = dp.get_next_field(form["schema"], answers)

        if next_field is None:
            # All fields collected on first message!
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
            question = dp.get_question_for_field(form, next_field)
            reply = f"{overview_msg}\n\n{question}"
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
    form = dp.fetch_form_schema(form_key)
    if not form:
        return JSONResponse({"error": "Active form not found"}, status_code=404)

    # Check if user wants to switch form mid-way
    switch_key = should_switch_to_form(user_text, form_key)
    if switch_key and switch_key != form_key:
        new_form = dp.fetch_form_schema(switch_key)
        new_answers = {}
        accepted = {}
        if not dp.message_is_only_form_name(user_text, new_form):
            accepted = dp.apply_extracted_answers(new_form["schema"], user_text, new_answers)
        overview_msg = dp.build_form_overview_message(new_form, accepted=accepted)
        next_field = dp.get_next_field(new_form["schema"], new_answers)
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
            question = dp.get_question_for_field(new_form, next_field)
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

    # Mid-form smalltalk check
    if dp.is_greeting_or_smalltalk(user_text):
        if not (current_field and not current_field.get("required", True) and user_text.strip().lower() in ("no", "none", "skip", "na", "n/a", "nil")):
            label = current_field["label"] if current_field else "the next field"
            reply = f"Hello! We are currently filling out **{form['form_name']}**.\n\nPlease share {label.lower()} to continue, or type 'cancel' to exit."
            return make_response({
                "status": "in_progress",
                "reply": reply,
                "form": form,
                "form_key": form_key,
                "answers": answers,
                "current_field": current_field,
                "ready_for_review": False
            })

    # Handle field collection: first extract any fields from the message
    accepted = dp.apply_extracted_answers(form["schema"], user_text, answers)

    # If the current field was not populated by extraction, test if user_text directly answers it
    if current_field and current_field["name"] not in answers:
        ok, val = dp.validate_answer(current_field, user_text)
        if ok:
            answers[current_field["name"]] = val
            accepted[current_field["name"]] = val
        elif not accepted:
            # Failed validation and nothing else was extracted
            return make_response({
                "status": "validation_error",
                "reply": val,
                "form": form,
                "form_key": form_key,
                "answers": answers,
                "current_field": current_field,
                "ready_for_review": False
            })

    # Check next missing field
    next_field = dp.get_next_field(form["schema"], answers)
    msg_prefix = ""
    if len(accepted) > 1:
        msg_prefix = dp.format_filled_message(form["schema"], answers, accepted) + "\n\n"

    if next_field is None:
        reply = msg_prefix + f"✨ **All fields for {form['form_name']} are complete!**\nOpening the review dialog so you can verify and submit."
        return make_response({
            "status": "ready",
            "reply": reply,
            "form": form,
            "form_key": form_key,
            "answers": answers,
            "current_field": None,
            "ready_for_review": True,
            "accepted": accepted
        })

    question = dp.get_question_for_field(form, next_field)
    reply = msg_prefix + question
    return make_response({
        "status": "in_progress",
        "reply": reply,
        "form": form,
        "form_key": form_key,
        "answers": answers,
        "current_field": next_field,
        "ready_for_review": False,
        "accepted": accepted
    })


async def submit_form_endpoint(request):
    """
    POST /api/forms/{key}/submit
    Submits finalized/edited answers from modal into SQLite DB.
    """
    form_key = request.path_params.get("key")
    form = dp.fetch_form_schema(form_key)
    if not form:
        return JSONResponse({"error": "Form not found"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    answers = dict(body.get("answers") or {})
    final_data = dp.build_final_data(form["schema"], answers)
    submission_id = dp.submit_form(form["id"], final_data)

    if not submission_id:
        return JSONResponse({"error": "Database submission failed"}, status_code=500)

    summary = dp.format_completion_message(form["schema"], answers, submission_id)
    chat_id = body.get("chat_id")
    if chat_id:
        conn = dp.get_connection()
        try:
            with conn:
                conn.execute(
                    "UPDATE chats SET completed = 1, submission_id = ?, updated_at = ? WHERE id = ?",
                    (submission_id, dp.utc_now(), chat_id)
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


async def list_submissions_endpoint(request):
    """GET /api/submissions - returns recent submissions from database."""
    conn = dp.get_connection()
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


routes = [
    Route("/api/forms", list_forms_endpoint, methods=["GET"]),
    Route("/api/forms/{key}/schema", get_form_schema_endpoint, methods=["GET"]),
    Route("/api/chats", list_chats_endpoint, methods=["GET"]),
    Route("/api/chats", create_chat_endpoint, methods=["POST"]),
    Route("/api/chats/{id}", get_chat_endpoint, methods=["GET"]),
    Route("/api/chats/{id}", update_chat_endpoint, methods=["PUT"]),
    Route("/api/chats/{id}", delete_chat_endpoint, methods=["DELETE"]),
    Route("/api/chat", chat_endpoint, methods=["POST"]),
    Route("/api/forms/{key}/submit", submit_form_endpoint, methods=["POST"]),
    Route("/api/submissions", list_submissions_endpoint, methods=["GET"]),
    Mount("/", app=StaticFiles(directory=str(FRONTEND_DIR), html=True)),
]

middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    ),
    Middleware(NoCacheMiddleware),
]

app = Starlette(debug=True, routes=routes, middleware=middleware)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    log.info("Starting FieldFlow API Server on http://0.0.0.0:%s", port)
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=True)

