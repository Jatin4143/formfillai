"""
Chat session persistence and history management in SQLite.
"""

from datetime import date, datetime, timedelta, timezone
import json
import logging
import sqlite3
import uuid

from core.db import get_connection

log = logging.getLogger("core.chat_session")


def utc_now() -> str:
    """Return current UTC timestamp in ISO 8601 string format."""
    return datetime.now(timezone.utc).isoformat()


def load_chats_from_disk() -> list[dict]:
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


def save_chats_to_disk(chats: list[dict]):
    """Save or update chat sessions in SQLite DB (forms.db)."""
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


def delete_chat_from_db(chat_id: str):
    """Delete a chat session from SQLite DB (forms.db)."""
    conn = get_connection()
    try:
        with conn:
            conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
    except sqlite3.Error as exc:
        log.error("Failed to delete chat %s from DB: %s", chat_id, exc)
    finally:
        conn.close()


def make_chat(title: str = "Untitled") -> dict:
    """Create a new empty chat session object."""
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


def chat_local_date(chat: dict) -> date:
    """Extract local date from chat updated_at timestamp."""
    raw = chat.get("updated_at") or ""
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().date()
    except ValueError:
        return date.today()


def group_chats_by_recency(chats: list[dict]) -> list[tuple[str, list[dict]]]:
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

