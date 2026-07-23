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
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
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
_DEFAULT_LIST_OVERRIDE_RULES = [
    "parenthetical list name, e.g. (List Name)",
    "explicit phrasing: put X in Y list",
    "shorthand: >> Y",
    "blanket phrasing: all/every ... list",
]
_DEFAULT_TIMEZONE = "Asia/Hong_Kong"
_DEFAULT_CATEGORY = "Household"


def _settings_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["list_override_rules"] = json.loads(d["list_override_rules"])
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
                (settings_id, list_override_rules, default_timezone, default_category,
                 settings_last_modified_datetime_UTC)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                database.SINGLETON_ID,
                json.dumps(_DEFAULT_LIST_OVERRIDE_RULES),
                _DEFAULT_TIMEZONE,
                _DEFAULT_CATEGORY,
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
_SETTINGS_JSON_FIELDS = {"list_override_rules"}
_SETTINGS_FIELDS = {"list_override_rules", "default_timezone", "default_category"}


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
# list_table (list_table refactor -- replaces the fixed priority/other/
# event bucket model with category+keyword-tagged Microsoft To Do lists).
# ===========================================================================


def _list_entry_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["list_alt_names"] = json.loads(d["list_alt_names"])
    d["list_category"] = json.loads(d["list_category"])
    d["list_keywords"] = json.loads(d["list_keywords"])
    d["list_is_category_default"] = bool(d["list_is_category_default"])
    return d


def _clear_category_default(conn: sqlite3.Connection, categories: list, exclude_list_id: Optional[str] = None) -> None:
    """Keeps 'the default list for category X' unambiguous: unsets
    list_is_category_default on every OTHER row sharing any of the given
    categories. Called before setting the flag on a row, not after --
    SQLite can't express this as a cross-row UNIQUE constraint here."""
    if not categories:
        return
    rows = conn.execute(
        "SELECT list_id, list_category FROM list_table WHERE list_is_category_default = 1"
    ).fetchall()
    for row in rows:
        if row["list_id"] == exclude_list_id:
            continue
        if set(json.loads(row["list_category"])) & set(categories):
            conn.execute(
                "UPDATE list_table SET list_is_category_default = 0 WHERE list_id = ?",
                (row["list_id"],),
            )


