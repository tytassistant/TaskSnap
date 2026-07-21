"""One-off migration for the list_table refactor.

Run manually via SSH -- no web route, same convention as create_user.py.
Idempotent: safe to re-run (checks current schema/row state before each
step, so a second run is a no-op).

Steps:
1. Back up tasksnap.db (timestamped copy) before touching anything.
2. Ensure list_table exists (additive -- database.init_db() already does
   this via CREATE TABLE IF NOT EXISTS, called first).
3. Seed list_table from ../.dev/list_table_seed.json's real MS To Do
   lists, if list_table is currently empty.
4. Rebuild settings_table onto the trimmed schema (drop
   priority_keywords/default_list_name_*/lite_mode_list_names, add
   default_category), preserving list_override_rules/default_timezone
   from the old row if it still has the old shape.
5. Rebuild draft_task_table dropping task_priority (preserves all
   existing drafts/tasks otherwise -- checked live before running:
   1 open + 1 synced draft, 1 unsynced task, all preserved by this step).

Usage:
    /home/node/venvs/tasksnap/bin/python migrate_list_table.py
"""

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import database

SEED_PATH = Path(__file__).parent.parent / ".dev" / "list_table_seed.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def backup_db() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = database.DB_PATH.with_name(f"{database.DB_PATH.stem}.bak-{stamp}.db")
    shutil.copy2(database.DB_PATH, backup_path)
    print(f"Backed up {database.DB_PATH} -> {backup_path}")
    return backup_path


