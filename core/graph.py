"""
LangGraph Conversational Flow Architecture for FieldFlow AI Form Assistant.
Defines StateGraph, nodes, conditional edges, and execution helpers for handling
both single-form and compound multi-form conversational interactions.
"""

import logging
import re
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import StateGraph, START, END

from core import db, validation, intent, extractor, questions
from core.multi_form_planner import (
    plan_and_extract,
    generate_conversational_reply,
)

log = logging.getLogger("core.graph")


# ============================================================================
# 1. State Schema
# ============================================================================

class FormChatState(TypedDict, total=False):
    # Session and Input parameters
    user_text: str
    chat_id: Optional[str]
    form_key: Optional[str]
    form: Optional[Dict[str, Any]]
    answers: Dict[str, Any]
    current_field: Optional[Dict[str, Any]]
    current_action_id: Optional[str]
    actions: List[Dict[str, Any]]
    is_batch: bool

    # Form switching & progress persistence (P2)
    previous_drafts: Dict[str, Any]

    # Routing & execution flags (P1)
    intent: str
    accepted: Dict[str, Any]
    is_complete: bool
    is_new_form_start: bool
    validation_error: Optional[str]

    # Response fields
    reply: str
    status: str  # "greeting" | "in_progress" | "ready" | "cancelled" | "validation_error" | "help"
    ready_for_review: bool
    chat_title: Optional[str]


# ============================================================================
# 2. Heuristics & Helpers (P2 Multi-action Improvements)
# ============================================================================

def is_probable_multi_action(user_text: str) -> bool:
    """
    Purely dynamic heuristic to detect multi-form or multi-record requests.
    Reads form definitions, aliases, and schema options directly from the database (forms.db).
    Contains no hardcoded static categories or form names.
    """
    text_lower = user_text.lower().strip()
    try:
        forms = db.list_forms()
        matched_form_keys = set()
        for f in forms:
            f_key = f.get("key")
            f_name = f.get("form_name", "").lower()
            key_phrase = f_key.replace("_", " ")

            # 1. Match form name or key
            if f_name in text_lower or key_phrase in text_lower:
                matched_form_keys.add(f_key)

            # 2. Match database aliases
            for a in f.get("aliases", []):
                a_str = str(a).strip().lower()
                if len(a_str) >= 3 and re.search(r"\b" + re.escape(a_str) + r"\b", text_lower):
                    matched_form_keys.add(f_key)

            # 3. Match schema field options from database (e.g. Lift, Water, Electricity)
            schema = db.fetch_form_schema(f_key)
            if schema and schema.get("schema"):
                for field in schema["schema"].get("fields", []):
                    for opt in (field.get("options") or []):
                        opt_str = str(opt).strip().lower()
                        if len(opt_str) >= 4 and re.search(r"\b" + re.escape(opt_str) + r"\b", text_lower):
                            matched_form_keys.add(f_key)

        # Multiple distinct database forms mentioned with coordinating punctuation/conjunctions
        if len(matched_form_keys) >= 2 and any(sep in text_lower for sep in (",", ";", "\n", " and ", " also ", " & ")):
            return True

        # Check for multiple quantity expressions paired with dynamic form names/aliases from DB
        # e.g. "two complaints", "2 visitors", "both notices"
        for f in forms:
            names = [f.get("form_name", "").lower(), f.get("key", "").replace("_", " ")] + [str(a).lower() for a in f.get("aliases", [])]
            for n in names:
                if len(n) >= 3:
                    if re.search(r"\b(two|three|four|2|3|4|both|multiple)\s+" + re.escape(n) + r"s?\b", text_lower):
                        return True

    except Exception:
        pass

    # Check for multiple distinct flat/unit numbers separated by coordinating conjunctions (e.g. '101 and 102')
    units = set(re.findall(r"\b[a-z]?\s*[-]?\s*\d{3,4}\b", text_lower))
    if len(units) >= 2 and any(sep in text_lower for sep in (",", ";", "\n", " and ", " & ")):
        return True

    # Check for compound intent clauses introducing an additional distinct request
    # e.g. "also raise a complaint", "and also need to register"
    COMPOUND_INTENT_RE = re.compile(
        r"\b(?:and\s+also|also|as\s+well\s+as)\s+(?:need\s+to\s+|want\s+to\s+)?(?:raise|create|add|log|file|register|book|submit)\b",
        re.IGNORECASE,
    )
    if COMPOUND_INTENT_RE.search(text_lower):
        return True

    return False


