"""
Form submissions API endpoints (single and batch).
"""
import json
import logging
from starlette.responses import JSONResponse
from core import db, validation, extractor, questions, chat_session
from core.multi_form_planner import execute_batch_submission

log = logging.getLogger("api.submissions")


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

