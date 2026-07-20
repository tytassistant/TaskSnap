"""Extraction: prompt builder + Poe API call + server-side shaping
(decision 8's third bullet). Ports buildAndSendPrompt/processExtractionResult/
buildEventTask/buildRegularTasks/isPriorityTask/formatDateSuffix/
getNextBusinessDay/getPreviousDay from the current tasksnap/index.html
(plan §7) -- same classification rules, same JSON-contract shape, same
due-date-default/title-suffix behavior.

Settings-backed rules (decision 6): priority_keywords and
list_override_rules come from crud.get_settings(), not hardcoded JS
constants. Everything else in the prompt -- the CRITICAL/SCOPE ISOLATION
instructional prose, the JSON-contract shape -- stays fixed template text
here, same reasoning as decision 6's "structured settings, not raw prompt
editing": letting a user edit that prose directly could silently break the
JSON contract the rest of the pipeline (this module's own parser) depends
on.

Pure extraction logic -- no crud.py/database.py imports. api.py calls
extract() and writes the returned shaped tasks into a draft itself.
"""

import base64
import json
import os
import re
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests

POE_API_URL = "https://api.poe.com/v1/chat/completions"
POE_MODEL = "GPT-5.2"

MONTH_ABBREVIATIONS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PoeClientError(Exception):
    """Raised for a missing API key, a Poe API failure, or an
    unparseable/empty response."""


def _get_poe_api_key() -> str:
    key = os.environ.get("POE_API_KEY")
    if not key:
        raise PoeClientError("POE_API_KEY is not set (systemd EnvironmentFile or environment)")
    return key


def to_data_url(content: bytes, content_type: str) -> str:
    """The `data:` URL format Poe's image_url.url field expects -- same
    shape the current JS produces from a FileReader / camera capture."""
    return f"data:{content_type};base64,{base64.b64encode(content).decode()}"


# ---------------------------------------------------------------------------
# Date helpers -- ports of getTodayDate/getNextBusinessDay/getPreviousDay/
# formatDateSuffix. "Today" is computed in the request's timezone (not the
# server VM's own), matching the original browser-based app reading the
# user's own local clock.
# ---------------------------------------------------------------------------

def today_date(timezone: str) -> str:
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).strftime("%Y-%m-%d")