def should_switch_to_form(user_text: str, current_form_key: Optional[str]) -> Optional[str]:
    """Check if user wants to switch to a different form."""
    detected_key = intent.detect_form_key(user_text)
    if not detected_key or detected_key == current_form_key:
        return None
    meta = db.form_meta_by_key(detected_key)
    if not meta:
        return None

    # 1. Message is solely the form name (e.g. "raise complaint", "visitor entry")
    if intent.message_is_only_form_name(user_text, meta):
        return detected_key

    # 2. Explicit switch/alternative request (e.g. "switch to complaint", "actually raise complaint", "instead create notice")
    text_lower = user_text.lower()
    if any(w in text_lower for w in ("switch", "change", "instead", "rather", "actually", "different form", "new form")):
        return detected_key

    # 3. Explicit form command prefix (e.g. "I want to raise a complaint", "raise complaint for tap leak")
    form_name_lower = meta.get("form_name", "").lower()
    if re.search(r"\b(raise|create|add|log|register|file|book)\s+" + re.escape(form_name_lower) + r"\b", text_lower):
        return detected_key

    return None


# ============================================================================
# 3. Router Function (P1 & P3 Routing Verification)
# ============================================================================

def route_input(state: FormChatState) -> str:
    """
    Deterministic Router:
    Classifies user input and directs execution to exactly ONE mutually exclusive node.
    Prevents unintended parallel branch execution or race conditions.
    """
    user_text = state.get("user_text", "").strip()
    form_key = state.get("form_key")

    # 1. Cancel command takes precedence
    if intent.is_cancel_command(user_text):
        return "cancel"

    # 2. If NO active form is set
    if not form_key:
        # Check pure greeting/smalltalk (P3)
        if intent.is_greeting_or_smalltalk(user_text) or any(
            w in user_text.lower().split() for w in ("hy", "helo", "hiii", "hey", "hello", "hi")
        ):
            direct_key = intent.detect_form_key(user_text)
            if not direct_key and len(user_text.split()) < 12:
                return "greeting"

        # Check direct form name selection (fast path)
        direct_key = intent.detect_form_key(user_text)
        if direct_key:
            form = db.fetch_form_schema(direct_key)
            if form and intent.message_is_only_form_name(user_text, form):
                return "select_form"

        # Cold request with details/paragraph
        return "plan_or_classify"

    # 3. If an active form exists
    # Check if user mid-way entered a multi-form compound request (P2)
    if is_probable_multi_action(user_text):
        return "multi_action_override"

    # Check if user requested switching to a different form (P2)
    switch_key = should_switch_to_form(user_text, form_key)
    if switch_key and switch_key != form_key:
        return "switch_form"

    # Mid-form smalltalk check (P3: preserves active form without wiping state)
    if intent.is_greeting_or_smalltalk(user_text) or any(
        w in user_text.lower().split() for w in ("hy", "helo", "hiii", "hey", "hello", "hi")
    ):
        current_field = state.get("current_field")
        # If user replied 'skip', 'no', 'none' to an optional field, treat as answer, not greeting
        if not (
            current_field
            and not current_field.get("required", True)
            and user_text.strip().lower() in ("no", "none", "skip", "na", "n/a", "nil")
        ):
            return "mid_greeting"

    # Standard turn answering active question or supplying fields
    return "field_answer"


# ============================================================================
# 4. Graph Nodes (P1, P2, P3 Enhanced)
# ============================================================================

def node_route_intent(state: FormChatState) -> FormChatState:
    """
    Initial Router Node (P1 Fix):
    Explicitly resets per-turn status and validation_error flags so previous errors
    never persist into new turns, then records the classified intent.
    """
    classified_intent = route_input(state)
    return {
        **state,
        "intent": classified_intent,
        "status": "in_progress",       # P1: Reset per-turn status
        "validation_error": None,      # P1: Clear any lingering validation error
        "is_new_form_start": False,
    }


