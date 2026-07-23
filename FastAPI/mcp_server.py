"""Standalone MCP server for TaskSnap.

Exposes the app's JSON API as MCP tools so an AI assistant can extract
tasks from a photo/text, present them back to the user conversationally,
take edits, and sync to Microsoft To Do -- the whole point of plan §3
decision 8's draft model.

Thin HTTP client over /api/* (default http://127.0.0.1:8004, override with
TASKSNAP_API_URL) -- no business logic here. Extraction rules and shaping
(poe_client.py), Graph calls (graph_client.py), and the draft lifecycle
(crud.py) all live behind the API, so this server can never drift from
what the GUI does.

Human-in-the-loop (decision 3): deleting an ALREADY-SYNCED Microsoft To Do
task or checklist item (delete_task/delete_task_step) queues a pending
action for human approval (Settings -> Pending Approvals) -- same pattern
as portfolio-management, reserved for the one class of action an approval
click can't be second-guessed on: an already-real deletion. Every other
mutation of an already-synced task (update_task, add_task_step,
update_task_step, add_task_attachment) is immediate instead, on the same
exception as sync_draft/add_draft_new_list and draft mutations generally:
low blast-radius, easily corrected by another call, so the only safeguard
is the agent getting the user's explicit go-ahead in conversation first --
each of those tools' own docstring says so. Draft mutations (add/edit/
delete, plus add_draft_new_list) are immediate for a different reason: a
draft (or a pending new list on one) hasn't touched MS Graph yet, so
editing one has zero external blast radius until sync_draft runs.

Run manually for a smoke test:  python3 mcp_server.py
Register with Claude Code:      claude mcp add tasksnap -- python3 /path/to/mcp_server.py

Network mode -- for agents on other machines (LAN only; never exposed
through the public Traefik router, per plan §4):
    MCP_TRANSPORT=streamable-http MCP_AUTH_TOKEN=<secret> python3 mcp_server.py
serves the same tools at http://<host>:8766/mcp (override with MCP_HOST /
MCP_PORT). Every request must carry "Authorization: Bearer <secret>" or it
is rejected with 401 -- MCP_AUTH_TOKEN is mandatory in this mode.
"""

import base64
import os
import secrets as _secrets
import sys
from pathlib import Path
from typing import Any, Optional

import requests
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

API_BASE = os.environ.get("TASKSNAP_API_URL", "http://127.0.0.1:8004").rstrip("/")
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8766"))


def _load_api_key() -> Optional[str]:
    """The app's AuthGuard accepts X-API-Key from machine callers. Same
    source as the app itself: TASKSNAP_API_KEY env var, else the
    .tasksnap-api-key file the app persists next to this script. None is
    fine while auth isn't enabled yet (no active users)."""
    key = os.environ.get("TASKSNAP_API_KEY")
    if key:
        return key
    key_file = Path(__file__).parent / ".tasksnap-api-key"
    return key_file.read_text().strip() if key_file.exists() else None


API_KEY = _load_api_key()

