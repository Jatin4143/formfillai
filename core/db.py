"""
Database access and initialization for FieldFlow AI.
Manages forms, form fields, submissions, and database connections.
"""

import json
import logging
import sqlite3
from pathlib import Path

from config import DB_PATH
from core.prompts import GENERIC_SYSTEM_PROMPT

log = logging.getLogger("core.db")


def get_connection():
    """Return a sqlite3 Connection with Row factory pointing to forms.db."""
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
            # Seed default forms only when the forms table is empty so restarts
            # do not inflate SQLite AUTOINCREMENT sequences via INSERT OR IGNORE.
            if conn.execute("SELECT COUNT(*) FROM forms").fetchone()[0] == 0:
                conn.executescript("""
                    INSERT OR IGNORE INTO forms (key, name, aliases) VALUES
                    ('add_notice', 'Add Notice',
                     '["add notice", "notice", "new notice", "create notice"]'),
                    ('visitor_entry', 'Visitor Entry',
                     '["visitor entry", "visitor", "guest", "add visitor", "visitor pass"]'),
                    ('raise_complaint', 'Raise Complaint',
                     '["raise complaint", "complaint", "issue", "report issue", "problem"]');

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

                    -- Facility Booking
                    INSERT OR IGNORE INTO forms (key, name, aliases) VALUES
                    ('facility_booking', 'Facility Booking',
                     '["facility booking", "book facility", "amenity booking", "clubhouse booking"]');

                    INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                    SELECT id, 'facility', 'Facility', 'select', 1, '["Clubhouse", "Party Hall", "Gym", "Swimming Pool", "Sports Court", "Other"]', 1
                    FROM forms WHERE key = 'facility_booking';

                    INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                    SELECT id, 'booking_date', 'Booking Date', 'date', 1, NULL, 2
                    FROM forms WHERE key = 'facility_booking';

                    INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                    SELECT id, 'time_slot', 'Time Slot', 'select', 1, '["Morning", "Afternoon", "Evening"]', 3
                    FROM forms WHERE key = 'facility_booking';

                    INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                    SELECT id, 'purpose', 'Purpose', 'textarea', 0, NULL, 4
                    FROM forms WHERE key = 'facility_booking';

                    INSERT OR IGNORE INTO form_fields (form_id, name, label, type, required, options, sort_order)
                    SELECT id, 'number_of_people', 'Number of People', 'number', 0, NULL, 5
                    FROM forms WHERE key = 'facility_booking';
                """)
            # Ensure any date fields are typed as 'date'
            conn.execute("UPDATE form_fields SET type = 'date' WHERE (name LIKE '%date%' OR label LIKE '%date%') AND type = 'text'")
    except sqlite3.Error as exc:
        log.error("Failed to initialize database: %s", exc)
    finally:
        conn.close()


def list_forms():
    """GET all forms with key, name, aliases."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, key, name, aliases FROM forms ORDER BY name"
        ).fetchall()
    except sqlite3.Error:
        log.error("GET forms failed")
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
    return forms


def fetch_form_schema(form_key):
    """Fetch full schema and field definitions for a form key."""
    conn = get_connection()
    try:
        form_row = conn.execute(
            "SELECT id, key, name, aliases FROM forms WHERE key = ?",
            (form_key,),
        ).fetchone()
        if form_row is None:
            log.warning("Form %s not found", form_key)
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
        log.error("Failed fetching schema for form %s", form_key)
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

    return {
        "id": form_row["id"],
        "key": form_row["key"],
        "form_name": form_row["name"],
        "aliases": json.loads(form_row["aliases"] or "[]"),
        "schema": {"form_name": form_row["name"], "fields": fields},
        "system_prompt": GENERIC_SYSTEM_PROMPT,
    }


def form_meta_by_key(form_key):
    """Find a form's metadata by key."""
    for form in list_forms():
        if form["key"] == form_key:
            return form
    return None


def submit_form(form_id, answers):
    """Insert submission record into form_submissions table and return submission id."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO form_submissions (form_id, answers_json) VALUES (?, ?)",
            (form_id, json.dumps(answers)),
        )
        submission_id = cur.lastrowid
        conn.commit()
        log.info("Form %s submitted successfully | id=%s", form_id, submission_id)
        return submission_id
    except sqlite3.Error as exc:
        log.error("Failed submitting form %s: %s", form_id, exc)
        return None
    finally:
        conn.close()

