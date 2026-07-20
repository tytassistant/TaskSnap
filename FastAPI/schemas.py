"""Pydantic request models for api.py. Field names here are the public API
contract (e.g. `due_datetime`, `list_name`) -- api.py maps them onto
crud.py's `task_`-prefixed column names, so the DB schema can evolve
without changing what a client sends, and vice versa.

No response models: like portfolio-management's api.py, routes return the
plain dicts crud.py already produces (crud.py is the single source of
truth for shape), rather than re-declaring the same fields a second time
here purely for outbound validation.
"""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

TaskKind = Literal["task", "event"]
DraftSource = Literal["photo", "text", "photo_text"]
PendingActionMethod = Literal["GET", "POST", "PATCH", "DELETE"]
PendingActionSource = Literal["mcp", "web"]


class _StrictModel(BaseModel):
    """extra='forbid' -- pydantic ignores unrecognized JSON fields by
    default, which would turn a typo'd field name (e.g. a PATCH body with
    'titel' instead of 'title') into a silent no-op instead of a clear 422.
    Every request model here inherits this."""
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# settings_table
# ---------------------------------------------------------------------------


class SettingsUpdate(_StrictModel):
    """All optional -- PATCH /api/settings only changes fields that are
    actually present in the request body (model_dump(exclude_unset=True)),
    same partial-update convention as portfolio-management."""
    priority_keywords: Optional[list[str]] = None
    list_override_rules: Optional[list[str]] = None
    default_list_name_priority: Optional[str] = None
    default_list_name_other: Optional[str] = None
    default_list_name_event: Optional[str] = None
    default_timezone: Optional[str] = None
    lite_mode_list_names: Optional[dict[str, str]] = None


# ---------------------------------------------------------------------------
# draft_task_table
# ---------------------------------------------------------------------------


class DraftTaskCreate(_StrictModel):
    """Manual 'add a task to this draft' -- the GUI's existing add-task
    button, and MCP's add_draft_task tool."""
    kind: TaskKind
    title: str
    body: Optional[str] = None
    due_datetime: Optional[str] = None
    timezone: Optional[str] = None
    priority: bool = False
    reminder_datetime: Optional[str] = None
    list_name: Optional[str] = None
    checked: bool = True


class DraftTaskUpdate(_StrictModel):
    """All optional -- PATCH only changes fields present in the request.
    Deliberately excludes task_id/draft_id/synced/synced_task_id/
    order_index: those are never set by a free-form edit (decision 8)."""
    title: Optional[str] = None
    body: Optional[str] = None
    due_datetime: Optional[str] = None
    timezone: Optional[str] = None
    priority: Optional[bool] = None
    reminder_datetime: Optional[str] = None
    list_name: Optional[str] = None
    list_id: Optional[str] = None
    checked: Optional[bool] = None


# Maps this schema's public field names onto crud.py's task_-prefixed
# column names -- the one place that translation is spelled out.
DRAFT_TASK_FIELD_MAP = {
    "title": "task_title",
    "body": "task_body",
    "due_datetime": "task_due_datetime",
    "timezone": "task_timezone",
    "priority": "task_priority",
    "reminder_datetime": "task_reminder_datetime",
    "list_name": "task_list_name",
    "list_id": "task_list_id",
    "checked": "task_checked",
}


# ---------------------------------------------------------------------------
# draft_table
# ---------------------------------------------------------------------------


class DraftSyncRequest(_StrictModel):
    """Optional per-task list-name overrides applied just before sync (the
    list-routing modal's job) -- keyed by task_id. Any task not present here
    syncs with whatever task_list_name is already stored on it."""
    list_assignments: Optional[dict[str, str]] = None


# ---------------------------------------------------------------------------
# MS To Do lists
# ---------------------------------------------------------------------------


class ListCreate(_StrictModel):
    list_name: str


# ---------------------------------------------------------------------------
# Already-synced MS To Do tasks (decision 3 -- queued edit)
# ---------------------------------------------------------------------------


class SyncedTaskUpdate(_StrictModel):
    """Body of the queued PATCH /api/tasks/{list_id}/{task_id} action --
    what a pending_action_table row's payload contains, replayed against
    this endpoint after human approval."""
    title: Optional[str] = None
    body: Optional[str] = None
    due_datetime: Optional[str] = None
    timezone: Optional[str] = None


# ---------------------------------------------------------------------------
# pending_action_table
# ---------------------------------------------------------------------------


class PendingActionCreate(_StrictModel):
    summary: str
    method: PendingActionMethod
    path: str
    payload: Optional[dict] = None
    source: PendingActionSource = "mcp"
