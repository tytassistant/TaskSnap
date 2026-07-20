"""JSON API for TaskSnap's web GUI and (via HTTP, over loopback/LAN) the
standalone MCP server. Thin wrappers over crud.py -- no business logic
lives here beyond request/response field-name translation
(schemas.DRAFT_TASK_FIELD_MAP), the draft-sync orchestration (calls into
graph_client.py), the extraction orchestration (calls into poe_client.py),
and, for pending actions, the approve/replay orchestration (mirrors
portfolio-management's api.py).
"""

import base64
import os
import sqlite3
import threading
from typing import Optional

import requests
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

import crud
import database
import graph_client
import helpers
import poe_client
import schemas

router = APIRouter(prefix="/api")

# Where approval replays calls. One uvicorn process on one port (§5:
# tasksnap.service on 8004); override only if that port ever changes.
SELF_BASE = os.environ.get("TASKSNAP_SELF_URL", "http://127.0.0.1:8004").rstrip("/")


def get_db():
    conn = database.get_connection()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@router.get("/config", tags=["Config"])
def get_config(conn: sqlite3.Connection = Depends(get_db)):
    """Minimal app config for MCP's get_config tool -- whether an MS
    account is linked (not the token itself) and the settings-backed
    default timezone, so an agent can sanity-check setup before calling
    extract_tasks."""
    settings = crud.get_settings(conn)
    return {
        "ms_linked": crud.get_ms_token(conn) is not None,
        "default_timezone": settings["default_timezone"],
        "self_url": SELF_BASE,
    }


# ---------------------------------------------------------------------------
# settings_table (decision 6)
# ---------------------------------------------------------------------------


@router.get("/settings", tags=["Settings"])
def get_settings_route(conn: sqlite3.Connection = Depends(get_db)):
    return crud.get_settings(conn)


@router.patch("/settings", tags=["Settings"])
def update_settings_route(data: schemas.SettingsUpdate, conn: sqlite3.Connection = Depends(get_db)):
    return crud.update_settings(conn, **data.model_dump(exclude_unset=True))


# ---------------------------------------------------------------------------
# Extraction (decision 8) -- depends on poe_client.py, not yet built
# ---------------------------------------------------------------------------


@router.post("/extract", tags=["Extraction"])
async def extract_route(
    image: Optional[UploadFile] = File(None),
    text: Optional[str] = Form(None),
    timezone: Optional[str] = Form(None),
    conn: sqlite3.Connection = Depends(get_db),
):
    """multipart (image?) + text? + timezone -> Poe extraction (settings-
    backed prompt rules) + server-side shaping (decision 8) -> creates a
    draft, returns it. Falls back to an empty list-override block (same as
    the current JS's fetchListNamesForPrompt) if MS isn't linked yet or the
    lists call fails -- extraction shouldn't be blocked by that."""
    settings = crud.get_settings(conn)
    tz = timezone or settings["default_timezone"]

    try:
        list_names = [lst["displayName"] for lst in graph_client.list_lists()]
    except Exception:
        list_names = []

    image_data_url = None
    photo_data_b64 = None
    if image is not None:
        raw = await image.read()
        photo_data_b64 = base64.b64encode(raw).decode()
        image_data_url = poe_client.to_data_url(raw, image.content_type or "image/jpeg")

    result = poe_client.extract(
        image_data_url, text, tz, list_names,
        settings["priority_keywords"], settings["list_override_rules"],
    )

    if image is not None and (text or "").strip():
        source = "photo_text"
    elif image is not None:
        source = "photo"
    else:
        source = "text"

    draft_id = crud.create_draft(conn, source=source, photo_data=photo_data_b64, created_via="web")
    for t in result["tasks"]:
        crud.add_draft_task(
            conn, draft_id,
            kind=t["kind"], title=t["title"], body=t["body"] or None,
            due_datetime=t["due_datetime"], timezone=tz, priority=t["priority"],
            reminder_datetime=t["reminder_datetime"], list_name=t["list_name"],
            checked=t["checked"], has_specific_due_date=t["has_specific_due_date"],
        )
    draft = crud.get_draft(conn, draft_id)
    # photo_date is an AI-detection artifact (like has_specific_due_date) --
    # not persisted on the draft row itself, just surfaced once here for the
    # GUI's read-only "tasks default to next business day after X" display.
    draft["photo_date"] = result["photo_date"]
    return draft