def node_cancel(state: FormChatState) -> FormChatState:
    """Handles cancellation while preserving conversation session identity and drafts."""
    return {
        **state,
        "status": "cancelled",
        "validation_error": None,
        "reply": "Current form filling was cancelled. Let me know whenever you'd like to fill a form.",
        "form": None,
        "form_key": None,
        "answers": {},
        "accepted": {},
        "current_field": None,
        "current_action_id": None,
        "actions": [],
        "is_batch": False,
        "ready_for_review": False,
        "is_new_form_start": False,
    }


def node_greeting(state: FormChatState) -> FormChatState:
    """Handles pleasantries and smalltalk when no form is active (P3)."""
    user_text = state.get("user_text", "")
    reply = generate_conversational_reply(user_text)
    return {
        **state,
        "status": "greeting",
        "validation_error": None,
        "reply": reply,
        "form": None,
        "form_key": None,
        "answers": {},
        "accepted": {},
        "current_field": None,
        "current_action_id": None,
        "actions": [],
        "is_batch": False,
        "ready_for_review": False,
        "is_new_form_start": False,
    }


def node_mid_greeting(state: FormChatState) -> FormChatState:
    """
    Handles smalltalk while actively filling a form without losing progress (P3).
    Reminds the user of the active field needed to continue.
    """
    user_text = state.get("user_text", "")
    form = state.get("form")
    form_key = state.get("form_key")
    if not form and form_key:
        form = db.fetch_form_schema(form_key)

    answers = state.get("answers") or {}
    current_field = state.get("current_field")
    if not current_field and form and "schema" in form:
        current_field = extractor.get_next_field(form["schema"], answers)

    conv_reply = generate_conversational_reply(user_text)
    form_name = form.get("form_name", "the form") if form else "the form"
    label_desc = f"your **{current_field['label']}**" if (current_field and "label" in current_field) else "the required details"
    reply = f"{conv_reply}\n\nWe are currently filling out **{form_name}** — please provide {label_desc} to continue, or type 'cancel' to exit."

    return {
        **state,
        "status": "in_progress",
        "validation_error": None,
        "reply": reply,
        "ready_for_review": False,
    }


def node_select_form(state: FormChatState) -> FormChatState:
    """Handles explicit form selection by name and resets prior field pointers."""
    user_text = state.get("user_text", "")
    direct_key = intent.detect_form_key(user_text)
    form = db.fetch_form_schema(direct_key)
    answers: Dict[str, Any] = {}
    if direct_key == "add_notice":
        answers["wing"] = "All"

    return {
        **state,
        "form": form,
        "form_key": direct_key,
        "answers": answers,
        "accepted": {},
        "current_field": None,
        "current_action_id": None,
        "actions": [],
        "is_batch": False,
        "is_new_form_start": True,
        "status": "in_progress",
        "validation_error": None,
    }


def node_switch_form(state: FormChatState) -> FormChatState:
    """
    Handles switching to a different form mid-way (P2 Fix):
    1. Saves previous form progress in state['previous_drafts'] to prevent data loss.
    2. Carries over overlapping fields (e.g. flat_number, phone_number, resident_name, wing).
    3. Restores any prior draft for the target form if user switched back.
    4. Extracts any newly stated fields from the switching message.
    5. Clears previous form's current_field pointer to prevent field contamination.
    """
    user_text = state.get("user_text", "")
    old_form_key = state.get("form_key")
    old_answers = dict(state.get("answers") or {})

    switch_key = intent.detect_form_key(user_text)
    new_form = db.fetch_form_schema(switch_key)
    if not new_form:
        return state

    # 1. Save previous form progress in drafts
    previous_drafts = dict(state.get("previous_drafts") or {})
    if old_form_key and old_answers:
        previous_drafts[old_form_key] = old_answers

    # 2. Check if target form has an existing draft or inherit overlapping fields
    new_answers: Dict[str, Any] = {}
    new_schema = new_form.get("schema", {})
    new_fields = new_schema.get("fields", [])
    new_field_names = {f["name"] for f in new_fields}

    # Restore draft if user previously worked on this form
    if switch_key in previous_drafts:
        new_answers.update(previous_drafts[switch_key])

    # Carry over overlapping/common fields from previous form (e.g. flat_number, phone_number, wing)
    for k, v in old_answers.items():
        if k in new_field_names and (k not in new_answers or new_answers[k] is None):
            if v is not None and str(v).strip() != "" and str(v).lower() != "null":
                new_answers[k] = v

    # 3. Extract any fields mentioned in the switching message itself
    accepted: Dict[str, Any] = {}
    if not intent.message_is_only_form_name(user_text, new_form):
        accepted = extractor.apply_extracted_answers(new_schema, user_text, new_answers)
    if switch_key == "add_notice" and "wing" not in new_answers:
        new_answers["wing"] = "All"

    return {
        **state,
        "form": new_form,
        "form_key": switch_key,
        "answers": new_answers,
        "accepted": accepted,
        "previous_drafts": previous_drafts,
        "current_field": None,       # Clear old field pointer
        "current_action_id": None,
        "actions": [],
        "is_batch": False,
        "is_new_form_start": True,
        "status": "in_progress",
        "validation_error": None,
    }