def _parse_date(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y-%m-%d")


def get_next_business_day(date_str: str) -> str:
    d = _parse_date(date_str) + timedelta(days=1)
    if d.weekday() == 5:  # Saturday -> Monday
        d += timedelta(days=2)
    elif d.weekday() == 6:  # Sunday -> Monday
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def get_previous_day(date_str: str) -> str:
    return (_parse_date(date_str) - timedelta(days=1)).strftime("%Y-%m-%d")


def format_date_suffix(date_str: Optional[str]) -> str:
    if not date_str or not _DATE_ONLY_RE.match(date_str):
        return ""
    d = _parse_date(date_str)
    return f"{d.day}-{MONTH_ABBREVIATIONS[d.month - 1]}"


def is_priority_task(title: str, body: str, priority_keywords: list) -> bool:
    combined = f"{title or ''} {body or ''}".lower()
    return any(kw.lower() in combined for kw in priority_keywords)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _list_override_block(list_names: list, extra_examples: list) -> list:
    if not list_names:
        return []
    lines = [
        "",
        "=== LIST OVERRIDE ===",
        f"The user has the following task lists available: {json.dumps(list_names)}",
        "",
        "CRITICAL: Only set listOverride when the user EXPLICITLY names or references a specific "
        "list using clear list-assignment language. Examples of EXPLICIT list instructions:",
        "- 'english exam (quiz list)' → the parenthetical names a list",
        "- 'put homework in household list' → 'put ... in ... list' pattern",
        "- 'put all exams in quiz list' → explicit bulk assignment",
        "- 'maths homework >> homework' → explicit delimiter syntax",
    ]
    if extra_examples:
        lines.append("Additional examples the user has configured:")
        lines.extend(f"- {ex}" for ex in extra_examples)
    lines.extend([
        "",
        "NEVER set listOverride based on your own inference of which list a task belongs to. If the "
        "user just says 'maths assessment next Sunday' without mentioning any list, listOverride MUST "
        "be null -- even if you think you know which list it should go to. The app handles default "
        "list routing automatically based on task priority. Your job is ONLY to detect when the user "
        "explicitly asks for a specific list.",
        "",
        "SCOPE ISOLATION: Each task's listOverride must ONLY come from list instructions that directly "
        "accompany THAT specific task, OR from a global instruction that uses words like 'all', "
        "'every', 'everything' to indicate it applies to all tasks. Never propagate one task's specific "
        "list instruction to other tasks that have no list instruction of their own. For example, if "
        "the user writes 'Maths assessment due next Sunday / Register for boating. Put in Summer "
        "list', only the boating task gets listOverride -- the maths assessment has no list "
        "instruction and its listOverride MUST be null.",
        "",
        "When the user DOES explicitly specify a list:",
        "- Match the user's words to the closest list name from the available lists above.",
        "- If the user says something like 'put all in X list' or 'put everything in X', apply the "
        "override to ALL tasks/events.",
        "- If the user specifies overrides for some tasks but not others, only set listOverride on the "
        "specified tasks. Tasks without explicit list instructions should have listOverride set to "
        "null.",
        "- If you cannot confidently match the user's words to any available list, set listOverride to "
        'the user\'s words as-is (e.g. "work list").',
        "- The listOverride field should contain the EXACT list name from the available lists when a "
        "match is found, or the user's raw words when no match is found.",
        "",
    ])
    return lines


def build_image_prompt(today: str, list_names: list, priority_keywords: list, list_override_rules: list) -> str:
    keywords = ", ".join(priority_keywords)
    lines = [
        f"Analyze this image carefully. Today's date is: {today}",
        "",
        "=== STEP 1: CLASSIFY THE IMAGE ===",
        "Determine if this image is:",
        '- "todo_list": a handwritten or printed list of homework tasks, assignments, or things to do',
        '- "event": an event flier, event poster, event summary email/webpage, registration notice, '
        "competition announcement, workshop notice, or similar event information",
        "",
        "IMPORTANT: If the user instruction mentions ADDITIONAL tasks beyond what the image shows "
        "(e.g. the image is an event flier but the user also mentions homework or exams), you MUST "
        "return ALL items -- both from the image AND from the user instruction -- using the mixed "
        "format described below.",
        "",
        "=== STEP 2: EXTRACT BASED ON TYPE ===",
        "",
        '--- If type is "todo_list" (and no events) ---',
        "",
        "1. PHOTO DATE: Look at the top-left area of the image for a date (the photo date / header "
        "date). Extract it as photoDate in YYYY-MM-DD format. If no date is found, set photoDate to "
        "null.",
        "",
        "2. EXTRACT ALL TASKS visible in the image.",
        "",
        "3. SUBJECT GROUPING: Tasks in the photo are often grouped by subject using abbreviations "
        "like M (Maths), E (English), 中 (Chinese), 音 (Music), 人 (Liberal Studies/人文), "
        "Sci (Science), etc. When multiple tasks fall under the same subject heading, the subject is "
        "written once and sub-tasks are indented with numbers, bullets, or *. You MUST prefix EVERY "
        'task title with its subject in square brackets, e.g. "[Maths] Complete worksheet p.12" or '
        '"[中文] 默書溫紙". Always include the subject prefix even for the first task under a '
        "heading.",
        "",
        f"4. PRIORITY DETECTION: For each task, determine if it is a priority task. A task is priority "
        f"if its title or content relates to any of these keywords (case-insensitive): {keywords}. "
        'Set "priority" to true for these tasks, false otherwise.',
        "",
        "5. DATE PARSING -- CRITICAL: Dates in the image may appear in various formats. You MUST "
        "convert them accurately to YYYY-MM-DDTHH:mm:ss. Common formats include:",
        "   - Chinese dates: 6月25日, 六月五日, 6/5, 12月20日",
        "   - Shortened numerical: 6/5, 12/20, 5-6, 20/12",
        "   - With year: 2025年6月5日, 2025/6/5",
        "   - Without year: assume the current or next occurrence based on context",
        "   - If only a date is given with no time, default the time to 09:00:00",
        "   - For priority tasks (quiz/exam/test/assessment/dictation), pay EXTRA attention to "
        "correctly reading the due date -- these dates are critical.",
        "",
        "Return JSON:",
        "{",
        '  "type": "todo_list",',
        '  "photoDate": "YYYY-MM-DD" or null,',
        '  "tasks": [',
        "    {",
        '      "title": "string",',
        '      "body": "string (additional details; empty string if none)",',
        '      "dueDateTime": "YYYY-MM-DDTHH:mm:ss" or null,',
        '      "priority": true or false,',
        '      "listOverride": "exact list name" or null',
        "    }",
        "  ]",
        "}",
        "If a task is already marked as done/completed in the image, still include it but prepend "
        "[DONE] to the title.",
        "",
        '--- If type is "event" (and no additional tasks from user instruction) ---',
        "",
        "Extract the following from the event flier/email/webpage:",
        '- eventName: The name/title of the event (e.g. "Math Olympiad 2026", "School Science Fair")',
        "- registrationDeadline: The deadline/closing date for registration, in YYYY-MM-DD format. "
        'Look for phrases like "register by", "deadline", "closing date", "報名截止", '
        '"截止日期". null if not found.',
        "- registrationMethod: How to register -- include any URL links, email addresses, QR code "
        "mentions, form names, or description of the registration process. Combine all relevant info "
        "into one string. Empty string if not found.",
        "- taskDueDateTime: If the user instruction specifies WHEN the task should be due (e.g. 'in "
        "two days', 'tonight', 'by Friday'), compute it as YYYY-MM-DDTHH:mm:ss based on today's date. "
        "If the user does not specify a due date, set to null (the app will calculate a default).",
        "",
        "Return JSON:",
        "{",
        '  "type": "event",',
        '  "eventName": "string",',
        '  "registrationDeadline": "YYYY-MM-DD" or null,',
        '  "registrationMethod": "string or empty string",',
        '  "taskDueDateTime": "YYYY-MM-DDTHH:mm:ss" or null,',
        '  "listOverride": "exact list name" or null',
        "}",
        "",
        '--- If MIXED (image has an event AND user instruction mentions additional tasks, or vice '
        "versa) ---",
        "",
        "When the input contains BOTH event registration items AND regular tasks (e.g. user says "
        "'register for this event, also english exam next Monday and maths homework by 25 Mar'), you "
        "MUST return ALL items using this mixed format:",
        "",
        "Return JSON:",
        "{",
        '  "type": "mixed",',
        '  "photoDate": "YYYY-MM-DD" or null,',
        '  "tasks": [',
        "    {",
        '      "title": "string",',
        '      "body": "string (additional details; empty string if none)",',
        '      "dueDateTime": "YYYY-MM-DDTHH:mm:ss" or null,',
        '      "priority": true or false,',
        '      "listOverride": "exact list name" or null',
        "    }",
        "  ],",
        '  "events": [',
        "    {",
        '      "eventName": "string",',
        '      "registrationDeadline": "YYYY-MM-DD" or null,',
        '      "registrationMethod": "string or empty string",',
        '      "taskDueDateTime": "YYYY-MM-DDTHH:mm:ss" or null,',
        '      "listOverride": "exact list name" or null',
        "    }",
        "  ]",
        "}",
        "Apply the same PRIORITY DETECTION and DATE PARSING rules to the tasks array.",
        "",
    ]
    lines.extend(_list_override_block(list_names, list_override_rules))
    lines.append("=== IMPORTANT ===")
    lines.append("Return ONLY a valid JSON object (no other text, no markdown code blocks).")
    return "\n".join(lines)


def build_text_prompt(today: str, list_names: list, priority_keywords: list, list_override_rules: list) -> str:
    keywords = ", ".join(priority_keywords)
    lines = [
        f"The user wants to create tasks based on the following natural language input. Today's date "
        f"is {today}.",
        "",
        "The user may describe MULTIPLE items in a single input. Each item can be either a regular "
        "task or an event registration. You MUST extract ALL of them.",
        "",
        "For EACH item, classify it as:",
        "- regular task: homework, assignment, reminder, or thing to do",
        "- event: a request to register for an event, competition, workshop, activity, dinner, etc. "
        "(look for keywords like 'register', 'sign up', 'enrol', 'enroll', '報名')",
        "",
        "=== EXTRACTION RULES ===",
        "",
        "For regular tasks:",
        "- Extract a clear task title",
        "- Parse due date/time if mentioned (interpret relative dates like 'tonight' = today 21:00, "
        "'tomorrow' = next day 09:00, 'in two days' = 2 days from today 09:00, 'next Friday' = the "
        "coming Friday 09:00, etc.)",
        "- Extract any additional notes/details",
        f"- PRIORITY DETECTION: A task is priority if its title or content relates to any of these "
        f'keywords (case-insensitive): {keywords}. Set "priority" to true for these tasks, false '
        "otherwise.",
        "",
        "For events:",
        "- eventName: The name/title of the event",
        "- registrationDeadline: deadline date in YYYY-MM-DD format, null if not mentioned",
        "- registrationMethod: how to register (URL, email, etc.), empty string if not mentioned",
        "- taskDueDateTime: parse from user input as YYYY-MM-DDTHH:mm:ss, null if not specified",
        "",
        "=== RESPONSE FORMAT ===",
        "",
        "If the input contains ONLY regular tasks (no events):",
        "{",
        '  "type": "text_task",',
        '  "tasks": [',
        '    { "title": "string", "body": "string", "dueDateTime": "YYYY-MM-DDTHH:mm:ss" or null, '
        '"priority": true or false, "listOverride": "exact list name" or null }',
        "  ]",
        "}",
        "",
        "If the input contains ONLY event(s) (no regular tasks):",
        "{",
        '  "type": "event",',
        '  "eventName": "string",',
        '  "registrationDeadline": "YYYY-MM-DD" or null,',
        '  "registrationMethod": "string or empty string",',
        '  "taskDueDateTime": "YYYY-MM-DDTHH:mm:ss" or null,',
        '  "listOverride": "exact list name" or null',
        "}",
        "",
        "If the input contains BOTH events AND regular tasks:",
        "{",
        '  "type": "mixed",',
        '  "tasks": [',
        '    { "title": "string", "body": "string", "dueDateTime": "YYYY-MM-DDTHH:mm:ss" or null, '
        '"priority": true or false, "listOverride": "exact list name" or null }',
        "  ],",
        '  "events": [',
        '    { "eventName": "string", "registrationDeadline": "YYYY-MM-DD" or null, '
        '"registrationMethod": "string or empty string", "taskDueDateTime": '
        '"YYYY-MM-DDTHH:mm:ss" or null, "listOverride": "exact list name" or null }',
        "  ]",
        "}",
        "",
    ]
    lines.extend(_list_override_block(list_names, list_override_rules))
    lines.append("=== IMPORTANT ===")
    lines.append("Return ONLY a valid JSON object (no other text, no markdown code blocks).")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Poe API call
# ---------------------------------------------------------------------------

def call_poe(prompt_text: str, image_data_url: Optional[str] = None) -> str:
    content = [{"type": "text", "text": prompt_text}]
    if image_data_url:
        content.append({"type": "image_url", "image_url": {"url": image_data_url}})
    resp = requests.post(
        POE_API_URL,
        headers={"Authorization": f"Bearer {_get_poe_api_key()}", "Content-Type": "application/json"},
        json={"model": POE_MODEL, "stream": False, "messages": [{"role": "user", "content": content}]},
        timeout=120,
    )
    if resp.status_code >= 400:
        raise PoeClientError(f"Poe API error (HTTP {resp.status_code}): {resp.text}")
    data = resp.json()
    choices = data.get("choices") or []
    if not choices or not choices[0].get("message"):
        raise PoeClientError("Empty response from AI")
    text = choices[0]["message"].get("content") or ""
    if not text:
        raise PoeClientError("Empty response from AI")
    return text


# ---------------------------------------------------------------------------
# Response parsing + shaping (processExtractionResult and friends, §7)
# ---------------------------------------------------------------------------

def _extract_json_object(content: str) -> dict:
    text = content.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence_match:
        text = fence_match.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise PoeClientError("Could not parse AI response as JSON")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise PoeClientError(f"Could not parse AI response as JSON: {exc}")


def _build_event_task(ev: dict, user_instruction: str, upload_date: str) -> dict:
    event_name = (ev.get("eventName") or "Unknown Event").strip()
    deadline = ev.get("registrationDeadline") or None
    reg_method = (ev.get("registrationMethod") or "").strip()
    user_due = ev.get("taskDueDateTime") or None
    if user_due and re.match(r"^\d{4}-\d{2}-\d{2}T", user_due):
        due_datetime_str = user_due
    else:
        if deadline and _DATE_ONLY_RE.match(deadline):
            due_date = get_previous_day(deadline)
        else:
            due_date = upload_date
        due_datetime_str = f"{due_date}T21:00:00"
    due_date_only = due_datetime_str[:10]
    reminder_datetime = f"{due_date_only}T21:00:00.0000000"

    body_parts = []
    if reg_method:
        body_parts.append(f"Registration: {reg_method}")
    if deadline:
        body_parts.append(f"Registration deadline: {deadline}")
    if user_instruction:
        prefix = "\n" if body_parts else ""
        body_parts.append(f"{prefix}User note: {user_instruction}")

    deadline_suffix = f" ({format_date_suffix(deadline)})" if deadline else ""
    return {
        "kind": "event",
        "title": f"Register for {event_name}{deadline_suffix}",
        "body": "\n".join(body_parts),
        "due_datetime": due_datetime_str,
        "priority": False,
        "checked": True,
        "reminder_datetime": reminder_datetime,
        "list_name": ev.get("listOverride") or None,
        "has_specific_due_date": False,
    }


def _build_regular_tasks(
    tasks_arr: list, default_due_datetime: Optional[str], user_instruction: str,
    input_had_text: bool, priority_keywords: list,
) -> list:
    default_date_portion = default_due_datetime.split("T")[0] if default_due_datetime else None
    shaped = []
    for t in tasks_arr:
        ai_explicit_due = t.get("dueDateTime") or None
        due = ai_explicit_due
        if not due and default_due_datetime:
            due = default_due_datetime

        has_specific_due_date = False
        if ai_explicit_due:
            if not default_date_portion:
                has_specific_due_date = True
            else:
                ai_date_portion = ai_explicit_due.split("T")[0]
                has_specific_due_date = ai_date_portion != default_date_portion

        ai_date_part = None
        if has_specific_due_date:
            ai_date_part = ai_explicit_due.split("T")[0]
            due = f"{get_previous_day(ai_date_part)}T21:00:00"

        title_str = t.get("title") or "Untitled task"
        body_str = t.get("body") or ""
        # Priority is checked BEFORE appending the user instruction, to
        # avoid false positives from other tasks' keywords appearing in
        # the shared user-instruction text.
        pri = (t.get("priority") is True) or is_priority_task(title_str, body_str, priority_keywords)
        if user_instruction:
            body_str = (f"{body_str}\n\n" if body_str else "") + f"User note: {user_instruction}"

        date_suffix = f" ({format_date_suffix(ai_date_part)})" if has_specific_due_date else ""
        shaped.append({
            "kind": "task",
            "title": f"{title_str}{date_suffix}",
            "body": body_str,
            "due_datetime": due,
            "priority": pri,
            "checked": True if input_had_text else (pri or has_specific_due_date),
            "reminder_datetime": None,
            "list_name": t.get("listOverride") or None,
            "has_specific_due_date": has_specific_due_date,
        })
    return shaped


def parse_and_shape(
    content: str, user_instruction: str, upload_date: str,
    input_had_text: bool, priority_keywords: list,
) -> dict:
    """Returns {'photo_date': str|None, 'tasks': [shaped task dicts]} --
    port of processExtractionResult's routing + shaping (plan §7). Each
    shaped task dict has keys matching crud.add_draft_task's kwargs
    (kind/title/body/due_datetime/priority/reminder_datetime/list_name/
    checked/has_specific_due_date). Raises PoeClientError if the response
    can't be parsed as JSON or doesn't contain a usable tasks/events
    structure -- deliberately simpler than the current JS's fallback
    bare-array scan, which was a defensive branch for malformed AI output;
    not worth porting for a single-user app that can just retry."""
    parsed = _extract_json_object(content)
    parsed_type = parsed.get("type")

    if parsed_type == "mixed":
        photo_date = parsed.get("photoDate") or None
        events_arr = parsed.get("events") or []
        reg_tasks_arr = parsed.get("tasks") or []
        all_tasks = [_build_event_task(ev, user_instruction, upload_date) for ev in events_arr]
        default_due = None
        if photo_date and _DATE_ONLY_RE.match(photo_date):
            default_due = f"{get_next_business_day(photo_date)}T09:00:00"
        all_tasks += _build_regular_tasks(
            reg_tasks_arr, default_due, user_instruction, input_had_text, priority_keywords
        )
        return {"photo_date": photo_date, "tasks": all_tasks}

    if parsed_type == "event":
        return {"photo_date": None, "tasks": [_build_event_task(parsed, user_instruction, upload_date)]}

    # todo_list or text_task
    tasks_arr = parsed.get("tasks")
    if not isinstance(tasks_arr, list):
        raise PoeClientError("AI response did not include a tasks array")
    photo_date = parsed.get("photoDate") or None
    default_due_datetime = None
    if photo_date and _DATE_ONLY_RE.match(photo_date):
        default_due_datetime = f"{get_next_business_day(photo_date)}T09:00:00"
    all_tasks = _build_regular_tasks(
        tasks_arr, default_due_datetime, user_instruction, input_had_text, priority_keywords
    )
    return {"photo_date": photo_date, "tasks": all_tasks}


# ---------------------------------------------------------------------------
# Top-level entry point -- called by api.py's POST /api/extract handler
# ---------------------------------------------------------------------------

def extract(
    image_data_url: Optional[str], text: Optional[str], timezone: str, list_names: list,
    priority_keywords: list, list_override_rules: list,
) -> dict:
    """Builds the right prompt variant (image / image+text / text-only),
    calls Poe, parses + shapes the result. Raises PoeClientError on any
    failure (missing key, Poe API error, empty/unparseable response, no
    image and no text)."""
    has_photo = bool(image_data_url)
    user_text = (text or "").strip()
    if not has_photo and not user_text:
        raise PoeClientError("Provide an image, text, or both")

    upload_date = today_date(timezone)

    if has_photo and user_text:
        prompt = build_image_prompt(upload_date, list_names, priority_keywords, list_override_rules)
        prompt += (
            "\n\n=== USER INSTRUCTION ===\n"
            "The user provided the following instruction alongside the image. Use it as your PRIMARY "
            "GUIDE for how to interpret the image and what tasks to create. The user's instruction "
            "takes priority over the default classification rules above.\n\n"
            f'User says: "{user_text}"'
        )
    elif has_photo:
        prompt = build_image_prompt(upload_date, list_names, priority_keywords, list_override_rules)
    else:
        prompt = build_text_prompt(upload_date, list_names, priority_keywords, list_override_rules)
        prompt += f'\n\nUser input: "{user_text}"'

    content = call_poe(prompt, image_data_url if has_photo else None)
    return parse_and_shape(
        content, user_text, upload_date, input_had_text=bool(user_text), priority_keywords=priority_keywords
    )
