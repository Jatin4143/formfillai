"""
Chat orchestration endpoint powered by LangGraph.
Executes the compiled StateGraph for turn-by-turn conversational routing,
form slot filling, compound extraction, and confirmation review modal triggers.
"""
import logging
from starlette.responses import JSONResponse

from core import db
from core.graph import FormChatState, compiled_chat_graph
from api.chat_sessions import save_chat_interaction

log = logging.getLogger("api.chat")


async def chat_endpoint(request):
    """
    POST /api/chat
    Processes conversational input via LangGraph StateGraph, manages form state,
    field extraction, and returns whether all fields are collected to trigger review modal.
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

    if not user_text:
        return JSONResponse({"error": "Empty message"}, status_code=400)

    form = db.fetch_form_schema(form_key) if form_key else None
    if form_key and not form:
        return JSONResponse({"error": "Active form not found"}, status_code=404)

    # Initial state for LangGraph workflow
    initial_state: FormChatState = {
        "user_text": user_text,
        "chat_id": chat_id,
        "form_key": form_key,
        "form": form,
        "answers": answers,
        "current_field": current_field,
        "current_action_id": current_action_id,
        "actions": actions,
        "is_batch": is_batch,
    }

    # Execute turn via compiled LangGraph
    try:
        result_state = compiled_chat_graph.invoke(initial_state)
    except Exception as e:
        log.exception("Error executing LangGraph workflow: %s", e)
        return JSONResponse({"error": f"Graph execution error: {str(e)}"}, status_code=500)

    # Format response payload to match exact frontend expectations
    res_data = {
        "status": result_state.get("status", "in_progress"),
        "reply": result_state.get("reply", ""),
        "form": result_state.get("form"),
        "form_key": result_state.get("form_key"),
        "answers": result_state.get("answers") or {},
        "current_field": result_state.get("current_field"),
        "current_action_id": result_state.get("current_action_id"),
        "is_batch": result_state.get("is_batch", False),
        "actions": result_state.get("actions") or [],
        "ready_for_review": result_state.get("ready_for_review", False),
        "accepted": result_state.get("accepted") or {},
    }

    # Save turn in chat history
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