def node_multi_action_override(state: FormChatState) -> FormChatState:
    """Handles compound / batch requests received while in an active form (P2)."""
    user_text = state.get("user_text", "")
    plan = plan_and_extract(user_text)

    if plan and plan.get("is_multi_action") and plan.get("actions"):
        return {
            **state,
            "actions": plan["actions"],
            "is_batch": True,
            "form": None,
            "form_key": None,
            "answers": {},
            "current_field": None,
            "current_action_id": None,
            "accepted": {},
            "is_new_form_start": True,
            "status": "in_progress",
            "validation_error": None,
        }

    # If planner didn't confirm multi-action, fall back to treating message as standard answer
    return node_field_answer(state)


def node_plan_or_classify(state: FormChatState) -> FormChatState:
    """Plans multi-action compound forms or classifies single form starting from cold text."""
    user_text = state.get("user_text", "")

    # 1. Multi-form planner check
    plan = plan_and_extract(user_text)
    if plan and plan.get("actions"):
        if plan.get("is_multi_action"):
            return {
                **state,
                "is_batch": True,
                "actions": plan["actions"],
                "form": None,
                "form_key": None,
                "answers": {},
                "current_field": None,
                "current_action_id": None,
                "accepted": {},
                "is_new_form_start": True,
                "status": "in_progress",
                "validation_error": None,
            }
        else:
            # Single action identified by planner
            act = plan["actions"][0]
            single_form = db.fetch_form_schema(act["form_key"]) or {
                "key": act["form_key"],
                "form_name": act["form_name"],
                "schema": act["schema"],
            }
            single_answers = {
                k: v for k, v in (act.get("fields") or {}).items()
                if v is not None and str(v).strip() != "" and str(v).lower() != "null"
            }
            return {
                **state,
                "is_batch": False,
                "actions": [act],
                "form": single_form,
                "form_key": act["form_key"],
                "answers": single_answers,
                "current_action_id": None,
                "accepted": dict(single_answers),
                "is_new_form_start": True,
                "status": "in_progress",
                "validation_error": None,
            }

    # 2. Heuristic and LLM classification fallback
    detected_key = intent.detect_form_key(user_text)
    prefilled: Dict[str, Any] = {}
    if not detected_key:
        detected_key, prefilled = extractor.classify_and_extract(user_text)

    if not detected_key:
        reply = questions.chatbot_help_reply(user_text, db.list_forms())
        return {
            **state,
            "status": "help",
            "reply": reply,
            "form": None,
            "form_key": None,
            "answers": {},
            "current_field": None,
            "current_action_id": None,
            "actions": [],
            "is_batch": False,
            "ready_for_review": False,
            "is_new_form_start": False,
            "validation_error": None,
        }

    form = db.fetch_form_schema(detected_key)
    answers: Dict[str, Any] = {}
    accepted: Dict[str, Any] = {}
    if form:
        if prefilled:
            accepted = extractor.merge_extracted_fields(form["schema"], prefilled, answers)
        if extractor.get_next_field(form["schema"], answers) is not None and not intent.message_is_only_form_name(user_text, form) and not intent.is_greeting_or_smalltalk(user_text):
            more_accepted = extractor.apply_extracted_answers(form["schema"], user_text, answers)
            accepted.update(more_accepted)

        if detected_key == "add_notice" and "wing" not in answers:
            answers["wing"] = "All"

    return {
        **state,
        "is_batch": False,
        "actions": [],
        "form": form,
        "form_key": detected_key,
        "answers": answers,
        "accepted": accepted,
        "current_field": None,
        "current_action_id": None,
        "is_new_form_start": True,
        "status": "in_progress",
        "validation_error": None,
    }


