"""
Question generation and response formatting for FieldFlow AI.
Generates conversational single questions, overview messages, and completion summaries.
"""

import json
import logging

from config import MODEL, APP_NAME, ollama_client
from core.extractor import build_final_data
from core.intent import get_short_reply, is_greeting
from core.prompts import HELP_SYSTEM_PROMPT
import core.multi_form_planner as mfp

log = logging.getLogger("core.questions")


def build_fallback_question(field: dict) -> str:
    """Deterministic question when LLM is unavailable."""
    label = field["label"].lower()
    options = field.get("options") or []
    required = field.get("required", True)

    if options:
        q = f"What is the {label}? ({', '.join(options)})"
    else:
        q = f"What is the {label}?"

    if not required:
        q += ' (or type "skip")'
    return q


def get_question_for_field(form: dict, field: dict) -> str:
    """Ask Ollama to generate ONE polite conversational question for the field."""
    prompt = f"""
Form: {form['form_name']}

Field to collect:
{json.dumps(field, indent=2)}

Ask ONE natural, clear, and polite question to collect this field.
Return JSON:
{{
  "question": "..."
}}
"""

    try:
        response = ollama_client.chat(
            model=MODEL,
            format="json",
            messages=[
                {"role": "system", "content": form["system_prompt"]},
                {"role": "user", "content": prompt},
            ],
        )
        result = json.loads(response["message"]["content"])
        question = (result.get("question") or "").strip()
        if question:
            return question
    except Exception:
        pass

    return build_fallback_question(field)


def build_greeting_reply(forms=None, user_text=None) -> str:
    """Return a natural, 1-sentence model conversational reply."""
    if user_text:
        return mfp.generate_conversational_reply(user_text)
    return "Hello! I am here to help you fill out forms, register visitors, or report maintenance requests. How can I assist you today?"


def build_form_overview_message(form: dict, accepted: dict = None) -> str:
    """Produce a short, clean overview of the form fields."""
    schema = form["schema"]
    form_name = form["form_name"]

    real_accepted = {k: v for k, v in (accepted or {}).items() if v is not None and str(v).strip() != "" and str(v).lower() != "null"}
    if real_accepted:
        lines = [f"Let's fill out **{form_name}**.\n"]
        lines.append("Captured from your message:")
        for field in schema["fields"]:
            if field["name"] in real_accepted:
                lines.append(f"- {field['label']}: {real_accepted[field['name']]}")

        remaining = [f for f in schema["fields"] if f["name"] not in real_accepted]
        if remaining:
            lines.append("")
            lines.append("Still needed:")
            for idx, field in enumerate(remaining, 1):
                req_tag = "Required" if field.get("required", True) else "Optional"
                options = field.get("options")
                opts_str = f" ({', '.join(options)})" if options and field.get("type") == "select" else ""
                lines.append(f"{idx}. {field['label']} [{req_tag}]{opts_str}")
        return "\n".join(lines)

    return f"Let's fill out **{form_name}**."


def format_filled_message(schema: dict, answers: dict, accepted: dict) -> str:
    """List out fields captured in the current turn."""
    lines = []
    for field in schema["fields"]:
        if field["name"] not in accepted:
            continue
        shown = answers[field["name"]]
        if shown is None:
            shown = "skipped"
        lines.append(f"- {field['label']}: {shown}")
    return "I filled these from your message:\n" + "\n".join(lines)


def format_completion_message(schema: dict, answers: dict, submission_id=None) -> str:
    """Format final completion receipt message with JSON preview."""
    final_data = build_final_data(schema, answers)
    if submission_id:
        header = f"All done. Submitted as `#{submission_id}`."
    else:
        header = "All done."
    json_str = json.dumps(final_data, indent=2)
    return (
        f"{header}\n\n"
        f"```json\n{json_str}\n```\n\n"
        f"Type another form name or greeting to continue."
    )


def chatbot_help_reply(user_text: str, forms: list[dict]) -> str:
    """Natural, concise chatbot reply when no form is selected yet."""
    short = get_short_reply(user_text)
    if short:
        return short

    if is_greeting(user_text):
        return build_greeting_reply(forms, user_text=user_text)

    names = [form["form_name"] for form in forms] or ["(none yet)"]
    prompt = f"""Available forms: {", ".join(names)}

User message: {user_text}

Reply politely in 1 or 2 SHORT sentences maximum. Do not write essays or long lists."""
    try:
        response = ollama_client.chat(
            model=MODEL,
            messages=[
                {"role": "system", "content": HELP_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        text = (response["message"]["content"] or "").strip()
        if text:
            log.info("HELP reply done | preview=%s", text[:120])
            return text
    except Exception as exc:
        log.error("HELP reply failed | error=%s", exc)

    return "I can help you fill out any of our forms whenever you're ready. Just let me know!"