# ---------------------------------------------------------------------------
# draft_table / draft_task_table (decision 8)
# ---------------------------------------------------------------------------


def _get_draft_or_404(conn: sqlite3.Connection, draft_id: str) -> dict:
    draft = crud.get_draft(conn, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail=f"draft '{draft_id}' not found")
    return draft


@router.get("/drafts/{draft_id}", tags=["Drafts"])
def get_draft_route(draft_id: str, conn: sqlite3.Connection = Depends(get_db)):
    return _get_draft_or_404(conn, draft_id)


@router.post("/drafts/{draft_id}/tasks", tags=["Drafts"], status_code=201)
def add_draft_task_route(draft_id: str, data: schemas.DraftTaskCreate, conn: sqlite3.Connection = Depends(get_db)):
    _get_draft_or_404(conn, draft_id)
    crud.add_draft_task(
        conn, draft_id,
        kind=data.kind, title=data.title, body=data.body, due_datetime=data.due_datetime,
        timezone=data.timezone, priority=data.priority, reminder_datetime=data.reminder_datetime,
        list_name=data.list_name, checked=data.checked,
    )
    return crud.get_draft(conn, draft_id)


@router.patch("/drafts/{draft_id}/tasks/{task_id}", tags=["Drafts"])
def update_draft_task_route(
    draft_id: str, task_id: str, data: schemas.DraftTaskUpdate, conn: sqlite3.Connection = Depends(get_db)
):
    fields = {
        schemas.DRAFT_TASK_FIELD_MAP[key]: value
        for key, value in data.model_dump(exclude_unset=True).items()
    }
    return crud.update_draft_task(conn, draft_id, task_id, **fields)


@router.delete("/drafts/{draft_id}/tasks/{task_id}", tags=["Drafts"])
def delete_draft_task_route(draft_id: str, task_id: str, conn: sqlite3.Connection = Depends(get_db)):
    return crud.delete_draft_task(conn, draft_id, task_id)


@router.post("/drafts/{draft_id}/sync", tags=["Drafts"])
def sync_draft_route(draft_id: str, data: schemas.DraftSyncRequest, conn: sqlite3.Connection = Depends(get_db)):
    """For each checked+unsynced task: resolve/create its MS To Do list,
    create the task (+ photo attachment) in MS Graph, mark it synced.
    Per-task failures are reported individually (results[]) rather than
    aborting the whole call -- except an auth error, which aborts the rest
    of the batch since every remaining task would fail the same way (same
    behavior as the current JS's executeSyncTasks, plan §7)."""
    draft = _get_draft_or_404(conn, draft_id)
    overrides = data.list_assignments or {}
    lists_cache: Optional[list] = None
    results = []
    for task in draft["tasks"]:
        if not task["task_checked"] or task["task_synced"]:
            continue
        list_name = overrides.get(task["task_id"]) or task["task_list_name"]
        if not list_name:
            results.append({"task_id": task["task_id"], "status": "failed", "detail": "no list assigned"})
            continue
        try:
            if lists_cache is None:
                lists_cache = graph_client.list_lists()
            list_id = graph_client.find_or_create_list(list_name, lists=lists_cache)
            created = graph_client.create_task(
                list_id, title=task["task_title"], body=task["task_body"],
                due_datetime=task["task_due_datetime"], timezone=task["task_timezone"] or "UTC",
                reminder_datetime=task["task_reminder_datetime"],
            )
        except graph_client.GraphError as exc:
            results.append({"task_id": task["task_id"], "status": "failed", "detail": str(exc)})
            if exc.is_auth_error:
                break
            continue
        if draft["draft_photo_data"]:
            try:
                graph_client.attach_photo(list_id, created["id"], draft["draft_photo_data"])
            except graph_client.GraphError:
                pass  # best-effort -- same as the current JS, task still counts as synced
        crud.mark_draft_task_synced(conn, draft_id, task["task_id"], created["id"], list_id)
        results.append({"task_id": task["task_id"], "status": "synced", "synced_task_id": created["id"]})

    updated_draft = crud.get_draft(conn, draft_id)
    if all(t["task_synced"] or not t["task_checked"] for t in updated_draft["tasks"]):
        crud.set_draft_status(conn, draft_id, "synced")
        updated_draft = crud.get_draft(conn, draft_id)
    return {"draft": updated_draft, "results": results}