def add_list_entry(
    conn: sqlite3.Connection,
    list_name: str,
    list_ms_id: Optional[str] = None,
    list_alt_names: Optional[list] = None,
    list_category: Optional[list] = None,
    list_keywords: Optional[list] = None,
    list_is_category_default: bool = False,
) -> dict:
    list_id = helpers.generate_id(conn, "list_table", "list_id", "list_")
    next_index = conn.execute(
        "SELECT COALESCE(MAX(list_order_index), -1) + 1 FROM list_table"
    ).fetchone()[0]
    now = helpers.utc_now_iso()
    categories = list_category or []
    if list_is_category_default:
        _clear_category_default(conn, categories)
    conn.execute(
        """
        INSERT INTO list_table
            (list_id, list_ms_id, list_name, list_alt_names, list_category, list_keywords,
             list_is_category_default, list_order_index, list_create_datetime_UTC,
             list_last_modified_datetime_UTC)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            list_id, list_ms_id, list_name, json.dumps(list_alt_names or []),
            json.dumps(categories), json.dumps(list_keywords or []),
            int(list_is_category_default), next_index, now, now,
        ),
    )
    conn.commit()
    return get_list_entry(conn, list_id)


def get_list_entry(conn: sqlite3.Connection, list_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM list_table WHERE list_id = ?", (list_id,)).fetchone()
    return _list_entry_row_to_dict(row) if row else None


def list_all_list_entries(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM list_table ORDER BY list_order_index").fetchall()
    return [_list_entry_row_to_dict(r) for r in rows]


_LIST_ENTRY_EDITABLE_FIELDS = {
    "list_ms_id", "list_name", "list_alt_names", "list_category",
    "list_keywords", "list_is_category_default",
}
_LIST_ENTRY_JSON_FIELDS = {"list_alt_names", "list_category", "list_keywords"}


def update_list_entry(conn: sqlite3.Connection, list_id: str, **fields: Any) -> dict:
    _require_exists(conn, "list_table", "list_id", list_id, "list_id")
    unknown = set(fields) - _LIST_ENTRY_EDITABLE_FIELDS
    if unknown:
        raise helpers.ValidationError(f"Unknown list-entry field(s): {', '.join(sorted(unknown))}")
    if fields:
        if fields.get("list_is_category_default"):
            categories = fields.get("list_category")
            if categories is None:
                categories = get_list_entry(conn, list_id)["list_category"]
            _clear_category_default(conn, categories, exclude_list_id=list_id)
        set_clauses = []
        values = []
        for key, value in fields.items():
            set_clauses.append(f"{key} = ?")
            if key in _LIST_ENTRY_JSON_FIELDS:
                values.append(json.dumps(value))
            elif key == "list_is_category_default":
                values.append(int(value))
            else:
                values.append(value)
        set_clauses.append("list_last_modified_datetime_UTC = ?")
        values.append(helpers.utc_now_iso())
        values.append(list_id)
        conn.execute(
            f"UPDATE list_table SET {', '.join(set_clauses)} WHERE list_id = ?",
            values,
        )
        conn.commit()
    return get_list_entry(conn, list_id)


def delete_list_entry(conn: sqlite3.Connection, list_id: str) -> None:
    _require_exists(conn, "list_table", "list_id", list_id, "list_id")
    conn.execute("DELETE FROM list_table WHERE list_id = ?", (list_id,))
    conn.commit()


# ===========================================================================
# draft_table / draft_task_table (decision 8)
# ===========================================================================


def _task_row_to_dict(row: sqlite3.Row) -> dict:
    return _row_to_dict(row, bool_fields=[
        "task_checked", "task_has_specific_due_date", "task_synced",
    ])


def create_draft(
    conn: sqlite3.Connection,
    source: str,
    photo_data: Optional[str] = None,
    created_via: str = "web",
    photo_filename: Optional[str] = None,
    photo_content_type: Optional[str] = None,
) -> str:
    """photo_filename/photo_content_type: the real, caller-declared name and
    server-detected type of photo_data (when present) -- carried through to
    sync_draft's auto-attach call so the eventual Microsoft To Do attachment
    isn't stuck with the generic "todo-list-photo.jpg"/image-jpeg default
    regardless of what the photo actually is."""
    draft_id = helpers.generate_id(conn, "draft_table", "draft_id", "draft_")
    now = helpers.utc_now_iso()
    conn.execute(
        """
        INSERT INTO draft_table
            (draft_id, draft_source, draft_photo_data, draft_photo_filename, draft_photo_content_type,
             draft_status, draft_created_via, draft_create_datetime_UTC, draft_last_modified_datetime_UTC)
        VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?)
        """,
        (draft_id, source, photo_data, photo_filename, photo_content_type, created_via, now, now),
    )
    conn.commit()
    return draft_id


def get_draft(conn: sqlite3.Connection, draft_id: str) -> Optional[dict]:
    """Returns the draft with its tasks nested under `tasks` (ordered by
    task_order_index) and any pending new-list registrations under
    `new_lists`. This is what both the GUI (page load/resume) and MCP's
    get_draft tool call to re-ground against server truth."""
    row = conn.execute(
        "SELECT * FROM draft_table WHERE draft_id = ?", (draft_id,)
    ).fetchone()
    if row is None:
        return None
    draft = dict(row)
    draft["tasks"] = list_draft_tasks(conn, draft_id)
    draft["new_lists"] = list_draft_new_lists(conn, draft_id)
    return draft


def list_draft_tasks(conn: sqlite3.Connection, draft_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM draft_task_table WHERE draft_id = ? ORDER BY task_order_index",
        (draft_id,),
    ).fetchall()
    return [_task_row_to_dict(r) for r in rows]


def list_drafts(conn: sqlite3.Connection) -> list[dict]:
    """Summary view for the 'My Drafts' page/MCP tool -- task_count only,
    not the full nested tasks (GET /api/drafts/{id} is for that). Most
    recent first, since that's almost always what you want to resume or
    clean up."""
    rows = conn.execute(
        """
        SELECT d.*, COUNT(t.task_id) AS task_count
        FROM draft_table d
        LEFT JOIN draft_task_table t ON t.draft_id = d.draft_id
        GROUP BY d.draft_id
        ORDER BY d.draft_create_datetime_UTC DESC, d.draft_id DESC
        """
        # draft_id as a secondary key: utc_now_iso() truncates to whole
        # seconds, so two drafts created within the same second would
        # otherwise tie -- draft_id's zero-padded sequence number breaks
        # the tie in true creation order.
    ).fetchall()
    return [dict(r) for r in rows]


def delete_draft(conn: sqlite3.Connection, draft_id: str) -> None:
    """Hard delete, same no-audit-requirement reasoning as
    delete_draft_task (decision 4) -- deletes the draft's tasks first
    since draft_task_table's FK has no ON DELETE CASCADE."""
    _require_exists(conn, "draft_table", "draft_id", draft_id, "draft_id")
    conn.execute("DELETE FROM draft_task_table WHERE draft_id = ?", (draft_id,))
    conn.execute("DELETE FROM draft_table WHERE draft_id = ?", (draft_id,))
    conn.commit()


