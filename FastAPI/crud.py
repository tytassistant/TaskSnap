"""Shared data-access layer for TaskSnap -- used by api.py and (once built)
cli.py/mcp_server.py, so id generation, validation, and the draft lifecycle
live here exactly once. Mirrors portfolio-management's crud.py style: raw
sqlite3, no ORM, functions take an open connection as their first arg.

Functions here take plain keyword arguments rather than pydantic schema
objects (unlike portfolio-management's crud.py, which takes `schemas.XCreate`
instances) -- request/response validation belongs in api.py once it's built,
not here; keeping this module schema-agnostic means it has no dependency on
files that don't exist yet.

See programs/TaskSnap/.dev/plan-requirements-20260717-v01.md §3 (decisions)
and §5 (data model) for the design this implements.
"""

import json
import sqlite3
from typing import Any, Optional

import database
import helpers

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _row_to_dict(row: Optional[sqlite3.Row], bool_fields: tuple = ()) -> Optional[dict]:
    if row is None:
        return None
    d = dict(row)
    for f in bool_fields:
        if f in d:
            d[f] = bool(d[f])
    return d


def _require_exists(conn: sqlite3.Connection, table: str, id_column: str, id_value: str, label: str) -> None:
    row = conn.execute(f"SELECT 1 FROM {table} WHERE {id_column} = ?", (id_value,)).fetchone()
    if row is None:
        raise helpers.ValidationError(f"{label} '{id_value}' does not exist")


# ===========================================================================
# user_record_table (login gate -- decision 5)
# ===========================================================================


def create_user(conn: sqlite3.Connection, user_name: str, password: str) -> str:
    """Used only by create_user.py -- no web route or MCP tool exposes this,
    same reasoning as portfolio-management/namecard-extract/sake-research/
    statement-checker: auth material, admin-only, console access only."""
    user_id = helpers.generate_id(conn, "user_record_table", "user_id", "user_")
    now = helpers.utc_now_iso()
    conn.execute(
        """
        INSERT INTO user_record_table
            (user_id, user_name, password_hash, is_active, user_removed,
             user_create_datetime_UTC, user_last_modified_datetime_UTC)
        VALUES (?, ?, ?, 1, 0, ?, ?)
        """,
        (user_id, user_name, helpers.hash_password(password), now, now),
    )
    conn.commit()
    return user_id


def authenticate_user(conn: sqlite3.Connection, user_name: str, password: str) -> Optional[dict]:
    """Never reveals which check failed (unknown user vs. wrong password vs.
    deactivated) -- same contract as the other apps' auth modules."""
    row = conn.execute(
        "SELECT * FROM user_record_table WHERE user_name = ? AND user_removed = 0", (user_name,)
    ).fetchone()
    if row is None:
        return None
    user = dict(row)
    if not user["is_active"] or not helpers.verify_password(password, user["password_hash"]):
        return None
    return _row_to_dict(row, bool_fields=["is_active", "user_removed"])


def any_active_users(conn: sqlite3.Connection) -> bool:
    """Whether login is enforceable at all -- until the first active user is
    created (via create_user.py), AuthGuard stays open so adding this can
    never lock out access to a running deployment."""
    row = conn.execute(
        "SELECT 1 FROM user_record_table WHERE is_active = 1 AND user_removed = 0 LIMIT 1"
    ).fetchone()
    return row is not None


def session_user_ok(conn: sqlite3.Connection, user_id: str) -> bool:
    """Re-checked per request so deactivating/removing a user cuts their
    access immediately, not when their cookie expires."""
    row = conn.execute(
        "SELECT 1 FROM user_record_table WHERE user_id = ? AND is_active = 1 AND user_removed = 0",
        (user_id,),
    ).fetchone()
    return row is not None


# ===========================================================================
# ms_token_table (single row -- decision 1)
# ===========================================================================


def get_ms_token(conn: sqlite3.Connection) -> Optional[dict]:
    """Returns the stored MS OAuth token with refresh_token already
    decrypted, or None if no account has been linked yet."""
    row = conn.execute(
        "SELECT * FROM ms_token_table WHERE ms_token_id = ?", (database.SINGLETON_ID,)
    ).fetchone()
    if row is None:
        return None
    token = dict(row)
    if token["ms_token_refresh_token_encrypted"]:
        token["ms_token_refresh_token"] = helpers.decrypt_ms_refresh_token(
            token.pop("ms_token_refresh_token_encrypted")
        )
    else:
        token["ms_token_refresh_token"] = None
        token.pop("ms_token_refresh_token_encrypted", None)
    return token