def node_field_answer(state: FormChatState) -> FormChatState:
    """
    Processes user answer against active field, validations, and compound field extractions.
    P1 Fix: Sets validation_error on failure, and explicitly clears it upon successful answer.
    """
    user_text = state.get("user_text", "")
    form = state.get("form") or {}
    form_key = state.get("form_key")
    answers = dict(state.get("answers") or {})
    current_field = state.get("current_field")
    current_action_id = state.get("current_action_id")
    actions = list(state.get("actions") or [])
    is_batch = bool(state.get("is_batch", False))

    accepted: Dict[str, Any] = {}
    direct_match = False
    f_name = current_field.get("name") if (current_field and isinstance(current_field, dict)) else None
    is_field_unanswered = bool(
        f_name
        and (f_name not in answers or answers[f_name] is None or str(answers[f_name]).strip() == "" or str(answers[f_name]).lower() == "null")
    )

    # 1. Direct validation check
    if is_field_unanswered:
        ok, val = validation.validate_answer(current_field, user_text)
        if ok:
            answers[f_name] = val
            accepted[f_name] = val
            direct_match = True

    # 2. Compound extraction
    if not direct_match or (any(c in user_text for c in (",", "\n", ";")) or " and " in user_text.lower()):
        if form and "schema" in form:
            extracted = extractor.apply_extracted_answers(form["schema"], user_text, answers)
            accepted.update(extracted)

    # 3. Validation error check (P1 Fix: set validation_error flag)
    if is_field_unanswered and (f_name not in answers or answers[f_name] is None or str(answers[f_name]).strip() == ""):
        ok, val = validation.validate_answer(current_field, user_text)
        if not ok and not accepted:
            return {
                **state,
                "status": "validation_error",
                "validation_error": val,
                "reply": val,
                "answers": answers,
                "ready_for_review": False,
                "is_new_form_start": False,
            }

    # 4. Synchronize into batch actions basket if in batch mode
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

    # Successful answer processing: P1 Fix - explicitly clears validation_error
    return {
        **state,
        "status": "in_progress",
        "validation_error": None,
        "answers": answers,
        "actions": actions,
        "accepted": accepted,
        "is_new_form_start": False,
    }


def node_check_remaining(state: FormChatState) -> FormChatState:
    """
    Universal Completion Engine:
    Evaluates whether all required fields are satisfied across batch actions or single form.
    """
    is_batch = bool(state.get("is_batch", False))
    actions = list(state.get("actions") or [])

    if is_batch and actions:
        # Check completeness for each action
        for act in actions:
            fields = act.get("fields") or {}
            missing = []
            for fld in act.get("schema", {}).get("fields", []):
                if fld.get("required", True):
                    fval = fields.get(fld["name"])
                    if fval is None or str(fval).strip() == "" or str(fval).lower() == "null":
                        missing.append(fld["label"])
            act["missing_fields"] = missing
            act["is_complete"] = len(missing) == 0

        all_complete = all(a.get("is_complete", False) for a in actions)
        return {
            **state,
            "actions": actions,
            "is_complete": all_complete,
            "status": "in_progress",
            "validation_error": None,
        }

    # Single form completion check
    form = state.get("form")
    answers = state.get("answers") or {}
    if not form or "schema" not in form:
        form_key = state.get("form_key")
        if form_key:
            form = db.fetch_form_schema(form_key)

    if form and "schema" in form:
        next_field = extractor.get_next_field(form["schema"], answers)
        all_complete = (next_field is None)
        return {
            **state,
            "form": form,
            "current_field": next_field,
            "is_complete": all_complete,
            "status": "in_progress",
            "validation_error": None,
        }

    return {
        **state,
        "is_complete": False,
        "status": "in_progress",
        "validation_error": None,
    }