def add_draft_task(
    conn: sqlite3.Connection,
    draft_id: str,
    kind: str,
    title: str,
    body: Optional[str] = None,
    due_datetime: Optional[str] = None,
    timezone: Optional[str] = None,
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
    date-specific auto-sync filter. Manual adds default it to False,
    matching "no AI ever looked at this task"."""
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
             task_timezone, task_reminder_datetime, task_list_name,
             task_list_id, task_checked, task_has_specific_due_date, task_synced,
             task_synced_task_id, task_order_index, task_create_datetime_UTC,
             task_last_modified_datetime_UTC)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 0, NULL, ?, ?, ?)
        """,
        (
            task_id, draft_id, kind, title, body, due_datetime, timezone,
            reminder_datetime, list_name, int(checked),
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
    "task_reminder_datetime", "task_list_name", "task_list_id",
    "task_checked",
}
_DRAFT_TASK_BOOL_FIELDS = {"task_checked"}


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
# draft_new_list_table -- pending "create a brand-new real MS To Do list"
# registrations on a draft (decision 3: gated the same way as the rest of
# sync_draft, not the pending_action_table queue).
# ===========================================================================


def _new_list_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["list_alt_names"] = json.loads(d["list_alt_names"])
    d["list_category"] = json.loads(d["list_category"])
    d["list_keywords"] = json.loads(d["list_keywords"])
    d["list_is_category_default"] = bool(d["list_is_category_default"])
    return d


def add_draft_new_list(
    conn: sqlite3.Connection,
    draft_id: str,
    list_name: str,
    list_alt_names: Optional[list] = None,
    list_category: Optional[list] = None,
    list_keywords: Optional[list] = None,
    list_is_category_default: bool = False,
) -> str:
    """Registers intent to create list_name as a real Microsoft To Do list
    (plus its list_table routing config) the next time this draft is
    synced -- nothing touches Graph here. See sync_draft_route, which
    creates every row of this draft's new_lists before syncing its tasks,
    then deletes each row once the real list exists."""
    _require_exists(conn, "draft_table", "draft_id", draft_id, "draft_id")
    new_list_id = helpers.generate_id(conn, "draft_new_list_table", "new_list_id", "newlist_")
    now = helpers.utc_now_iso()
    conn.execute(
        """
        INSERT INTO draft_new_list_table
            (new_list_id, draft_id, list_name, list_alt_names, list_category,
             list_keywords, list_is_category_default, new_list_create_datetime_UTC)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_list_id, draft_id, list_name, json.dumps(list_alt_names or []),
            json.dumps(list_category or []), json.dumps(list_keywords or []),
            int(list_is_category_default), now,
        ),
    )
    conn.commit()
    return new_list_id


def list_draft_new_lists(conn: sqlite3.Connection, draft_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM draft_new_list_table WHERE draft_id = ? ORDER BY new_list_create_datetime_UTC",
        (draft_id,),
    ).fetchall()
    return [_new_list_row_to_dict(r) for r in rows]


def _require_new_list_in_draft(conn: sqlite3.Connection, draft_id: str, new_list_id: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM draft_new_list_table WHERE new_list_id = ? AND draft_id = ?", (new_list_id, draft_id)
    ).fetchone()
    if row is None:
        raise helpers.ValidationError(f"new_list_id '{new_list_id}' does not exist in draft '{draft_id}'")


def delete_draft_new_list(conn: sqlite3.Connection, draft_id: str, new_list_id: str) -> dict:
    """Removes a pending new-list registration before it's ever synced --
    this never touches MS Graph. Returns the full updated draft, same
    reasoning as delete_draft_task."""
    _require_new_list_in_draft(conn, draft_id, new_list_id)
    conn.execute(
        "DELETE FROM draft_new_list_table WHERE new_list_id = ? AND draft_id = ?", (new_list_id, draft_id)
    )
    conn.commit()
    return get_draft(conn, draft_id)


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


# ---------------------------------------------------------------------------
# attachment_upload_table -- single-use upload tokens for
# get_task_attachment_upload_url (mcp_server.py), redeemed by a plain
# multipart POST to /api/uploads/{token} (api.py), which is deliberately
# AuthGuard-exempt: the token here is the only credential that route checks.
# ---------------------------------------------------------------------------

_ATTACHMENT_UPLOAD_TTL_SECONDS = 900  # 15 minutes


def create_attachment_upload(
    conn: sqlite3.Connection, list_id: str, task_id: str, filename: str, content_type: str,
) -> dict:
    """Mints a single-use token -- secrets.token_urlsafe (256 bits), not
    helpers.generate_id like every other table's id, since this one doubles
    as the redemption route's sole credential rather than an opaque
    reference. Size isn't known yet (no bytes have arrived) -- only
    filename/content_type; the real attachmentInfo.size Graph needs is
    computed from the actual uploaded bytes once redeemed."""
    token = secrets.token_urlsafe(32)
    now = helpers.utc_now_iso()
    expires = (
        datetime.now(timezone.utc) + timedelta(seconds=_ATTACHMENT_UPLOAD_TTL_SECONDS)
    ).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO attachment_upload_table
            (upload_token, upload_list_id, upload_task_id, upload_filename, upload_content_type,
             upload_status, upload_create_datetime_UTC, upload_expires_datetime_UTC)
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (token, list_id, task_id, filename, content_type, now, expires),
    )
    conn.commit()
    return get_attachment_upload(conn, token)


def _attachment_upload_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d["upload_result"] is not None:
        d["upload_result"] = json.loads(d["upload_result"])
    return d


def get_attachment_upload(conn: sqlite3.Connection, token: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM attachment_upload_table WHERE upload_token = ?", (token,)
    ).fetchone()
    return _attachment_upload_row_to_dict(row) if row else None


def claim_attachment_upload(conn: sqlite3.Connection, token: str) -> Optional[dict]:
    """Atomically moves a token pending -> claimed -- the WHERE clause
    guards against two concurrent redeem requests for the same token both
    proceeding. Returns None (not a raise) if the row wasn't 'pending' at
    the moment of the UPDATE -- api.py already does its own not-found/
    expired/already-used checks before calling this, so None here means a
    genuine race lost, which api.py maps to its own 410."""
    cur = conn.execute(
        "UPDATE attachment_upload_table SET upload_status = 'claimed' "
        "WHERE upload_token = ? AND upload_status = 'pending'",
        (token,),
    )
    conn.commit()
    return get_attachment_upload(conn, token) if cur.rowcount else None


def record_attachment_upload_result(conn: sqlite3.Connection, token: str, status: str, result: dict) -> dict:
    """Terminal state after a redeem attempt -- 'completed' (Graph upload
    succeeded) or 'failed' (oversized file, GraphError, or expiry caught
    late). Single-use is enforced by this: once written, the token can
    never be claimed again (claim_attachment_upload's status check)."""
    if status not in ("completed", "failed"):
        raise helpers.ValidationError(f"Invalid attachment-upload result status '{status}'")
    conn.execute(
        "UPDATE attachment_upload_table SET upload_status = ?, upload_result = ?, "
        "upload_completed_datetime_UTC = ? WHERE upload_token = ?",
        (status, json.dumps(result, default=str), helpers.utc_now_iso(), token),
    )
    conn.commit()
    return get_attachment_upload(conn, token)


# ---------------------------------------------------------------------------
# photo_extraction_upload_table -- backs get_photo_extraction_upload_url
# (mcp_server.py) and its /api/extract/uploads/{token} redemption+status
# routes (api.py), which are AuthGuard-exempt for the same reason as
# attachment_upload_table above: the token here is the only credential
# those routes check. The draft itself doesn't exist yet when this is
# minted -- it's only created once Poe succeeds, inside the redemption
# route -- so unlike attachment_upload_table there's no existing
# list_id/task_id to record, just the text/timezone declared at mint time.
# ---------------------------------------------------------------------------

_EXTRACTION_UPLOAD_TTL_SECONDS = 900  # 15 minutes


def create_photo_extraction_upload(
    conn: sqlite3.Connection, text: Optional[str], tz: Optional[str], filename: Optional[str] = None,
) -> dict:
    """Mints a single-use token -- secrets.token_urlsafe (256 bits), same
    reasoning as create_attachment_upload: it doubles as the redemption
    route's sole credential. Parameter named `tz`, not `timezone` --
    `timezone` is this module's own `from datetime import timezone` import
    (used two lines down for `timezone.utc`); a same-named parameter would
    shadow it for the whole function body and break that call.

    filename is caller-declared here (mint time, reliable JSON), same
    reasoning as attachment_upload_table's upload_filename -- the
    multipart upload step's own Content-Disposition filename isn't trusted
    (already proven unreliable for the attachment-upload feature)."""
    token = secrets.token_urlsafe(32)
    now = helpers.utc_now_iso()
    expires = (
        datetime.now(timezone.utc) + timedelta(seconds=_EXTRACTION_UPLOAD_TTL_SECONDS)
    ).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO photo_extraction_upload_table
            (upload_token, upload_text, upload_timezone, upload_filename, upload_status,
             upload_create_datetime_UTC, upload_expires_datetime_UTC)
        VALUES (?, ?, ?, ?, 'pending', ?, ?)
        """,
        (token, text, tz, filename, now, expires),
    )
    conn.commit()
    return get_photo_extraction_upload(conn, token)


def _photo_extraction_upload_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d["upload_result"] is not None:
        d["upload_result"] = json.loads(d["upload_result"])
    return d


def get_photo_extraction_upload(conn: sqlite3.Connection, token: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM photo_extraction_upload_table WHERE upload_token = ?", (token,)
    ).fetchone()
    return _photo_extraction_upload_row_to_dict(row) if row else None


def claim_photo_extraction_upload(conn: sqlite3.Connection, token: str) -> Optional[dict]:
    """Atomically moves a token pending -> claimed, same race-closing
    reasoning as claim_attachment_upload."""
    cur = conn.execute(
        "UPDATE photo_extraction_upload_table SET upload_status = 'claimed' "
        "WHERE upload_token = ? AND upload_status = 'pending'",
        (token,),
    )
    conn.commit()
    return get_photo_extraction_upload(conn, token) if cur.rowcount else None


def record_photo_extraction_upload_result(conn: sqlite3.Connection, token: str, status: str, result: dict) -> dict:
    """Terminal state after a redemption attempt -- 'completed' (draft
    created, result holds its draft_id) or 'failed' (oversized image,
    PoeClientError, or expiry caught late)."""
    if status not in ("completed", "failed"):
        raise helpers.ValidationError(f"Invalid photo-extraction-upload result status '{status}'")
    conn.execute(
        "UPDATE photo_extraction_upload_table SET upload_status = ?, upload_result = ?, "
        "upload_completed_datetime_UTC = ? WHERE upload_token = ?",
        (status, json.dumps(result, default=str), helpers.utc_now_iso(), token),
    )
    conn.commit()
    return get_photo_extraction_upload(conn, token)
