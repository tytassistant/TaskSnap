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
    list_override_rules: Optional[list[str]] = None
    default_timezone: Optional[str] = None
    default_category: Optional[str] = None


# ---------------------------------------------------------------------------
# list_table (list_table refactor)
# ---------------------------------------------------------------------------


class ListEntryCreate(_StrictModel):
    """One row = one real Microsoft To Do list, tagged with category (who/
    what context) and keywords (what kind of task) for the AI extraction
    pass to route against. list_ms_id is left null until the list is
    first resolved/created against the real MS account."""
    list_name: str
    list_ms_id: Optional[str] = None
    list_alt_names: list[str] = []
    list_category: list[str] = []
    list_keywords: list[str] = []
    list_is_category_default: bool = False


class ListEntryUpdate(_StrictModel):
    """All optional -- PATCH only changes fields present in the request."""
    list_name: Optional[str] = None
    list_ms_id: Optional[str] = None
    list_alt_names: Optional[list[str]] = None
    list_category: Optional[list[str]] = None
    list_keywords: Optional[list[str]] = None
    list_is_category_default: Optional[bool] = None


# ---------------------------------------------------------------------------
# draft_task_table
# ---------------------------------------------------------------------------


class DraftTaskCreate(_StrictModel):
    """Manual 'add a task to this draft' -- the GUI's existing add-task
    button, and MCP's add_draft_task tool.

    category is not a stored column -- when list_name is omitted but
    category is given, api.py resolves it to that category's
    list_is_category_default-flagged list via list_matcher.resolve_list
    before ever calling crud.add_draft_task, the same mechanism the AI
    extraction path uses. Giving neither leaves the task unassigned."""
    kind: TaskKind
    title: str
    body: Optional[str] = None
    due_datetime: Optional[str] = None
    timezone: Optional[str] = None
    reminder_datetime: Optional[str] = None
    list_name: Optional[str] = None
    category: Optional[str] = None
    checked: bool = True


class DraftTaskUpdate(_StrictModel):
    """All optional -- PATCH only changes fields present in the request.
    Deliberately excludes task_id/draft_id/synced/synced_task_id/
    order_index: those are never set by a free-form edit (decision 8).
    category behaves the same as on DraftTaskCreate (resolved, not
    stored) -- only applied when list_name isn't also given."""
    title: Optional[str] = None
    body: Optional[str] = None
    due_datetime: Optional[str] = None
    timezone: Optional[str] = None
    reminder_datetime: Optional[str] = None
    list_name: Optional[str] = None
    list_id: Optional[str] = None
    category: Optional[str] = None
    checked: Optional[bool] = None


# Maps this schema's public field names onto crud.py's task_-prefixed
# column names -- the one place that translation is spelled out. category
# is deliberately absent -- it's resolved to list_name/list_id in api.py
# before persistence, never stored itself.
DRAFT_TASK_FIELD_MAP = {
    "title": "task_title",
    "body": "task_body",
    "due_datetime": "task_due_datetime",
    "timezone": "task_timezone",
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


class DraftNewListCreate(_StrictModel):
    """Body of POST /api/drafts/{draft_id}/new-lists -- registers intent to
    create list_name as a brand-new real Microsoft To Do list (with this
    routing config) the next time this draft is synced. Nothing touches
    Graph here; sync_draft is what actually creates it, gated by the same
    conversational go-ahead rule as the rest of that call (decision 3)."""
    list_name: str
    list_alt_names: list[str] = []
    list_category: list[str] = []
    list_keywords: list[str] = []
    list_is_category_default: bool = False


# ---------------------------------------------------------------------------
# MS To Do lists
# ---------------------------------------------------------------------------


class ListCreate(_StrictModel):
    list_name: str


# ---------------------------------------------------------------------------
# Already-synced MS To Do tasks (decision 3 -- queued edit)
# ---------------------------------------------------------------------------


TaskStatus = Literal["notStarted", "inProgress", "completed", "waitingOnOthers", "deferred"]


class SyncedTaskUpdate(_StrictModel):
    """Body of the queued PATCH /api/tasks/{list_id}/{task_id} action --
    what a pending_action_table row's payload contains, replayed against
    this endpoint after human approval."""
    title: Optional[str] = None
    body: Optional[str] = None
    due_datetime: Optional[str] = None
    timezone: Optional[str] = None
    status: Optional[TaskStatus] = None


# ---------------------------------------------------------------------------
# Checklist items (steps) on an already-synced task
# ---------------------------------------------------------------------------


class ChecklistItemCreate(_StrictModel):
    """Body of the queued POST .../checklist-items action -- adding a step
    changes an already-real task, so it's gated the same as SyncedTaskUpdate."""
    display_name: str


class ChecklistItemUpdate(_StrictModel):
    """Body of PATCH .../checklist-items/{item_id} -- called directly, no
    approval queue (toggling a step's checked state is treated like
    sync_draft: low blast-radius, trivially reversed)."""
    is_checked: bool


class TaskAttachmentCreate(_StrictModel):
    """Body of the queued POST .../attachments action -- attaching a file
    to an already-real task is gated the same as SyncedTaskUpdate."""
    photo_base64: str
    filename: str = "todo-list-photo.jpg"
    content_type: str = "image/jpeg"


# ---------------------------------------------------------------------------
# pending_action_table
# ---------------------------------------------------------------------------


class PendingActionCreate(_StrictModel):
    summary: str
    method: PendingActionMethod
    path: str
    payload: Optional[dict] = None
    source: PendingActionSource = "mcp"