def node_ask_missing(state: FormChatState) -> FormChatState:
    """
    Formulates conversational prompt for the next pending field.
    Distinguishes initial form overview from turn-by-turn prompts to keep conversation clean.
    """
    is_batch = bool(state.get("is_batch", False))
    actions = list(state.get("actions") or [])

    if is_batch and actions:
        incomplete = [a for a in actions if not a.get("is_complete")]
        if incomplete:
            first_inc = incomplete[0]
            missing_label = first_inc["missing_fields"][0] if first_inc.get("missing_fields") else "required details"
            next_fld = next((f for f in first_inc["schema"]["fields"] if f["label"] == missing_label), None)
            if next_fld:
                next_fld = dict(next_fld)
                next_fld["action_id"] = first_inc["id"]

            prev_field = state.get("current_field")
            is_new_start = state.get("is_new_form_start", False)
            if not is_new_start and prev_field and isinstance(prev_field, dict) and prev_field.get("label"):
                prefix = f"Saved **{prev_field['label']}**. Next, please provide "
            else:
                prefix = f"I've drafted {len(actions)} items, but need "

            inc_title = first_inc.get("title") or first_inc.get("form_name") or first_inc.get("name") or first_inc.get("form_key", "this form")
            reply = f"{prefix}**{missing_label}** for {inc_title}."
            return {
                **state,
                "status": "in_progress",
                "validation_error": None,
                "reply": reply,
                "form": db.fetch_form_schema(first_inc["form_key"]),
                "form_key": first_inc["form_key"],
                "answers": first_inc["fields"],
                "current_field": next_fld,
                "current_action_id": first_inc["id"],
                "is_batch": True,
                "actions": actions,
                "ready_for_review": False,
            }

    form = state.get("form")
    answers = state.get("answers") or {}
    accepted = state.get("accepted") or {}
    is_new_start = state.get("is_new_form_start", False)
    next_field = state.get("current_field") or (extractor.get_next_field(form["schema"], answers) if form else None)

    question = questions.get_question_for_field(form, next_field) if (form and next_field) else ""

    if is_new_start and form:
        # First turn of the form: present clear context
        if accepted:
            overview_msg = questions.build_form_overview_message(form, accepted=accepted)
            reply = f"{overview_msg}\n\n{question}"
        else:
            reply = f"Let's fill out **{form['form_name']}**.\n\n{question}"
    else:
        # Continuing turns: keep replies concise without repeating entire overview
        msg_prefix = ""
        if len(accepted) > 1 and form:
            msg_prefix = questions.format_filled_message(form["schema"], answers, accepted) + "\n\n"
        reply = msg_prefix + question

    return {
        **state,
        "status": "in_progress",
        "validation_error": None,
        "reply": reply,
        "current_field": next_field,
        "ready_for_review": False,
    }


def node_prepare_review(state: FormChatState) -> FormChatState:
    """Prepares confirmation message triggering the pop-up review modal."""
    is_batch = bool(state.get("is_batch", False))
    actions = list(state.get("actions") or [])

    if is_batch and actions:
        reply = f"🎉 **Identified and extracted {len(actions)} items!** Please review and confirm in the pop-up modal."
        return {
            **state,
            "status": "ready",
            "validation_error": None,
            "reply": reply,
            "form": None,
            "form_key": None,
            "answers": {},
            "current_field": None,
            "current_action_id": None,
            "is_batch": True,
            "actions": actions,
            "ready_for_review": True,
        }

    form = state.get("form") or {}
    accepted = state.get("accepted") or {}
    answers = state.get("answers") or {}
    form_name = form.get("form_name", "the form")

    msg_prefix = ""
    if len(accepted) > 1 and form and "schema" in form:
        msg_prefix = questions.format_filled_message(form["schema"], answers, accepted) + "\n\n"

    reply = msg_prefix + f"✨ **All fields for {form_name} are complete!**\nOpening the review dialog so you can verify and submit."

    return {
        **state,
        "status": "ready",
        "validation_error": None,
        "reply": reply,
        "current_field": None,
        "ready_for_review": True,
    }