def save_ms_token(
    conn: sqlite3.Connection,
    access_token: str,
    refresh_token: str,
    expires_datetime_utc: str,
    account_id: Optional[str] = None,
    account_name: Optional[str] = None,
) -> None:
    """Upserts the single ms_token_table row (create the first time a Google/MS
    account is linked, overwrite on every subsequent refresh)."""
    now = helpers.utc_now_iso()
    refresh_token_encrypted = helpers.encrypt_ms_refresh_token(refresh_token)
    conn.execute(
        """
        INSERT INTO ms_token_table
            (ms_token_id, ms_token_access_token, ms_token_refresh_token_encrypted,
             ms_token_expires_datetime_UTC, ms_token_account_id, ms_token_account_name,
             ms_token_last_modified_datetime_UTC)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (ms_token_id) DO UPDATE SET
            ms_token_access_token = excluded.ms_token_access_token,
            ms_token_refresh_token_encrypted = excluded.ms_token_refresh_token_encrypted,
            ms_token_expires_datetime_UTC = excluded.ms_token_expires_datetime_UTC,
            ms_token_account_id = excluded.ms_token_account_id,
            ms_token_account_name = excluded.ms_token_account_name,
            ms_token_last_modified_datetime_UTC = excluded.ms_token_last_modified_datetime_UTC
        """,
        (
            database.SINGLETON_ID, access_token, refresh_token_encrypted,
            expires_datetime_utc, account_id, account_name, now,
        ),
    )
    conn.commit()


def clear_ms_token(conn: sqlite3.Connection) -> None:
    """Disconnects the linked MS account (e.g. a 'disconnect' button in
    Settings) -- deletes the row rather than nulling fields, so
    get_ms_token() cleanly reports 'not linked'."""
    conn.execute("DELETE FROM ms_token_table WHERE ms_token_id = ?", (database.SINGLETON_ID,))
    conn.commit()


# ===========================================================================
# settings_table (single row -- decision 6)
# ===========================================================================

# Seed values carried over from the current hardcoded JS (see plan §1/§7).
# default_list_name_priority/other/event are left unset (None) -- the
# current app has no equivalent hardcoded default for the *review-screen*
# list-routing modal, only for lite mode.
_DEFAULT_PRIORITY_KEYWORDS = [
    "quiz", "exam", "test", "assessment",
    "測", "小測", "測驗", "考", "考試", "口試",
    "dict", "dictation", "默", "默書",
]
_DEFAULT_LIST_OVERRIDE_RULES = [
    "parenthetical list name, e.g. (List Name)",
    "explicit phrasing: put X in Y list",
    "shorthand: >> Y",
    "blanket phrasing: all/every ... list",
]
_DEFAULT_LITE_MODE_LIST_NAMES = {
    "priority": "Theo Quiz and Dictation",
    "other": "Theo Homework",
    "event": "Household Tasks",
}
_DEFAULT_TIMEZONE = "Asia/Hong_Kong"


def _settings_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["priority_keywords"] = json.loads(d["priority_keywords"])
    d["list_override_rules"] = json.loads(d["list_override_rules"])
    d["lite_mode_list_names"] = json.loads(d["lite_mode_list_names"])
    return d