# ---------------------------------------------------------------------------
# MS To Do lists
# ---------------------------------------------------------------------------


@router.get("/lists", tags=["Lists"])
def list_lists_route():
    return graph_client.list_lists()


@router.post("/lists", tags=["Lists"], status_code=201)
def create_list_route(data: schemas.ListCreate):
    return graph_client.create_list(data.list_name)


# ---------------------------------------------------------------------------
# Already-synced MS To Do tasks -- edit/delete queue for approval (decision
# 3). Reached only by the pending-action approval replay (see
# _execute_pending_action below), not called directly by the GUI or MCP.
# ---------------------------------------------------------------------------


@router.patch("/tasks/{list_id}/{task_id}", tags=["Tasks"])
def update_synced_task_route(list_id: str, task_id: str, data: schemas.SyncedTaskUpdate):
    return graph_client.update_task(list_id, task_id, **data.model_dump(exclude_unset=True))


@router.delete("/tasks/{list_id}/{task_id}", tags=["Tasks"])
def delete_synced_task_route(list_id: str, task_id: str):
    graph_client.delete_task(list_id, task_id)
    return {"deleted": True}


# ---------------------------------------------------------------------------
# pending_action_table (decision 3) -- fully implemented, self-contained
# ---------------------------------------------------------------------------


@router.post("/pending-actions", tags=["Pending Actions"], status_code=201)
def create_pending_action_route(data: schemas.PendingActionCreate, conn: sqlite3.Connection = Depends(get_db)):
    return crud.create_pending_action(conn, data.summary, data.method, data.path, data.payload, data.source)


@router.get("/pending-actions", tags=["Pending Actions"])
def list_pending_actions_route(status: Optional[str] = Query(None), conn: sqlite3.Connection = Depends(get_db)):
    return crud.list_pending_actions(conn, status)


@router.get("/pending-actions/{pending_id}", tags=["Pending Actions"])
def get_pending_action_route(pending_id: str, conn: sqlite3.Connection = Depends(get_db)):
    action = crud.get_pending_action(conn, pending_id)
    if action is None:
        raise HTTPException(status_code=404, detail=f"pending action '{pending_id}' not found")
    return action


def _execute_pending_action(action: dict) -> None:
    """Runs an approved action in a background thread: replays the stored
    call against this same app and records the outcome. Every path -- API
    success, API rejection, even a network/timeout error on the replay
    itself -- records a result, so a row can never stay result-less
    forever. Uses its own DB connection: the approving request's connection
    is already closed by the time this runs."""
    try:
        resp = requests.request(
            action["pending_method"], f"{SELF_BASE}{action['pending_path']}",
            json=action["pending_payload"], timeout=900,
            headers={"X-API-Key": helpers.get_api_key()},  # replay must pass AuthGuard once it exists
        )
        try:
            body = resp.json()
        except ValueError:
            body = {"detail": resp.text}
        result = {"status_code": resp.status_code, "response": body}
        status = "approved" if resp.status_code < 400 else "failed"
    except Exception as exc:
        result = {"status_code": None, "response": {"detail": f"execution error: {exc}"}}
        status = "failed"
    conn = database.get_connection()
    try:
        crud.record_pending_action_result(conn, action["pending_id"], status, result)
    finally:
        conn.close()


@router.post("/pending-actions/{pending_id}/approve", tags=["Pending Actions"])
def approve_pending_action_route(pending_id: str, conn: sqlite3.Connection = Depends(get_db)):
    """Claims the action (atomically, so it can't run twice) and hands
    execution to a background thread, returning immediately -- a Graph
    call can be slow and must not hold the browser's request open. Until
    execution finishes the row reads status 'approved' with pending_result
    null; poll GET /pending-actions/{id} to see the outcome."""
    action = crud.claim_pending_action(conn, pending_id, "approved")
    threading.Thread(target=_execute_pending_action, args=(action,), daemon=True).start()
    return action


@router.post("/pending-actions/{pending_id}/reject", tags=["Pending Actions"])
def reject_pending_action_route(pending_id: str, conn: sqlite3.Connection = Depends(get_db)):
    return crud.claim_pending_action(conn, pending_id, "rejected")