# Same DNS-rebinding-protection tradeoff as portfolio-management: disabled
# because the mandatory bearer token in network mode already blocks what
# that check defends against (a rebinding attacker's browser can never
# attach our Authorization header), and the SDK's default Host-pinning
# would otherwise reject LAN clients.
mcp = FastMCP(
    "tasksnap", host=MCP_HOST, port=MCP_PORT,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _api(
    method: str, path: str, *, json: Any = None, params: Optional[dict] = None,
    data: Optional[dict] = None, files: Optional[dict] = None, timeout: int = 60,
) -> Any:
    """Calls the app's JSON API, surfacing its error detail as the tool
    error text so the assistant sees exactly what the server rejected and
    why. Longer default timeout than a typical CRUD call (60s) since
    extract_tasks involves a real LLM call (Poe) that can take a while."""
    try:
        resp = requests.request(
            method, f"{API_BASE}{path}", json=json, params=params, data=data, files=files,
            headers={"X-API-Key": API_KEY} if API_KEY else None,
            timeout=timeout,
        )
    except requests.ConnectionError:
        raise RuntimeError(
            f"Cannot reach the TaskSnap app at {API_BASE} -- is the FastAPI "
            "server running? Start it from the FastAPI directory with: "
            "python3 -m uvicorn main:app --host 127.0.0.1 --port 8004"
        )
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        raise RuntimeError(f"API error {resp.status_code}: {detail}")
    return resp.json()


def _queue(summary: str, method: str, path: str, payload: Optional[dict] = None) -> dict:
    """Decision 3's approval queue -- used only by delete_task/
    delete_task_step below (deleting an ALREADY-SYNCED MS To Do task or
    checklist item; that class of action keeps the formal approval click).
    Every other already-synced-task mutation calls _api directly instead,
    relying on in-conversation confirmation only. Draft mutations never go
    through this either: nothing has touched MS Graph at that point, so
    there's nothing external to protect against."""
    action = _api("POST", "/api/pending-actions", json={
        "summary": summary, "method": method, "path": path, "payload": payload,
    })
    return {
        "queued_for_approval": True,
        "pending_id": action["pending_id"],
        "summary": summary,
        "next_step": (
            "NOTHING HAS BEEN CHANGED YET. Tell the user this action is awaiting "
            "their approval in the web GUI under Settings -> Pending Approvals, "
            "then use check_pending_action to see the outcome once they've decided."
        ),
    }


# ---------------------------------------------------------------------------
# Config / settings (read-only)
# ---------------------------------------------------------------------------


@mcp.tool()
def get_config() -> dict:
    """Whether an MS account is linked (not the token itself) and the
    settings-backed default timezone. Check this before extract_tasks if
    you're unsure whether syncing will work at all."""
    return _api("GET", "/api/config")


@mcp.tool()
def get_settings() -> dict:
    """The editable extraction rules: list_override_rules (phrasing
    patterns the AI recognizes as an explicit list instruction),
    default_timezone, and default_category (the category assumed for a
    task/event unless the input explicitly indicates otherwise). For
    per-list category/keyword configuration -- which drives the actual
    routing decision -- use list_config_entries instead."""
    return _api("GET", "/api/settings")


@mcp.tool()
def list_task_lists() -> list:
    """The user's actual Microsoft To Do lists (id + displayName), fetched
    live from MS Graph. Useful before calling sync_draft with
    list_assignments, to check exact list names/ids rather than guessing.
    For the category/keyword configuration behind automatic routing (not
    just the raw list names), use list_config_entries instead."""
    return _api("GET", "/api/lists")


# ---------------------------------------------------------------------------
# Reading existing MS To Do tasks -- pure reads, so no approval gate
# (decision 3 only gates writes to already-synced tasks). Both tools
# resolve list_name against a live list_task_lists fetch, same
# case-insensitive match as everywhere else a list name is typed by hand.
# ---------------------------------------------------------------------------


def _resolve_list(list_name: str) -> dict:
    lists = _api("GET", "/api/lists")
    lower = list_name.strip().lower()
    exact = [l for l in lists if l["displayName"].lower() == lower]
    if exact:
        return exact[0]
    partial = [l for l in lists if lower in l["displayName"].lower()]
    if len(partial) == 1:
        return partial[0]
    if partial:
        raise RuntimeError(
            f'"{list_name}" matches multiple lists '
            f'({", ".join(l["displayName"] for l in partial)}) -- use the exact name.'
        )
    names = ", ".join(l["displayName"] for l in lists)
    raise RuntimeError(f'No Microsoft To Do list named "{list_name}". Available lists: {names}')


def _shape_task(task: dict) -> dict:
    due = task.get("dueDateTime") or {}
    notes = ((task.get("body") or {}).get("content") or "").strip()
    shaped = {
        "task_id": task["id"],
        "title": task.get("title"),
        "status": task.get("status"),
        "due_datetime": due.get("dateTime"),
    }
    if notes:
        shaped["notes"] = notes
    return shaped


@mcp.tool()
def list_tasks_in_list(list_name: str, status: str = "open") -> list:
    """Tasks in one Microsoft To Do list, fetched live from MS Graph.
    list_name is matched case-insensitively against the real list names
    (see list_task_lists), not list_config_entries. status is "open"
    (default -- not completed), "completed", or "all"."""
    lst = _resolve_list(list_name)
    tasks = _api("GET", f"/api/lists/{lst['id']}/tasks", params={"status": status})
    return [_shape_task(t) for t in tasks]


@mcp.tool()
def find_tasks_due(
    due_before: str, due_after: Optional[str] = None, list_name: Optional[str] = None, status: str = "open",
) -> list:
    """Tasks due on or before due_before (YYYY-MM-DD or a full ISO
    datetime), optionally also on or after due_after. Scoped to one list
    if list_name is given, otherwise searched across every real MS To Do
    list -- each result is tagged with list_name so a cross-list search
    stays readable. status defaults to "open" (not completed); pass
    "completed" or "all" to include finished tasks too."""
    params = {"status": status, "due_before": due_before}
    if due_after:
        params["due_after"] = due_after
    lists = [_resolve_list(list_name)] if list_name else _api("GET", "/api/lists")
    results = []
    for lst in lists:
        tasks = _api("GET", f"/api/lists/{lst['id']}/tasks", params=params)
        results.extend(dict(_shape_task(t), list_name=lst["displayName"]) for t in tasks)
    results.sort(key=lambda t: t.get("due_datetime") or "")
    return results


def _shape_step(item: dict) -> dict:
    return {
        "step_id": item["id"],
        "display_name": item.get("displayName"),
        "is_checked": bool(item.get("isChecked")),
    }


@mcp.tool()
def list_task_steps(list_id: str, task_id: str, status: str = "open") -> list:
    """Checklist items (steps) on one already-synced Microsoft To Do task
    (list_id/task_id from list_tasks_in_list, find_tasks_due, or a
    sync_draft result). status is "open" (default -- unchecked),
    "completed" (checked), or "all". Pure read, no approval gate."""
    items = _api("GET", f"/api/tasks/{list_id}/{task_id}/checklist-items", params={"status": status})
    return [_shape_step(i) for i in items]


# ---------------------------------------------------------------------------
# list_table (list_table refactor) -- category/keyword config per list,
# read/write so an agent can explain or manage routing conversationally,
# not just through the Settings GUI.
# ---------------------------------------------------------------------------


@mcp.tool()
def list_config_entries() -> list:
    """Every configured list_table row: list_name, list_alt_names
    (recognized synonyms), list_category (who/what context, e.g. a
    person's name), list_keywords (what kind of task routes here within
    that category), list_is_category_default (the fallback list for its
    category when nothing more specific matches). Read this to explain to
    the user why a task landed on a particular list, or before adding a
    new category."""
    return _api("GET", "/api/list-entries")


@mcp.tool()
def add_list_entry(
    list_name: str, list_alt_names: Optional[list] = None, list_category: Optional[list] = None,
    list_keywords: Optional[list] = None, list_is_category_default: bool = False,
) -> dict:
    """Adds a new list_table row -- e.g. to set up a brand-new category
    ("add a Tony quiz list") purely through conversation. list_name should
    match (or will become) a real Microsoft To Do list -- check
    list_task_lists first if you're not sure it already exists.
    list_is_category_default=True makes this the fallback for its
    category when the AI can't confidently pick a more specific list;
    setting it unsets that flag on any other list sharing the category."""
    payload = {"list_name": list_name, "list_is_category_default": list_is_category_default}
    if list_alt_names is not None:
        payload["list_alt_names"] = list_alt_names
    if list_category is not None:
        payload["list_category"] = list_category
    if list_keywords is not None:
        payload["list_keywords"] = list_keywords
    return _api("POST", "/api/list-entries", json=payload)


@mcp.tool()
def update_list_entry(
    list_id: str, list_name: Optional[str] = None, list_alt_names: Optional[list] = None,
    list_category: Optional[list] = None, list_keywords: Optional[list] = None,
    list_is_category_default: Optional[bool] = None,
) -> dict:
    """Edits one or more fields on an existing list_table row (list_id
    from list_config_entries). Only pass the fields actually changing."""
    payload = {}
    if list_name is not None:
        payload["list_name"] = list_name
    if list_alt_names is not None:
        payload["list_alt_names"] = list_alt_names
    if list_category is not None:
        payload["list_category"] = list_category
    if list_keywords is not None:
        payload["list_keywords"] = list_keywords
    if list_is_category_default is not None:
        payload["list_is_category_default"] = list_is_category_default
    return _api("PATCH", f"/api/list-entries/{list_id}", json=payload)


@mcp.tool()
def delete_list_entry(list_id: str) -> dict:
    """Removes a list_table row (list_id from list_config_entries) -- does
    NOT delete the real Microsoft To Do list, only its category/keyword
    configuration here."""
    return _api("DELETE", f"/api/list-entries/{list_id}")


# ---------------------------------------------------------------------------
# Extraction + drafts (decision 8). add/edit/delete are immediate -- a
# draft hasn't touched MS Graph yet. sync_draft is the exception (a real
# write); see its own docstring.
# ---------------------------------------------------------------------------


def _shape_draft_task(task: dict) -> dict:
    """Renames the raw task_checked column to to_sync for the agent-facing
    shape -- "checked" repeatedly got misread as the task's completion
    status. It is NOT that: a synced task's completion is update_task's
    status field (notStarted/completed/...); a checklist item's done-ness
    is update_task_step's is_checked. to_sync only ever means "should
    sync_draft create this in Microsoft To Do.\""""
    shaped = dict(task)
    shaped["to_sync"] = shaped.pop("task_checked")
    return shaped


def _shape_draft(draft: dict) -> dict:
    shaped = dict(draft)
    shaped["tasks"] = [_shape_draft_task(t) for t in draft["tasks"]]
    return shaped


@mcp.tool()
def extract_tasks(
    image_b64: Optional[str] = None, text: Optional[str] = None, timezone: Optional[str] = None,
) -> dict:
    """Extracts tasks/events from a photo and/or free text, and creates a
    draft holding the result. Provide at least one of image_b64 (raw
    base64 image bytes -- NOT a data: URL, this tool adds that prefix
    itself) or text.

    Returns the draft with its tasks, each carrying a task_id -- ALWAYS
    present these tasks back to the user (e.g. as a list) before calling
    sync_draft; never sync silently.

    Each task's task_list_name reflects the app's own automatic category/
    keyword routing (list_config_entries) -- confidently-routed and
    default-routed tasks already have a list; null means genuinely
    unmatched and needs a list before syncing (pass it via sync_draft's
    list_assignments, or edit_draft_task with list_name/category). A free
    text instruction like "put these under Tony" or "put this in the
    household list" is recognized automatically -- no separate call
    needed.

    Each task's to_sync reflects the app's own default-selection rules
    (date-specific tasks, or tasks the AI confidently routed to a list,
    default to_sync=True when the input was photo-only; everything
    defaults to_sync=True when any text was given) -- treat this as a
    starting point, but the user's own instructions in this conversation
    take precedence over it. to_sync is NOT a completion status -- it only
    controls whether sync_draft will create this task in Microsoft To Do
    at all; a task with to_sync=False just means "still under review,
    don't sync it yet."

    timezone affects both how due dates are interpreted and what's sent to
    Microsoft To Do at sync time -- omit it to use the app's configured
    default_timezone (see get_settings)."""
    if not image_b64 and not (text or "").strip():
        raise RuntimeError("Provide an image, text, or both.")
    data = {"created_via": "mcp"}
    if text is not None:
        data["text"] = text
    if timezone is not None:
        data["timezone"] = timezone
    files = None
    if image_b64:
        files = {"image": ("photo.jpg", base64.b64decode(image_b64), "image/jpeg")}
    return _shape_draft(_api("POST", "/api/extract", data=data, files=files))


@mcp.tool()
def list_drafts() -> list:
    """Every draft that exists (not just the one from this conversation) --
    draft_id, status ('open'/'synced'/'abandoned'), source, created time,
    and task_count (not the full task list -- use get_draft for that).
    Most recent first. Useful if the user asks about a past extraction you
    don't have in context, or wants to clean up old ones."""
    return _api("GET", "/api/drafts")


@mcp.tool()
def get_draft(draft_id: str) -> dict:
    """Current state of a draft, including all its tasks. Call this to
    re-ground yourself if the conversation has gone on for a while --
    always trust this over your own memory of the draft's contents."""
    return _shape_draft(_api("GET", f"/api/drafts/{draft_id}"))


@mcp.tool()
def delete_draft(draft_id: str) -> dict:
    """Deletes a whole draft and all its tasks -- for cleaning up an old
    or unwanted extraction. This is NOT the same as sync_draft's tasks
    being removed from Microsoft To Do -- an already-synced task is
    untouched by this; it only removes the draft's own bookkeeping.
    Confirm with the user before calling this if the draft has any tasks
    -- there's no undo."""
    return _api("DELETE", f"/api/drafts/{draft_id}")


@mcp.tool()
def add_draft_task(
    draft_id: str, kind: str, title: str,
    body: Optional[str] = None, due_datetime: Optional[str] = None,
    timezone: Optional[str] = None,
    reminder_datetime: Optional[str] = None, list_name: Optional[str] = None,
    category: Optional[str] = None, to_sync: bool = True,
) -> dict:
    """Adds a new task to an existing draft (kind: 'task' or 'event').
    Give list_name if you know the exact destination list; otherwise give
    category (e.g. "Tony") and the app resolves it to that category's
    default list itself -- you don't need to already know which specific
    list that is (check list_config_entries if you want to know anyway).
    list_name wins if both are given.

    to_sync is NOT a completion status -- it only controls whether
    sync_draft will create this task in Microsoft To Do at all.
    to_sync=True (default) means "include it next time sync_draft runs";
    to_sync=False means "keep it in the draft for now, don't sync yet."
    (A synced task's own completion is a separate thing entirely -- see
    update_task's status field.)

    Returns the full updated draft -- show it to the user so they can
    confirm the add looks right before any sync."""
    payload = {"kind": kind, "title": title, "checked": to_sync}
    if body is not None:
        payload["body"] = body
    if due_datetime is not None:
        payload["due_datetime"] = due_datetime
    if timezone is not None:
        payload["timezone"] = timezone
    if reminder_datetime is not None:
        payload["reminder_datetime"] = reminder_datetime
    if list_name is not None:
        payload["list_name"] = list_name
    if category is not None:
        payload["category"] = category
    return _shape_draft(_api("POST", f"/api/drafts/{draft_id}/tasks", json=payload))


@mcp.tool()
def edit_draft_task(
    draft_id: str, task_id: str,
    title: Optional[str] = None, body: Optional[str] = None,
    due_datetime: Optional[str] = None, timezone: Optional[str] = None,
    reminder_datetime: Optional[str] = None,
    list_name: Optional[str] = None, list_id: Optional[str] = None,
    category: Optional[str] = None, to_sync: Optional[bool] = None,
) -> dict:
    """Edits one or more fields on an existing draft task. task_id must be
    an exact id from a previous get_draft/extract_tasks/add_draft_task
    result -- resolve which task the user means yourself (e.g. "the quiz
    one") using the draft state already in front of you; never guess by
    position ("the second one"). Only pass the fields actually changing --
    everything else is left untouched.

    category works the same as on add_draft_task (resolved to that
    category's default list) -- only applied when list_name isn't also
    given in this same call. to_sync works the same as on add_draft_task
    (it's not a completion status -- just whether sync_draft should create
    this task at all). Returns the full updated draft -- always show it
    back to the user so they can catch a misinterpretation immediately,
    rather than assuming the edit landed as intended."""
    payload = {}
    if title is not None:
        payload["title"] = title
    if body is not None:
        payload["body"] = body
    if due_datetime is not None:
        payload["due_datetime"] = due_datetime
    if timezone is not None:
        payload["timezone"] = timezone
    if reminder_datetime is not None:
        payload["reminder_datetime"] = reminder_datetime
    if list_name is not None:
        payload["list_name"] = list_name
    if list_id is not None:
        payload["list_id"] = list_id
    if category is not None:
        payload["category"] = category
    if to_sync is not None:
        payload["checked"] = to_sync
    return _shape_draft(_api("PATCH", f"/api/drafts/{draft_id}/tasks/{task_id}", json=payload))


@mcp.tool()
def delete_draft_task(draft_id: str, task_id: str) -> dict:
    """Removes a task from a draft before it's ever synced -- this never
    touches MS To Do. Returns the full updated draft."""
    return _shape_draft(_api("DELETE", f"/api/drafts/{draft_id}/tasks/{task_id}"))


@mcp.tool()
def add_draft_new_list(
    draft_id: str, list_name: str, list_alt_names: Optional[list] = None,
    list_category: Optional[list] = None, list_keywords: Optional[list] = None,
    list_is_category_default: bool = False,
) -> dict:
    """Registers intent to create list_name as a brand-new REAL Microsoft
    To Do list (with this category/keyword routing config) the next time
    this draft is synced -- e.g. "there's no Tony Quiz list yet, let's make
    one and put this task there." This call itself is immediate and never
    touches MS Graph -- it only adds a new_lists entry to the draft (see
    get_draft). The real list is only created when sync_draft runs for
    this draft, under the SAME rule as sync_draft's tasks: state clearly
    that a new list will be created and get the user's explicit
    conversational go-ahead first -- there is no separate approval queue
    for this, sync_draft's own confirmation is the only safeguard.

    To actually put a task on this not-yet-real list, set that draft
    task's list_name (via add_draft_task/edit_draft_task) to the same
    list_name given here -- sync_draft resolves it to the list it just
    created, in the same call. Returns the full updated draft."""
    payload = {"list_name": list_name, "list_is_category_default": list_is_category_default}
    if list_alt_names is not None:
        payload["list_alt_names"] = list_alt_names
    if list_category is not None:
        payload["list_category"] = list_category
    if list_keywords is not None:
        payload["list_keywords"] = list_keywords
    return _shape_draft(_api("POST", f"/api/drafts/{draft_id}/new-lists", json=payload))


@mcp.tool()
def delete_draft_new_list(draft_id: str, new_list_id: str) -> dict:
    """Cancels a pending add_draft_new_list registration before it's ever
    synced -- this never touches MS To Do. new_list_id comes from
    get_draft's new_lists. Returns the full updated draft."""
    return _shape_draft(_api("DELETE", f"/api/drafts/{draft_id}/new-lists/{new_list_id}"))


@mcp.tool()
def sync_draft(draft_id: str, list_assignments: Optional[dict] = None) -> dict:
    """Creates any lists this draft has pending via add_draft_new_list,
    then creates the draft's to_sync=True, not-yet-synced tasks in
    Microsoft To Do (with a photo attachment if the draft has one) -- the
    step that actually writes to the user's real account. ALWAYS state
    exactly which tasks (and any new lists) you're about to create and get
    the user's explicit go-ahead in this conversation before calling this
    -- there is no separate approval queue for it (decision 3: creating
    tasks/lists is low-blast-radius, same as the app's own GUI sync
    button), so this confirmation is the only safeguard in place.

    list_assignments is optional: {task_id: list_name} overrides for tasks
    that don't already have a list assigned (or to redirect one that
    does) -- everything else syncs using whatever list is already stored
    on it. A list_assignments name that doesn't exist yet in Microsoft To
    Do is created automatically -- but this only applies to THIS manual
    override path; the AI's own automatic routing (during extract_tasks)
    never invents a new list itself, it only ever names one of the
    already-configured list_config_entries or leaves the task unmatched.
    Prefer add_draft_new_list over this override when the new list should
    also get routing config (category/keywords/alt names) for future
    auto-routing -- a list created only via this override gets no
    list_table config at all.

    Returns {"draft": ..., "results": [...], "new_list_results": [...],
    "skipped_not_to_sync": [...]}. results/new_list_results are per-task/
    per-new-list ("synced"/"created" or "failed" with a detail) -- report
    any failures to the user rather than assuming the whole batch
    succeeded. skipped_not_to_sync lists any task that's still sitting in
    the draft, untouched, because its to_sync is False -- if you expected
    a task to be created and it shows up here instead of in results, that
    means it was never actually marked to_sync=True (use edit_draft_task/
    add_draft_task's to_sync field, or ask the user, then call sync_draft
    again)."""
    payload = {}
    if list_assignments is not None:
        payload["list_assignments"] = list_assignments
    result = _api("POST", f"/api/drafts/{draft_id}/sync", json=payload)
    result["draft"] = _shape_draft(result["draft"])
    result["skipped_not_to_sync"] = result.pop("skipped_unchecked")
    return result


# ---------------------------------------------------------------------------
# Already-synced MS To Do tasks (decision 3). update_task/add_task_step/
# add_task_attachment/update_task_step are immediate -- same exception as
# sync_draft/add_draft_new_list: get the user's explicit go-ahead in
# conversation first, since that confirmation is the only safeguard.
# delete_task/delete_task_step stay queued for HUMAN APPROVAL -- undoing an
# already-real deletion isn't as simple as undoing an add/edit, so that
# higher-stakes pair keeps the formal Settings -> Pending Approvals gate.
# ---------------------------------------------------------------------------


@mcp.tool()
def update_task(
    list_id: str, task_id: str,
    title: Optional[str] = None, body: Optional[str] = None,
    due_datetime: Optional[str] = None, timezone: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
    """Edits an ALREADY-SYNCED Microsoft To Do task -- applied
    IMMEDIATELY, no approval queue: state exactly what you're about to
    change and get the user's explicit go-ahead in this conversation
    before calling this -- that confirmation is the only safeguard in
    place (same exception as sync_draft/add_draft_new_list). Only for a
    task that already exists in MS To Do (list_id/task_id from a previous
    sync_draft result or a Graph lookup -- NOT a draft's task_id). For a
    task still sitting in a draft, use edit_draft_task instead. status is
    one of notStarted/inProgress/completed/waitingOnOthers/deferred -- use
    "completed" to mark done, "notStarted" to reopen."""
    payload = {}
    if title is not None:
        payload["title"] = title
    if body is not None:
        payload["body"] = body
    if due_datetime is not None:
        payload["due_datetime"] = due_datetime
    if timezone is not None:
        payload["timezone"] = timezone
    if status is not None:
        payload["status"] = status
    return _api("PATCH", f"/api/tasks/{list_id}/{task_id}", json=payload)


@mcp.tool()
def delete_task(list_id: str, task_id: str) -> dict:
    """Queues deleting an ALREADY-SYNCED Microsoft To Do task for HUMAN
    APPROVAL -- nothing changes until the user approves it (Settings ->
    Pending Approvals). For a task still sitting in a draft, use
    delete_draft_task instead -- that's immediate, no approval needed."""
    summary = f"Delete task {task_id} from list {list_id}"
    return _queue(summary, "DELETE", f"/api/tasks/{list_id}/{task_id}")


@mcp.tool()
def add_task_step(list_id: str, task_id: str, display_name: str) -> dict:
    """Adds a checklist item (step) to an ALREADY-SYNCED task -- applied
    IMMEDIATELY, no approval queue (same exception as update_task): get
    the user's explicit go-ahead in this conversation before calling
    this."""
    return _api("POST", f"/api/tasks/{list_id}/{task_id}/checklist-items", json={"display_name": display_name})


@mcp.tool()
def update_task_step(list_id: str, task_id: str, step_id: str, is_checked: bool) -> dict:
    """Checks or unchecks a step on an ALREADY-SYNCED task -- applied
    IMMEDIATELY, no approval needed: toggling a step is low blast-radius
    and trivially reversed by toggling it back. step_id comes from
    list_task_steps."""
    return _api(
        "PATCH", f"/api/tasks/{list_id}/{task_id}/checklist-items/{step_id}", json={"is_checked": is_checked}
    )


@mcp.tool()
def delete_task_step(list_id: str, task_id: str, step_id: str) -> dict:
    """Queues deleting a checklist item (step) from an ALREADY-SYNCED task
    for HUMAN APPROVAL -- nothing changes until the user approves it
    (Settings -> Pending Approvals). step_id comes from list_task_steps."""
    summary = f"Delete step {step_id} from task {task_id} in list {list_id}"
    return _queue(summary, "DELETE", f"/api/tasks/{list_id}/{task_id}/checklist-items/{step_id}")


@mcp.tool()
def add_task_attachment(
    list_id: str, task_id: str, file_base64: str, filename: str, content_type: str,
) -> dict:
    """Attaches a file of ANY type -- PDF, photo, spreadsheet, whatever --
    to an ALREADY-SYNCED Microsoft To Do task.

    HOW TO BUILD file_base64, exactly: take the file's raw binary bytes --
    however you actually have access to them (downloaded from this chat,
    read from a local path, fetched from a URL, etc.) -- and base64-encode
    THOSE EXACT BYTES into a plain string. In Python that's literally
    `base64.b64encode(raw_bytes).decode()`. Concretely, this argument must
    NOT be: a data: URL (e.g. "data:application/pdf;base64,..."), a file
    path or filename, the literal word "ContentBytes" or any other
    placeholder, or an empty string. It must be the real, non-empty
    base64 text produced by encoding the actual file's bytes -- nothing
    else. An empty or malformed value is rejected with a clear 422 before
    this ever reaches Microsoft Graph; if that happens, or if the error
    mentions "ContentBytes", it means the value you passed didn't decode
    to real file content -- go back and re-read/re-encode the file itself
    rather than retrying this call unchanged, which will fail identically.

    For files 3 MB or larger, Microsoft Graph rejects this base64 path
    outright -- use get_task_attachment_upload_url instead, which needs no
    encoding and supports files up to 25 MB via a plain file upload.

    This is a generic file attachment, not a photo-only tool: pick
    filename/content_type to genuinely match the real file (e.g.
    filename="invoice.pdf", content_type="application/pdf"; there is no
    default for either, on purpose, so a mismatched type never sneaks
    through silently). Applied IMMEDIATELY, no approval queue (same
    exception as update_task): get the user's explicit go-ahead in this
    conversation before calling this.

    Only for a task that already exists in MS To Do (list_id/task_id from
    list_tasks_in_list, find_tasks_due, or a sync_draft result); a draft's
    own task photo attaches automatically on sync_draft instead."""
    payload = {"photo_base64": file_base64, "filename": filename, "content_type": content_type}
    return _api("POST", f"/api/tasks/{list_id}/{task_id}/attachments", json=payload)


@mcp.tool()
def get_task_attachment_upload_url(list_id: str, task_id: str, filename: str, content_type: str) -> dict:
    """Mints a single-use upload link for attaching a file to an ALREADY-
    SYNCED Microsoft To Do task. Use THIS tool, not add_task_attachment,
    whenever EITHER is true: (1) the file is 3 MB or larger
    (add_task_attachment's base64 path is hard-capped under 3 MB by
    Microsoft Graph -- it will be rejected there regardless of how it's
    encoded), or (2) you cannot easily produce valid base64 of the file's
    exact bytes yourself. If your file is under 3 MB and you CAN produce
    base64 without friction, use add_task_attachment instead -- it's one
    tool call instead of two steps.

    This tool sends NO file bytes and does NO encoding of any kind -- it
    only returns a plain upload_url; you still have to actually upload the
    file to it as a second step (see below), which needs no base64 and no
    script.

    After calling this, perform a normal HTTP file upload: a
    multipart/form-data POST of the raw file to upload_url, with the file
    in a field named "file" -- the same kind of thing as a browser's
    <input type="file"> form submit, NOT a script, NOT code execution, and
    NOT base64 text in a JSON body. Example from a shell:
    `curl -F "file=@/path/to/file.pdf" "<upload_url>"`. Use whatever plain
    multipart-upload capability your own platform offers for "upload this
    file to this URL" -- not anything that runs code.

    upload_url is SINGLE-USE and expires at expires_datetime_utc (15
    minutes from now): if the upload doesn't happen in time, fails
    partway, or you already used it, do not retry the same upload_url --
    call this tool again for a fresh one.

    filename/content_type must genuinely match the real file (e.g.
    filename="invoice.pdf", content_type="application/pdf") -- there is no
    default for either, on purpose. File can be anything from 3 MB up to
    Microsoft Graph's 25 MB per-task-attachment ceiling (larger files are
    rejected with a clear error, not silently truncated).

    Only for a task that already exists in MS To Do (list_id/task_id from
    list_tasks_in_list, find_tasks_due, or a sync_draft result); a draft's
    own task photo attaches automatically on sync_draft instead. Get the
    user's explicit go-ahead in this conversation before calling this --
    same exception as update_task/add_task_attachment (applied immediately
    once the upload completes, no approval queue)."""
    payload = {"filename": filename, "content_type": content_type}
    return _api("POST", f"/api/tasks/{list_id}/{task_id}/attachments/upload-requests", json=payload)


@mcp.tool()
def check_pending_action(pending_id: Optional[str] = None) -> Any:
    """Status of a queued delete_task/delete_task_step action (or, with no
    id, every still-pending one). status 'pending' = the user hasn't decided
    yet. 'approved' with pending_result null = approved and still
    executing -- poll again; 'approved' with pending_result set = done,
    the API response is in it. 'rejected' = the user declined it.
    'failed' = approved but the replayed call errored -- pending_result
    holds the exact error, so read it, fix the payload, and queue a
    corrected action."""
    if pending_id:
        return _api("GET", f"/api/pending-actions/{pending_id}")
    return _api("GET", "/api/pending-actions", params={"status": "pending"})


class _BearerAuth:
    """Pure-ASGI middleware: rejects any HTTP request lacking the expected
    Authorization: Bearer token with a 401. Applied only in network mode."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope["headers"]).get(b"authorization", b"")
            if not _secrets.compare_digest(got, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"),
                                        (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await self.app(scope, receive, send)


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run()
    elif transport == "streamable-http":
        token = os.environ.get("MCP_AUTH_TOKEN")
        if not token:
            sys.exit("MCP_TRANSPORT=streamable-http requires MCP_AUTH_TOKEN to be "
                     "set -- this endpoint is network-facing and must not run open.")
        import uvicorn
        uvicorn.run(_BearerAuth(mcp.streamable_http_app(), token), host=MCP_HOST, port=MCP_PORT)
    else:
        sys.exit(f"Unknown MCP_TRANSPORT '{transport}' (use 'stdio' or 'streamable-http').")
