"""Extraction: prompt builder + Poe API call + server-side shaping.

list_table refactor: replaces the old fixed priority/other/event bucket
model. The AI extraction pass now does essentially all of the list-routing
work itself in one shot, because it has real context (the photo, the
phrasing) that Python substring-matching can't reliably approximate. Per
task/event, the AI returns:
  - categoryIdentified: ALWAYS populated (the default category unless the
    input explicitly indicates a different one -- see settings'
    default_category).
  - listIdentified: the exact list_name when confident (explicit naming,
    or a clear keyword match within that category's lists); null when
    genuinely unsure -- api.py's list_matcher.resolve_list() then applies
    a small deterministic fallback (the category's default-flagged list,
    or unmatched for manual assignment).

The old `priority` field and its global priority_keywords setting are
gone entirely -- superseded by each list's own `keywords`, which now
drive real routing instead of a separate cosmetic flag. The old `type`
field (todo_list/event/mixed/text_task) is also gone -- the response is
always {photoDate, tasks: [], events: []}, both arrays simply empty when
that kind isn't present.

Settings-backed rules (decision 6): list_override_rules comes from
crud.get_settings(), same as before. list_table itself (name/alt_names/
category/keywords per list) is the other settings-backed input, from
crud.list_all_list_entries(). Everything else in the prompt -- the
CRITICAL/SCOPE ISOLATION instructional prose, the JSON-contract shape --
stays fixed template text, same reasoning as before: letting a user edit
that prose directly could silently break the JSON contract the rest of
the pipeline (this module's own parser) depends on.

Pure extraction logic -- no crud.py/database.py imports. api.py calls
extract() with list_entries/default_category already fetched, and writes
the returned shaped tasks into a draft itself.
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


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _available_lists_block(list_entries: list, default_category: str, extra_examples: list) -> list:
    """The merged category+list block the AI uses to fill in
    categoryIdentified/listIdentified. list_entries come straight from
    crud.list_all_list_entries() -- list_name/list_alt_names/
    list_category/list_keywords per row."""
    if not list_entries:
        return [
            "",
            "=== AVAILABLE LISTS ===",
            "No lists are configured yet. Set categoryIdentified to "
            f'"{default_category}" (the default category) on every item, '
            "and leave listIdentified null on all of them.",
            "",
        ]
    lists_json = json.dumps([
        {
            "list_name": row["list_name"],
            "alt_names": row["list_alt_names"],
            "categories": row["list_category"],
            "keywords": row["list_keywords"],
        }
        for row in list_entries
    ])
    lines = [
        "",
        "=== AVAILABLE LISTS ===",
        "The user has the following task lists configured. Each list has a "
        "canonical name, optional alternate names (other ways the user might "
        "refer to it), belongs to one or more categories (a category groups "
        "lists by shared context -- e.g. a person's name, or a type of task), "
        "and optional keywords (what kind of task within that category "
        "belongs on that specific list):",
        "",
        lists_json,
        "",
        f'The default category is: "{default_category}"',
        "",
        "For EACH task/event you extract, determine two fields:",
        "",
        "STEP A -- categoryIdentified (ALWAYS fill this in, never null):",
        "- If the input explicitly indicates a different category (e.g. "
        "names a person, or an explicit context) than the default, set "
        "categoryIdentified to that EXACT category tag from the list above.",
        f'- Otherwise, set categoryIdentified to the default category: "{default_category}".',
        "",
        "STEP B -- listIdentified (fill in ONLY when confident; null otherwise):",
        "- If a specific list is explicitly named (by its exact name or an "
        "alt_name) for this item, set listIdentified to that list's EXACT "
        "list_name. This wins outright over any keyword-based guess.",
        "- Otherwise, using ONLY the lists whose categories include this "
        "item's categoryIdentified, compare the item's content against each "
        "candidate list's keywords. If exactly one list clearly fits, set "
        "listIdentified to that list's EXACT list_name.",
        "- If you genuinely can't tell which specific list fits (no clear "
        "keyword match, or it's ambiguous between two), set listIdentified "
        "to null -- do NOT guess. The app has a safe default list for this "
        "category.",
        "- NEVER invent a list_name that isn't in the AVAILABLE LISTS above "
        "-- if nothing matches, use null.",
        "",
        "SCOPE ISOLATION: category/list instructions apply only to the "
        "item(s) they directly accompany, or to ALL items if the "
        "instruction uses words like 'all'/'every'/'everything'. Never "
        "propagate one item's explicit instruction onto other items with "
        "no instruction of their own.",
        "",
        "Examples:",
        '- "these are all for Theo" -> categoryIdentified = "Theo" on every '
        "item this instruction applies to; listIdentified still determined "
        "per-item via keywords within Theo's lists (or null if unclear).",
        '- "put homework in household list" -> categoryIdentified = '
        '"Household", listIdentified = "Household Tasks" (an explicit '
        "'put X in Y list' pattern naming a specific list via its "
        "alt_name).",
        "- A quiz-sounding task with no explicit category/list mentioned "
        "-> categoryIdentified falls to the default category; "
        "listIdentified is set only if that category's lists have keywords "
        "matching \"quiz\"-like content, otherwise null.",
    ]
    if extra_examples:
        lines.append("Additional examples the user has configured:")
        lines.extend(f"- {ex}" for ex in extra_examples)
    lines.append("")
    return lines


_ITEM_CONTRACT = (
    '"categoryIdentified": "exact category tag", '
    '"listIdentified": "exact list name" or null'
)


def build_image_prompt(today: str, list_entries: list, default_category: str, list_override_rules: list) -> str:
    lines = [
        f"Analyze this image carefully. Today's date is: {today}",
        "",
        "=== STEP 1: FIND ITEMS ===",
        "Look for two kinds of items in the image (and in the user's "
        "instruction text, if any additional items are mentioned there "
        "beyond the image): regular tasks (homework, assignments, or "
        "things to do) and events (event fliers, event posters, event "
        "summary emails/webpages, registration notices, competition "
        "announcements, workshop notices, or similar). Extract ALL of both "
        "kinds you find -- an image may contain either, both, or (combined "
        "with instruction text) items of both kinds from two sources.",
        "",
        "=== STEP 2: FOR EACH REGULAR TASK ===",
        "",
        "1. PHOTO DATE: Look at the top-left area of the image for a date "
        "(the photo date / header date). Extract it as photoDate in "
        "YYYY-MM-DD format. If no date is found, set photoDate to null.",
        "",
        "2. SUBJECT GROUPING: Tasks in the photo are often grouped by "
        "subject using abbreviations like M (Maths), E (English), 中 "
        "(Chinese), 音 (Music), 人 (Liberal Studies/人文), Sci (Science), "
        "etc. When multiple tasks fall under the same subject heading, the "
        "subject is written once and sub-tasks are indented with numbers, "
        "bullets, or *. You MUST prefix EVERY task title with its subject "
        'in square brackets, e.g. "[Maths] Complete worksheet p.12" or '
        '"[中文] 默書溫紙". Always include the subject prefix even for the '
        "first task under a heading.",
        "",
        "3. DATE PARSING -- CRITICAL: Dates in the image may appear in "
        "various formats. You MUST convert them accurately to "
        "YYYY-MM-DDTHH:mm:ss. Common formats include:",
        "   - Chinese dates: 6月25日, 六月五日, 6/5, 12月20日",
        "   - Shortened numerical: 6/5, 12/20, 5-6, 20/12",
        "   - With year: 2025年6月5日, 2025/6/5",
        "   - Without year: assume the current or next occurrence based on context",
        "   - If only a date is given with no time, default the time to 09:00:00",
        "",
        "=== STEP 3: FOR EACH EVENT ===",
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
        "If a task is already marked as done/completed in the image, still include it but prepend "
        "[DONE] to the title.",
        "",
        "Return JSON (tasks/events are empty arrays when none of that kind exist):",
        "{",
        '  "photoDate": "YYYY-MM-DD" or null,',
        '  "tasks": [',
        "    {",
        '      "title": "string",',
        '      "body": "string (additional details; empty string if none)",',
        '      "dueDateTime": "YYYY-MM-DDTHH:mm:ss" or null,',
        f"      {_ITEM_CONTRACT}",
        "    }",
        "  ],",
        '  "events": [',
        "    {",
        '      "eventName": "string",',
        '      "registrationDeadline": "YYYY-MM-DD" or null,',
        '      "registrationMethod": "string or empty string",',
        '      "taskDueDateTime": "YYYY-MM-DDTHH:mm:ss" or null,',
        f"      {_ITEM_CONTRACT}",
        "    }",
        "  ]",
        "}",
        "",
    ]
    lines.extend(_available_lists_block(list_entries, default_category, list_override_rules))
    lines.append("=== IMPORTANT ===")
    lines.append("Return ONLY a valid JSON object (no other text, no markdown code blocks).")
    return "\n".join(lines)


def build_text_prompt(today: str, list_entries: list, default_category: str, list_override_rules: list) -> str:
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
        "",
        "For events:",
        "- eventName: The name/title of the event",
        "- registrationDeadline: deadline date in YYYY-MM-DD format, null if not mentioned",
        "- registrationMethod: how to register (URL, email, etc.), empty string if not mentioned",
        "- taskDueDateTime: parse from user input as YYYY-MM-DDTHH:mm:ss, null if not specified",
        "",
        "=== RESPONSE FORMAT ===",
        "",
        "Return JSON (tasks/events are empty arrays when none of that kind exist):",
        "{",
        '  "tasks": [',
        f'    {{ "title": "string", "body": "string", "dueDateTime": "YYYY-MM-DDTHH:mm:ss" or null, {_ITEM_CONTRACT} }}',
        "  ],",
        '  "events": [',
        f'    {{ "eventName": "string", "registrationDeadline": "YYYY-MM-DD" or null, '
        f'"registrationMethod": "string or empty string", "taskDueDateTime": '
        f'"YYYY-MM-DDTHH:mm:ss" or null, {_ITEM_CONTRACT} }}',
        "  ]",
        "}",
        "",
    ]
    lines.extend(_available_lists_block(list_entries, default_category, list_override_rules))
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
# Response parsing + shaping
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


def _build_event_task(ev: dict, user_instruction: str, upload_date: str, default_category: str) -> dict:
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
        "checked": True,
        "reminder_datetime": reminder_datetime,
        "category_identified": ev.get("categoryIdentified") or default_category,
        "list_identified": ev.get("listIdentified") or None,
        "has_specific_due_date": False,
    }


def _build_regular_tasks(
    tasks_arr: list, default_due_datetime: Optional[str], user_instruction: str,
    input_had_text: bool, default_category: str,
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
        list_identified = t.get("listIdentified") or None
        if user_instruction:
            body_str = (f"{body_str}\n\n" if body_str else "") + f"User note: {user_instruction}"

        date_suffix = f" ({format_date_suffix(ai_date_part)})" if has_specific_due_date else ""
        # Default-checked heuristic: for photo-only input, a task defaults
        # checked if the AI found a specific due date OR was confident
        # enough about the destination list to name one directly -- the
        # replacement for the old priority-keyword signal, now that
        # priority itself is gone.
        shaped.append({
            "kind": "task",
            "title": f"{title_str}{date_suffix}",
            "body": body_str,
            "due_datetime": due,
            "checked": True if input_had_text else (list_identified is not None or has_specific_due_date),
            "reminder_datetime": None,
            "category_identified": t.get("categoryIdentified") or default_category,
            "list_identified": list_identified,
            "has_specific_due_date": has_specific_due_date,
        })
    return shaped


def parse_and_shape(
    content: str, user_instruction: str, upload_date: str,
    input_had_text: bool, default_category: str,
) -> dict:
    """Returns {'photo_date': str|None, 'tasks': [shaped task dicts]}. Each
    shaped task dict has keys matching crud.add_draft_task's kwargs plus
    category_identified/list_identified (consumed by api.py's
    list_matcher.resolve_list call, not stored directly). Raises
    PoeClientError if the response can't be parsed as JSON or doesn't
    contain a usable tasks/events structure."""
    parsed = _extract_json_object(content)

    photo_date = parsed.get("photoDate") or None
    events_arr = parsed.get("events") or []
    tasks_arr = parsed.get("tasks") or []
    if not isinstance(events_arr, list) or not isinstance(tasks_arr, list):
        raise PoeClientError("AI response did not include usable tasks/events arrays")

    default_due_datetime = None
    if photo_date and _DATE_ONLY_RE.match(photo_date):
        default_due_datetime = f"{get_next_business_day(photo_date)}T09:00:00"

    all_tasks = [_build_event_task(ev, user_instruction, upload_date, default_category) for ev in events_arr]
    all_tasks += _build_regular_tasks(
        tasks_arr, default_due_datetime, user_instruction, input_had_text, default_category
    )
    return {"photo_date": photo_date, "tasks": all_tasks}


# ---------------------------------------------------------------------------
# Top-level entry point -- called by api.py's POST /api/extract handler
# ---------------------------------------------------------------------------

def extract(
    image_data_url: Optional[str], text: Optional[str], timezone: str, list_entries: list,
    default_category: str, list_override_rules: list,
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
        prompt = build_image_prompt(upload_date, list_entries, default_category, list_override_rules)
        prompt += (
            "\n\n=== USER INSTRUCTION ===\n"
            "The user provided the following instruction alongside the image. Use it as your PRIMARY "
            "GUIDE for how to interpret the image and what tasks to create. The user's instruction "
            "takes priority over the default classification rules above.\n\n"
            f'User says: "{user_text}"'
        )
    elif has_photo:
        prompt = build_image_prompt(upload_date, list_entries, default_category, list_override_rules)
    else:
        prompt = build_text_prompt(upload_date, list_entries, default_category, list_override_rules)
        prompt += f'\n\nUser input: "{user_text}"'

    content = call_poe(prompt, image_data_url if has_photo else None)
    return parse_and_shape(
        content, user_text, upload_date, input_had_text=bool(user_text), default_category=default_category
    )