def seed_list_table(conn: sqlite3.Connection) -> None:
    existing = conn.execute("SELECT COUNT(*) FROM list_table").fetchone()[0]
    if existing:
        print(f"list_table already has {existing} row(s) -- skipping seed.")
        return
    if not SEED_PATH.exists():
        print(f"No seed file at {SEED_PATH} -- leaving list_table empty.")
        return
    data = json.loads(SEED_PATH.read_text())
    now = _utc_now_iso()
    order_index = 0
    inserted = 0
    for entry in data.get("lists", []):
        category = entry.get("list_category") or []
        if not category:
            print(f"  skipping '{entry['list_name']}' (no category assigned -- excluded)")
            continue
        list_id = f"list_{order_index + 1:05d}"
        conn.execute(
            """
            INSERT INTO list_table
                (list_id, list_ms_id, list_name, list_alt_names, list_category, list_keywords,
                 list_is_category_default, list_order_index, list_create_datetime_UTC,
                 list_last_modified_datetime_UTC)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                list_id, entry.get("list_ms_id"), entry["list_name"],
                json.dumps(entry.get("list_alt_names") or []),
                json.dumps(category),
                json.dumps(entry.get("list_keywords") or []),
                int(bool(entry.get("list_is_category_default"))),
                order_index, now, now,
            ),
        )
        order_index += 1
        inserted += 1
    conn.commit()
    print(f"Seeded {inserted} list_table row(s) from {SEED_PATH}.")


def rebuild_settings_table(conn: sqlite3.Connection) -> None:
    cols = _table_columns(conn, "settings_table")
    if "default_category" in cols and "priority_keywords" not in cols:
        print("settings_table already on the trimmed schema -- skipping.")
        return
    print("Rebuilding settings_table onto the trimmed schema...")
    old_row = conn.execute("SELECT * FROM settings_table WHERE settings_id = ?", (database.SINGLETON_ID,)).fetchone()
    list_override_rules = old_row["list_override_rules"] if old_row and "list_override_rules" in old_row.keys() else json.dumps([])
    default_timezone = old_row["default_timezone"] if old_row and "default_timezone" in old_row.keys() else "Asia/Hong_Kong"
    now = _utc_now_iso()

    conn.execute("""
        CREATE TABLE settings_table_new (
            settings_id TEXT PRIMARY KEY,
            list_override_rules TEXT NOT NULL,
            default_timezone TEXT NOT NULL,
            default_category TEXT NOT NULL DEFAULT 'Household',
            settings_last_modified_datetime_UTC TEXT NOT NULL
        )
    """)
    conn.execute(
        """
        INSERT INTO settings_table_new
            (settings_id, list_override_rules, default_timezone, default_category,
             settings_last_modified_datetime_UTC)
        VALUES (?, ?, ?, 'Household', ?)
        """,
        (database.SINGLETON_ID, list_override_rules, default_timezone, now),
    )
    conn.execute("DROP TABLE settings_table")
    conn.execute("ALTER TABLE settings_table_new RENAME TO settings_table")
    conn.commit()
    print("settings_table rebuilt (default_category='Household').")


def rebuild_draft_task_table(conn: sqlite3.Connection) -> None:
    cols = _table_columns(conn, "draft_task_table")
    if "task_priority" not in cols:
        print("draft_task_table already lacks task_priority -- skipping.")
        return
    task_count = conn.execute("SELECT COUNT(*) FROM draft_task_table").fetchone()[0]
    print(f"Rebuilding draft_task_table dropping task_priority ({task_count} existing row(s) preserved)...")
    conn.execute("""
        CREATE TABLE draft_task_table_new (
            task_id TEXT PRIMARY KEY,
            draft_id TEXT NOT NULL REFERENCES draft_table(draft_id),
            task_kind TEXT NOT NULL CHECK (task_kind IN ('task', 'event')),
            task_title TEXT NOT NULL,
            task_body TEXT,
            task_due_datetime TEXT,
            task_timezone TEXT,
            task_reminder_datetime TEXT,
            task_list_name TEXT,
            task_list_id TEXT,
            task_checked INTEGER NOT NULL DEFAULT 1 CHECK (task_checked IN (0, 1)),
            task_has_specific_due_date INTEGER NOT NULL DEFAULT 0 CHECK (task_has_specific_due_date IN (0, 1)),
            task_synced INTEGER NOT NULL DEFAULT 0 CHECK (task_synced IN (0, 1)),
            task_synced_task_id TEXT,
            task_order_index INTEGER NOT NULL,
            task_create_datetime_UTC TEXT NOT NULL,
            task_last_modified_datetime_UTC TEXT NOT NULL
        )
    """)
    conn.execute("""
        INSERT INTO draft_task_table_new
            (task_id, draft_id, task_kind, task_title, task_body, task_due_datetime,
             task_timezone, task_reminder_datetime, task_list_name, task_list_id,
             task_checked, task_has_specific_due_date, task_synced, task_synced_task_id,
             task_order_index, task_create_datetime_UTC, task_last_modified_datetime_UTC)
        SELECT
            task_id, draft_id, task_kind, task_title, task_body, task_due_datetime,
            task_timezone, task_reminder_datetime, task_list_name, task_list_id,
            task_checked, task_has_specific_due_date, task_synced, task_synced_task_id,
            task_order_index, task_create_datetime_UTC, task_last_modified_datetime_UTC
        FROM draft_task_table
    """)
    conn.execute("DROP TABLE draft_task_table")
    conn.execute("ALTER TABLE draft_task_table_new RENAME TO draft_task_table")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_draft_task_draft_id ON draft_task_table(draft_id)")
    conn.commit()
    print("draft_task_table rebuilt (task_priority dropped).")


def main():
    if not database.DB_PATH.exists():
        print(f"No DB at {database.DB_PATH} -- nothing to migrate (init_db() will create the new schema fresh).")
        return
    backup_db()
    database.init_db()  # additive: creates list_table if it doesn't exist yet
    conn = database.get_connection()
    try:
        seed_list_table(conn)
        rebuild_settings_table(conn)
        rebuild_draft_task_table(conn)
    finally:
        conn.close()
    print("Migration complete. Restart tasksnap.service to pick up the new code.")


if __name__ == "__main__":
    main()
