"""MS Graph calls: To Do lists, task create/update/delete, photo
attachments. Ports findOrCreateList/matchListOverride/executeSyncTasks/
normalizeDateTimeForGraph from the current tasksnap/index.html (plan §7)
-- same request shapes, same 401/403 "auth error" classification, same
"attachment failure doesn't fail the task" behavior. Every call gets its
token from auth_ms.get_valid_access_token(); callers here never handle
refresh themselves.

Pure Graph API wrapper -- no crud.py/database.py imports. The
draft-sync orchestration (looping over a draft's tasks, deciding which
list each one goes to, marking rows synced) lives in api.py, which calls
into this module.
"""

import re
from typing import Optional

import requests

import auth_ms

GRAPH_BASE = "https://graph.microsoft.com/v1.0/me/todo"


class GraphError(Exception):
    """Raised for any Graph API failure. is_auth_error is True for 401/403
    -- same classification as the current JS's parseGraphError/isAuthError,
    used by the sync loop (api.py) to abort the rest of a batch instead of
    retrying calls that would all fail the same way."""

    def __init__(self, status_code: int, code: str = "", message: str = ""):
        self.status_code = status_code
        self.code = code
        self.is_auth_error = status_code in (401, 403)
        if not message:
            if status_code == 401:
                message = "Access token is invalid, expired, or missing required permissions."
            elif status_code == 403:
                message = "Insufficient permissions. The Tasks.ReadWrite scope may not be consented."
        self.message = message
        parts = [f"HTTP {status_code}"]
        if code:
            parts.append(code)
        if message:
            parts.append(message)
        super().__init__(" — ".join(parts))


def _headers(timezone: Optional[str] = None) -> dict:
    headers = {
        "Authorization": f"Bearer {auth_ms.get_valid_access_token()}",
        "Content-Type": "application/json",
    }
    if timezone:
        headers["Prefer"] = f'outlook.timezone="{timezone}"'
    return headers


def _raise_for_graph_error(resp: requests.Response) -> None:
    if resp.status_code < 400:
        return
    code, message = "", ""
    try:
        body = resp.json()
        if "error" in body:
            code = body["error"].get("code", "")
            message = body["error"].get("message", "")
    except ValueError:
        pass
    raise GraphError(resp.status_code, code, message)


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------


def list_lists() -> list[dict]:
    resp = requests.get(f"{GRAPH_BASE}/lists", headers=_headers(), timeout=30)
    _raise_for_graph_error(resp)
    return resp.json().get("value", [])


def find_list_by_name(lists: list[dict], list_name: str) -> Optional[dict]:
    """Case-insensitive match -- same as the current JS's matchListOverride/
    findOrCreateList lookup."""
    lower = list_name.lower()
    for lst in lists:
        if lst["displayName"].lower() == lower:
            return lst
    return None


def create_list(list_name: str) -> dict:
    resp = requests.post(f"{GRAPH_BASE}/lists", headers=_headers(), json={"displayName": list_name}, timeout=30)
    _raise_for_graph_error(resp)
    return resp.json()


def find_or_create_list(list_name: str, lists: Optional[list[dict]] = None) -> str:
    """Returns the list's id, creating it if no list with this name (case-
    insensitive) exists yet. Pass an already-fetched `lists` (list_lists()
    result) to avoid refetching when resolving several tasks in a row --
    the sync loop's actual usage. A newly created list is appended to
    `lists` in place, so a second task in the same batch that needs the
    same brand-new list name finds it instead of creating a duplicate."""
    if lists is None:
        lists = list_lists()
    existing = find_list_by_name(lists, list_name)
    if existing:
        return existing["id"]
    created = create_list(list_name)
    lists.append(created)
    return created["id"]