def get_settings(conn: sqlite3.Connection) -> dict:
    """Auto-seeds the default row on first call so every other function can
    assume settings_table always has exactly one row."""
    row = conn.execute(
        "SELECT * FROM settings_table WHERE settings_id = ?", (database.SINGLETON_ID,)
    ).fetchone()
    if row is None:
        now = helpers.utc_now_iso()
        conn.execute(
            """
            INSERT INTO settings_table
                (settings_id, priority_keywords, list_override_rules,
                 default_list_name_priority, default_list_name_other, default_list_name_event,
                 default_timezone, lite_mode_list_names, settings_last_modified_datetime_UTC)
            VALUES (?, ?, ?, NULL, NULL, NULL, ?, ?, ?)
            """,
            (
                database.SINGLETON_ID,
                json.dumps(_DEFAULT_PRIORITY_KEYWORDS),
                json.dumps(_DEFAULT_LIST_OVERRIDE_RULES),
                _DEFAULT_TIMEZONE,
                json.dumps(_DEFAULT_LITE_MODE_LIST_NAMES),
                now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM settings_table WHERE settings_id = ?", (database.SINGLETON_ID,)
        ).fetchone()
    return _settings_row_to_dict(row)


# Columns that store JSON-encoded values, needing json.dumps() before the
# UPDATE rather than being written as plain TEXT.
_SETTINGS_JSON_FIELDS = {"priority_keywords", "list_override_rules", "lite_mode_list_names"}
_SETTINGS_FIELDS = {
    "priority_keywords", "list_override_rules",
    "default_list_name_priority", "default_list_name_other", "default_list_name_event",
    "default_timezone", "lite_mode_list_names",
}


def update_settings(conn: sqlite3.Connection, **fields: Any) -> dict:
    """Partial update -- pass only the fields being changed. Unknown keys
    raise, so a typo'd field name fails loudly instead of being silently
    ignored."""
    get_settings(conn)  # ensure the row exists before updating it
    unknown = set(fields) - _SETTINGS_FIELDS
    if unknown:
        raise helpers.ValidationError(f"Unknown settings field(s): {', '.join(sorted(unknown))}")
    if not fields:
        return get_settings(conn)
    set_clauses = []
    values = []
    for key, value in fields.items():
        set_clauses.append(f"{key} = ?")
        values.append(json.dumps(value) if key in _SETTINGS_JSON_FIELDS else value)
    set_clauses.append("settings_last_modified_datetime_UTC = ?")
    values.append(helpers.utc_now_iso())
    values.append(database.SINGLETON_ID)
    conn.execute(
        f"UPDATE settings_table SET {', '.join(set_clauses)} WHERE settings_id = ?",
        values,
    )
    conn.commit()
    return get_settings(conn)


# ===========================================================================
# draft_table / draft_task_table (decision 8)
# ===========================================================================


def _task_row_to_dict(row: sqlite3.Row) -> dict:
    return _row_to_dict(row, bool_fields=[
        "task_priority", "task_checked", "task_has_specific_due_date", "task_synced",
    ])


def create_draft(
    conn: sqlite3.Connection,
    source: str,
    photo_data: Optional[str] = None,
    created_via: str = "web",
) -> str:
    draft_id = helpers.generate_id(conn, "draft_table", "draft_id", "draft_")
    now = helpers.utc_now_iso()
    conn.execute(
        """
        INSERT INTO draft_table
            (draft_id, draft_source, draft_photo_data, draft_status, draft_created_via,
             draft_create_datetime_UTC, draft_last_modified_datetime_UTC)
        VALUES (?, ?, ?, 'open', ?, ?, ?)
        """,
        (draft_id, source, photo_data, created_via, now, now),
    )
    conn.commit()
    return draft_id


def get_draft(conn: sqlite3.Connection, draft_id: str) -> Optional[dict]:
    """Returns the draft with its tasks nested under `tasks`, ordered the
    same way they're meant to be displayed (task_order_index). This is what
    both the GUI (page load/resume) and MCP's get_draft tool call to
    re-ground against server truth."""
    row = conn.execute(
        "SELECT * FROM draft_table WHERE draft_id = ?", (draft_id,)
    ).fetchone()
    if row is None:
        return None
    draft = dict(row)
    draft["tasks"] = list_draft_tasks(conn, draft_id)
    return draft


def list_draft_tasks(conn: sqlite3.Connection, draft_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM draft_task_table WHERE draft_id = ? ORDER BY task_order_index",
        (draft_id,),
    ).fetchall()
    return [_task_row_to_dict(r) for r in rows]


def add_draft_task(
    conn: sqlite3.Connection,
    draft_id: str,
    kind: str,
    title: str,
    body: Optional[str] = None,
    due_datetime: Optional[str] = None,
    timezone: Optional[str] = None,
    priority: bool = False,
    reminder_datetime: Optional[str] = None,
    list_name: Optional[str] = None,
    checked: bool = True,
    has_specific_due_date: bool = False,
) -> str:
    """Used both by the server-side extraction shaping step (decision 8 --
    populating a freshly created draft with already-shaped tasks) and by
    the manual 'add task' action (GUI button / MCP add_draft_task tool).

    has_specific_due_date is an AI-detection artifact (poe_client.py's
    shaping step -- plan §7's hasSpecificDueDate), not a user-editable
    field: it's set once at creation and used later by lite mode's
    priority-or-date-specific auto-sync filter. Manual adds default it to
    False, matching "no AI ever looked at this task"."""
    _require_exists(conn, "draft_table", "draft_id", draft_id, "draft_id")
    task_id = helpers.generate_id(conn, "draft_task_table", "task_id", "task_")
    next_index = conn.execute(
        "SELECT COALESCE(MAX(task_order_index), -1) + 1 FROM draft_task_table WHERE draft_id = ?",
        (draft_id,),
    ).fetchone()[0]
    now = helpers.utc_now_iso()
    conn.execute(
        """
        INSERT INTO draft_task_table
            (task_id, draft_id, task_kind, task_title, task_body, task_due_datetime,
             task_timezone, task_priority, task_reminder_datetime, task_list_name,
             task_list_id, task_checked, task_has_specific_due_date, task_synced,
             task_synced_task_id, task_order_index, task_create_datetime_UTC,
             task_last_modified_datetime_UTC)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 0, NULL, ?, ?, ?)
        """,
        (
            task_id, draft_id, kind, title, body, due_datetime, timezone,
            int(priority), reminder_datetime, list_name, int(checked),
            int(has_specific_due_date), next_index, now, now,
        ),
    )
    conn.commit()
    return task_id


# Fields a caller (GUI edit / MCP edit_draft_task) may change on an existing
# draft task. task_id/draft_id/task_synced/task_synced_task_id/
# task_order_index are not here -- those are only ever set by
# add_draft_task/mark_draft_task_synced/internal reordering, never by a
# free-form edit.
_DRAFT_TASK_EDITABLE_FIELDS = {
    "task_title", "task_body", "task_due_datetime", "task_timezone",
    "task_priority", "task_reminder_datetime", "task_list_name", "task_list_id",
    "task_checked",
}
_DRAFT_TASK_BOOL_FIELDS = {"task_priority", "task_checked"}


def update_draft_task(conn: sqlite3.Connection, draft_id: str, task_id: str, **fields: Any) -> dict:
    """Partial update of one task within a draft. Returns the full updated
    draft (not just the one task) -- every draft mutation does, per decision
    8, so the caller (GUI or MCP agent) always re-presents current state
    instead of trusting stale memory."""
    _require_task_in_draft(conn, draft_id, task_id)
    unknown = set(fields) - _DRAFT_TASK_EDITABLE_FIELDS
    if unknown:
        raise helpers.ValidationError(f"Unknown/non-editable task field(s): {', '.join(sorted(unknown))}")
    if fields:
        set_clauses = []
        values = []
        for key, value in fields.items():
            set_clauses.append(f"{key} = ?")
            values.append(int(value) if key in _DRAFT_TASK_BOOL_FIELDS else value)
        set_clauses.append("task_last_modified_datetime_UTC = ?")
        values.append(helpers.utc_now_iso())
        values.extend([task_id, draft_id])
        conn.execute(
            f"UPDATE draft_task_table SET {', '.join(set_clauses)} WHERE task_id = ? AND draft_id = ?",
            values,
        )
        conn.commit()
    return get_draft(conn, draft_id)


def delete_draft_task(conn: sqlite3.Connection, draft_id: str, task_id: str) -> dict:
    """Hard delete, not soft -- drafts are pre-sync working data with no
    audit/history requirement (decision 4), unlike the permanent business
    records portfolio-management soft-deletes. Returns the full updated
    draft, same reasoning as update_draft_task."""
    _require_task_in_draft(conn, draft_id, task_id)
    conn.execute("DELETE FROM draft_task_table WHERE task_id = ? AND draft_id = ?", (task_id, draft_id))
    conn.commit()
    return get_draft(conn, draft_id)


def mark_draft_task_synced(conn: sqlite3.Connection, draft_id: str, task_id: str, synced_task_id: str, list_id: str) -> None:
    """Called by the sync step (POST /api/drafts/{id}/sync) after a task is
    successfully created in MS Graph."""
    _require_task_in_draft(conn, draft_id, task_id)
    conn.execute(
        """
        UPDATE draft_task_table
        SET task_synced = 1, task_synced_task_id = ?, task_list_id = ?, task_last_modified_datetime_UTC = ?
        WHERE task_id = ? AND draft_id = ?
        """,
        (synced_task_id, list_id, helpers.utc_now_iso(), task_id, draft_id),
    )
    conn.commit()


def set_draft_status(conn: sqlite3.Connection, draft_id: str, status: str) -> None:
    if status not in ("open", "synced", "abandoned"):
        raise helpers.ValidationError(f"Invalid draft status '{status}'")
    _require_exists(conn, "draft_table", "draft_id", draft_id, "draft_id")
    conn.execute(
        "UPDATE draft_table SET draft_status = ?, draft_last_modified_datetime_UTC = ? WHERE draft_id = ?",
        (status, helpers.utc_now_iso(), draft_id),
    )
    conn.commit()


def _require_task_in_draft(conn: sqlite3.Connection, draft_id: str, task_id: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM draft_task_table WHERE task_id = ? AND draft_id = ?", (task_id, draft_id)
    ).fetchone()
    if row is None:
        raise helpers.ValidationError(f"task_id '{task_id}' does not exist in draft '{draft_id}'")


# ===========================================================================
# pending_action_table (decision 3 -- edit/delete of already-synced MS tasks)
# ===========================================================================


def create_pending_action(
    conn: sqlite3.Connection,
    summary: str,
    method: str,
    path: str,
    payload: Optional[dict] = None,
    source: str = "mcp",
) -> dict:
    """path must be an /api/ path, and can't target the pending-actions
    queue itself -- otherwise an MCP tool could queue an action whose
    replay queues another action, recursively (same guard as
    portfolio-management's create_pending_action)."""
    if not path.startswith("/api/") or path.startswith("/api/pending-actions"):
        raise helpers.ValidationError(
            "path must be an /api/ path and cannot target the pending-actions queue itself"
        )
    pending_id = helpers.generate_id(conn, "pending_action_table", "pending_id", "pending_")
    now = helpers.utc_now_iso()
    conn.execute(
        """
        INSERT INTO pending_action_table
            (pending_id, pending_summary, pending_method, pending_path, pending_payload,
             pending_source, pending_status, pending_create_datetime_UTC)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (pending_id, summary, method, path, json.dumps(payload) if payload is not None else None, source, now),
    )
    conn.commit()
    return get_pending_action(conn, pending_id)


def _pending_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d["pending_payload"] is not None:
        d["pending_payload"] = json.loads(d["pending_payload"])
    if d["pending_result"] is not None:
        d["pending_result"] = json.loads(d["pending_result"])
    return d


def get_pending_action(conn: sqlite3.Connection, pending_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM pending_action_table WHERE pending_id = ?", (pending_id,)
    ).fetchone()
    return _pending_row_to_dict(row) if row else None


def list_pending_actions(conn: sqlite3.Connection, status: Optional[str] = None) -> list[dict]:
    if status is not None:
        rows = conn.execute(
            "SELECT * FROM pending_action_table WHERE pending_status = ? ORDER BY pending_create_datetime_UTC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM pending_action_table ORDER BY pending_create_datetime_UTC"
        ).fetchall()
    return [_pending_row_to_dict(r) for r in rows]


def claim_pending_action(conn: sqlite3.Connection, pending_id: str, status: str) -> dict:
    """Atomically moves a pending action out of 'pending' (to 'approved' or
    'rejected') -- the WHERE clause guards against a double-click or
    concurrent decision executing the same action twice. Raises if it was
    already decided or doesn't exist."""
    if status not in ("approved", "rejected"):
        raise helpers.ValidationError(f"Invalid claim status '{status}' (must be approved/rejected)")
    _require_exists(conn, "pending_action_table", "pending_id", pending_id, "pending_id")
    cur = conn.execute(
        "UPDATE pending_action_table SET pending_status = ?, pending_decided_datetime_UTC = ? "
        "WHERE pending_id = ? AND pending_status = 'pending'",
        (status, helpers.utc_now_iso(), pending_id),
    )
    if cur.rowcount == 0:
        row = get_pending_action(conn, pending_id)
        raise helpers.ValidationError(
            f"pending action '{pending_id}' was already decided (status: {row['pending_status']})"
        )
    conn.commit()
    return get_pending_action(conn, pending_id)


def record_pending_action_result(conn: sqlite3.Connection, pending_id: str, status: str, result: dict) -> dict:
    """Stores what happened when an approved action was replayed -- the
    response on success, or the error detail with status 'failed' if the
    replayed call itself errored or the target endpoint rejected it. Called
    from the background thread that performs the replay, on its own DB
    connection (the approving request's connection is already closed by
    the time this runs)."""
    if status not in ("approved", "failed"):
        raise helpers.ValidationError(f"Invalid result status '{status}' (must be approved/failed)")
    conn.execute(
        "UPDATE pending_action_table SET pending_status = ?, pending_result = ? WHERE pending_id = ?",
        (status, json.dumps(result, default=str), pending_id),
    )
    conn.commit()
    return get_pending_action(conn, pending_id)