# ============================================================================
# 5. Graph Assembly & Compilation
# ============================================================================

def build_chat_graph():
    """Builds and compiles the complete FieldFlow LangGraph."""
    workflow = StateGraph(FormChatState)

    # Register nodes
    workflow.add_node("route_intent", node_route_intent)
    workflow.add_node("cancel", node_cancel)
    workflow.add_node("greeting", node_greeting)
    workflow.add_node("mid_greeting", node_mid_greeting)
    workflow.add_node("select_form", node_select_form)
    workflow.add_node("switch_form", node_switch_form)
    workflow.add_node("multi_action_override", node_multi_action_override)
    workflow.add_node("plan_or_classify", node_plan_or_classify)
    workflow.add_node("field_answer", node_field_answer)
    workflow.add_node("check_remaining", node_check_remaining)
    workflow.add_node("ask_missing", node_ask_missing)
    workflow.add_node("prepare_review", node_prepare_review)

    # Initial transition from START into the explicit route_intent node
    workflow.add_edge(START, "route_intent")

    # Conditional branching from route_intent node to the handler nodes
    def pick_intent_branch(state: FormChatState) -> str:
        return state.get("intent") or route_input(state)

    workflow.add_conditional_edges(
        "route_intent",
        pick_intent_branch,
        {
            "cancel": "cancel",
            "greeting": "greeting",
            "mid_greeting": "mid_greeting",
            "select_form": "select_form",
            "switch_form": "switch_form",
            "multi_action_override": "multi_action_override",
            "plan_or_classify": "plan_or_classify",
            "field_answer": "field_answer",
        },
    )

    # Direct edges for terminal conversational responses
    workflow.add_edge("cancel", END)
    workflow.add_edge("greeting", END)
    workflow.add_edge("mid_greeting", END)

    # Form initialization transitions to universal check
    workflow.add_edge("select_form", "check_remaining")
    workflow.add_edge("switch_form", "check_remaining")

    # Multi-action override: if validation failed, exit to END; else check_remaining
    def check_override_result(state: FormChatState) -> str:
        if state.get("validation_error") or state.get("status") == "validation_error":
            return END
        return "check_remaining"

    workflow.add_conditional_edges(
        "multi_action_override",
        check_override_result,
        {
            END: END,
            "check_remaining": "check_remaining",
        },
    )

    # Plan/classify: if help message, exit to END; else check_remaining
    def check_plan_result(state: FormChatState) -> str:
        if state.get("status") == "help":
            return END
        return "check_remaining"

    workflow.add_conditional_edges(
        "plan_or_classify",
        check_plan_result,
        {
            END: END,
            "check_remaining": "check_remaining",
        },
    )

    # Field answer (P1 Fix): if validation error, exit to END with error message; else check_remaining
    def check_answer_result(state: FormChatState) -> str:
        if state.get("validation_error") or state.get("status") == "validation_error":
            return END
        return "check_remaining"

    workflow.add_conditional_edges(
        "field_answer",
        check_answer_result,
        {
            END: END,
            "check_remaining": "check_remaining",
        },
    )

    # Check remaining: if all required fields are satisfied, prepare review; else ask missing
    def route_completion(state: FormChatState) -> str:
        if state.get("is_complete"):
            return "prepare_review"
        return "ask_missing"

    workflow.add_conditional_edges(
        "check_remaining",
        route_completion,
        {
            "prepare_review": "prepare_review",
            "ask_missing": "ask_missing",
        },
    )

    workflow.add_edge("ask_missing", END)
    workflow.add_edge("prepare_review", END)

    return workflow.compile()


# Global compiled instance
compiled_chat_graph = build_chat_graph()


class FormChatService:
    """Service wrapper for compiled LangGraph execution and visualization."""

    def __init__(self):
        self.graph = compiled_chat_graph

    def process(self, state: FormChatState) -> FormChatState:
        return self.graph.invoke(state)
