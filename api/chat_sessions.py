"""
Chat session management and persistence endpoints.
"""
import json
import logging
import uuid
from starlette.responses import JSONResponse
from core import db, chat_session

log = logging.getLogger("api.chats")


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