# ---------------------------------------------------------------------------
# Date normalization
# ---------------------------------------------------------------------------

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalize_datetime_for_graph(dt: Optional[str]) -> Optional[str]:
    """Ensures YYYY-MM-DDTHH:mm:ss.0000000 -- byte-for-byte port of the
    current JS's normalizeDateTimeForGraph (plan §7). Missing time
    defaults to 09:00:00; an unparseable date returns None (silently
    dropping dueDateTime from the create/update body, same as the JS)."""
    if not dt:
        return None
    clean = re.sub(r"Z$", "", dt)
    clean = re.sub(r"[+-]\d{2}:\d{2}$", "", clean)
    if "T" in clean:
        date_part, time_part = clean.split("T", 1)
    else:
        date_part, time_part = clean, "09:00:00"
    if not _DATE_ONLY_RE.match(date_part):
        return None
    time_parts = time_part.split(":")
    hh = (time_parts[0] or "09").zfill(2)
    mi = (time_parts[1] if len(time_parts) > 1 else "00").zfill(2)
    ss = (time_parts[2] if len(time_parts) > 2 else "00").zfill(2)[:2]
    return f"{date_part}T{hh}:{mi}:{ss}.0000000"


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def _build_task_body(
    title: Optional[str] = None,
    body: Optional[str] = None,
    due_datetime: Optional[str] = None,
    timezone: str = "UTC",
    reminder_datetime: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
    task_body: dict = {}
    if title is not None:
        task_body["title"] = title
    if body is not None:
        task_body["body"] = {"content": body, "contentType": "text"}
    if due_datetime is not None:
        normalized = normalize_datetime_for_graph(due_datetime)
        if normalized:
            task_body["dueDateTime"] = {"dateTime": normalized, "timeZone": timezone}
    if reminder_datetime is not None:
        normalized_reminder = normalize_datetime_for_graph(reminder_datetime)
        if normalized_reminder:
            task_body["isReminderOn"] = True
            task_body["reminderDateTime"] = {"dateTime": normalized_reminder, "timeZone": timezone}
    if status is not None:
        task_body["status"] = status
    return task_body


def create_task(
    list_id: str,
    title: str,
    body: Optional[str] = None,
    due_datetime: Optional[str] = None,
    timezone: str = "UTC",
    reminder_datetime: Optional[str] = None,
) -> dict:
    task_body = _build_task_body(title, body, due_datetime, timezone, reminder_datetime)
    resp = requests.post(
        f"{GRAPH_BASE}/lists/{list_id}/tasks", headers=_headers(timezone), json=task_body, timeout=30
    )
    _raise_for_graph_error(resp)
    return resp.json()


def update_task(
    list_id: str,
    task_id: str,
    title: Optional[str] = None,
    body: Optional[str] = None,
    due_datetime: Optional[str] = None,
    timezone: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
    """PATCH an existing task -- backs the queued edit path (decision 3):
    the pending-action replay hits PATCH /api/tasks/{list_id}/{task_id},
    which calls this. Only fields actually passed get included in the
    Graph request body (None means "don't touch this field", not "clear
    it")."""
    task_body = _build_task_body(title, body, due_datetime, timezone or "UTC", status=status)
    resp = requests.patch(
        f"{GRAPH_BASE}/lists/{list_id}/tasks/{task_id}", headers=_headers(timezone), json=task_body, timeout=30
    )
    _raise_for_graph_error(resp)
    return resp.json()


def list_tasks(list_id: str) -> list[dict]:
    """All tasks in a list, raw Graph shape (status/dueDateTime/body
    untouched -- filtering by status or due date is api.py's job, same
    division of labor as everything else here). Follows @odata.nextLink
    so a list with more than one page of tasks isn't silently truncated."""
    tasks: list[dict] = []
    url = f"{GRAPH_BASE}/lists/{list_id}/tasks"
    while url:
        resp = requests.get(url, headers=_headers(), timeout=30)
        _raise_for_graph_error(resp)
        page = resp.json()
        tasks.extend(page.get("value", []))
        url = page.get("@odata.nextLink")
    return tasks


def delete_task(list_id: str, task_id: str) -> None:
    """Backs the queued delete path (decision 3). Graph returns 204 on
    success; treat 404 (already gone) as success too, since the queued
    action's intent -- 'this task shouldn't exist' -- is already true."""
    resp = requests.delete(f"{GRAPH_BASE}/lists/{list_id}/tasks/{task_id}", headers=_headers(), timeout=30)
    if resp.status_code not in (204, 404):
        _raise_for_graph_error(resp)


# ---------------------------------------------------------------------------
# Checklist items (steps) on a task
# ---------------------------------------------------------------------------


def list_checklist_items(list_id: str, task_id: str) -> list[dict]:
    """Raw Graph shape (isChecked/displayName untouched) -- filtering by
    checked state is api.py's job, same division of labor as list_tasks."""
    resp = requests.get(
        f"{GRAPH_BASE}/lists/{list_id}/tasks/{task_id}/checklistItems", headers=_headers(), timeout=30
    )
    _raise_for_graph_error(resp)
    return resp.json().get("value", [])


def create_checklist_item(list_id: str, task_id: str, display_name: str) -> dict:
    """Backs the queued add-step path (decision 3 -- adding a step changes
    an already-real task, so it's gated like update_task/delete_task)."""
    resp = requests.post(
        f"{GRAPH_BASE}/lists/{list_id}/tasks/{task_id}/checklistItems",
        headers=_headers(), json={"displayName": display_name}, timeout=30,
    )
    _raise_for_graph_error(resp)
    return resp.json()


def update_checklist_item(list_id: str, task_id: str, item_id: str, is_checked: bool) -> dict:
    """Toggles a step's checked state -- called directly, no approval
    queue (treated like sync_draft: low blast-radius, trivially reversed
    by toggling again)."""
    resp = requests.patch(
        f"{GRAPH_BASE}/lists/{list_id}/tasks/{task_id}/checklistItems/{item_id}",
        headers=_headers(), json={"isChecked": is_checked}, timeout=30,
    )
    _raise_for_graph_error(resp)
    return resp.json()


def delete_checklist_item(list_id: str, task_id: str, item_id: str) -> None:
    """Backs the queued delete-step path (decision 3). Graph returns 204 on
    success; treat 404 (already gone) as success too, same reasoning as
    delete_task."""
    resp = requests.delete(
        f"{GRAPH_BASE}/lists/{list_id}/tasks/{task_id}/checklistItems/{item_id}", headers=_headers(), timeout=30
    )
    if resp.status_code not in (204, 404):
        _raise_for_graph_error(resp)


def attach_photo(
    list_id: str,
    task_id: str,
    photo_base64: str,
    filename: str = "todo-list-photo.jpg",
    content_type: str = "image/jpeg",
) -> None:
    """Attaches a photo to an already-created task. Raises GraphError on
    failure -- unlike the current JS (which only logs and moves on), this
    lets the caller (api.py's sync orchestration) decide how to surface it;
    api.py catches this the same way the JS behaves (task still counts as
    synced even if the attachment failed)."""
    resp = requests.post(
        f"{GRAPH_BASE}/lists/{list_id}/tasks/{task_id}/attachments",
        headers=_headers(),
        json={
            "@odata.type": "#microsoft.graph.taskFileAttachment",
            "name": filename,
            "contentType": content_type,
            "contentBytes": photo_base64,
        },
        timeout=30,
    )
    _raise_for_graph_error(resp)


_UPLOAD_SESSION_CHUNK_SIZE = 3 * 1024 * 1024 + 512 * 1024  # ~3.5MB, safely under Graph's <4MB-per-PUT ceiling


def attach_file_via_upload_session(
    list_id: str, task_id: str, file_bytes: bytes, filename: str, content_type: str,
) -> dict:
    """Large/generic-file attachment path via Graph's createUploadSession
    (docs: https://learn.microsoft.com/en-us/graph/todo-attachments), NOT
    attach_photo's <3MB base64 contentBytes POST. Used only by
    /api/uploads/{token} (api.py's token-redemption route) -- the caller
    there has already read the raw bytes server-side, so nothing here is
    ever base64-encoded. Works for any size 0-25MB, Graph's hard ceiling for
    To Do task attachments either way.

    Each PUT still needs the bearer token (Graph's docs list Authorization
    as required for the chunk uploads too, not just session creation) --
    reuse _headers(), just override Content-Type for the octet-stream body.

    Returns {"attachment_id": ..., "location": ...} parsed from the final
    PUT's Location response header."""
    size = len(file_bytes)
    resp = requests.post(
        f"{GRAPH_BASE}/lists/{list_id}/tasks/{task_id}/attachments/createUploadSession",
        headers=_headers(),
        json={"attachmentInfo": {"attachmentType": "file", "name": filename, "size": size}},
        timeout=30,
    )
    _raise_for_graph_error(resp)
    # Docs confirm the PUT target is uploadUrl + "/content", not uploadUrl itself.
    content_url = f"{resp.json()['uploadUrl']}/content"

    start, location = 0, None
    while True:
        end = min(start + _UPLOAD_SESSION_CHUNK_SIZE, size) - 1 if size else 0
        chunk = file_bytes[start:end + 1]
        headers = _headers()
        headers["Content-Type"] = "application/octet-stream"
        headers["Content-Length"] = str(len(chunk))
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        put_resp = requests.put(content_url, headers=headers, data=chunk, timeout=120)
        if put_resp.status_code == 201:
            location = put_resp.headers.get("Location")
            break
        if put_resp.status_code != 200:
            _raise_for_graph_error(put_resp)
        start = end + 1
        if size == 0:
            break  # zero-byte file -- single PUT, no natural loop exit otherwise

    return {"attachment_id": location.rsplit("/", 1)[-1] if location else None, "location": location}
