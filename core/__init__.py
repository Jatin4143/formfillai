"""
Core package for FieldFlow AI Form Assistant.
Re-exports database, validation, intent, extraction, question generation, and chat sessions.
"""

from core import (
    db,
    validation,
    intent,
    extractor,
    questions,
    chat_session,
    multi_form_planner,
    prompts,
    graph,
)
from core.graph import (
    FormChatState,
    FormChatService,
    build_chat_graph,
    compiled_chat_graph,
)
from core.db import (
    get_connection,
    init_db,
    list_forms,
    fetch_form_schema,
    submit_form,
    form_meta_by_key,
)
from core.validation import (
    normalize_plate,
    is_valid_indian_plate,
    normalize_date,
    is_valid_date,
    normalize_select_answer,
    validate_answer,
    INDIAN_PLATE_PATTERN,
    INDIAN_PLATE_RE,
    INDIAN_PLATE_EXTRACT_RE,
    EMAIL_RE,
    PHONE_RE,
    NUMBER_RE,
    NO_VEHICLE_PATTERNS,
    NO_VEHICLE_RE,
)
from core.intent import (
    get_domain_intent_words,
    invalidate_domain_words_cache,
    is_negative_or_closing,
    is_acknowledgment,
    is_greeting,
    get_short_reply,
    is_greeting_or_smalltalk,
    is_cancel_command,
    clean_form_selection_text,
    detect_form_key,
    message_is_only_form_name,
    should_switch_to_form,
)
from core.extractor import (
    get_next_field,
    is_all_filled,
    extract_heuristic_fields,
    extract_fields_from_text,
    accept_extracted_value,
    merge_extracted_fields,
    apply_extracted_answers,
    classify_and_extract,
    build_final_data,
)
from core.questions import (
    build_fallback_question,
    get_question_for_field,
    build_greeting_reply,
    build_form_overview_message,
    format_filled_message,
    format_completion_message,
    chatbot_help_reply,
)
from core.chat_session import (
    utc_now,
    load_chats_from_disk,
    save_chats_to_disk,
    delete_chat_from_db,
    make_chat,
    chat_local_date,
    group_chats_by_recency,
)
from core.prompts import (
    GENERIC_SYSTEM_PROMPT,
    EXTRACT_SYSTEM_PROMPT,
    CLASSIFY_SYSTEM_PROMPT,
    HELP_SYSTEM_PROMPT,
)

