"""SQLite schema + connection helper for TaskSnap. Mirrors
portfolio-management's database.py conventions (raw sqlite3, no ORM,
`_table` suffix, TEXT sequential ids, explicit create/last-modified UTC
timestamps) -- see programs/TaskSnap/.dev/plan-requirements-20260717-v01.md
§2 for the reference architecture and §5 for this schema's design.

No lock-retry wrapper here (unlike portfolio-management's _RetryConnection):
that exists specifically for portfolio.db's WSL2/DrvFs mount, which is a
different host/filesystem than this VM's vboxsf share. Matches the simpler
`get_connection()` already used by namecard-extract/sake-research/
statement-checker's helpers/auth.py on this same VM.
"""

import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "tasksnap.db"

# Fixed primary-key value for the two single-row tables (ms_token_table,
# settings_table) -- simpler than a CHECK/trigger to enforce "at most one
# row," and the trust boundary here is "single-admin app," not "hostile
# caller."
SINGLETON_ID = "singleton"

# Tables in dependency order: draft_task_table references draft_table, so
# it must come after it. The rest have no FKs between them.
TABLE_DDL = [
    """
    CREATE TABLE IF NOT EXISTS user_record_table (
        user_id TEXT PRIMARY KEY,
        user_name TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
        user_des TEXT,
        user_remark TEXT,
        user_removed INTEGER NOT NULL DEFAULT 0 CHECK (user_removed IN (0, 1)),
        user_create_datetime_UTC TEXT NOT NULL,
        user_last_modified_datetime_UTC TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ms_token_table (
        ms_token_id TEXT PRIMARY KEY,
        ms_token_access_token TEXT,
        ms_token_refresh_token_encrypted TEXT,
        ms_token_expires_datetime_UTC TEXT,
        ms_token_account_id TEXT,
        ms_token_account_name TEXT,
        ms_token_last_modified_datetime_UTC TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings_table (
        settings_id TEXT PRIMARY KEY,
        list_override_rules TEXT NOT NULL,
        default_timezone TEXT NOT NULL,
        default_category TEXT NOT NULL DEFAULT 'Household',
        settings_last_modified_datetime_UTC TEXT NOT NULL
    )
    """,
    # Replaces the old fixed priority/other/event bucket model (decision --
    # see .dev plan doc's list_table refactor). Each row is one real
    # Microsoft To Do list, tagged with category (who/what context, e.g. a
    # person's name) and keywords (what kind of task, within that context).
    # The AI extraction pass resolves both per task/event in one shot
    # (categoryIdentified/listIdentified); list_is_category_default is the
    # only piece Python itself still applies, as a deterministic fallback
    # when the AI can't confidently pick a specific list.
    """
    CREATE TABLE IF NOT EXISTS list_table (
        list_id TEXT PRIMARY KEY,
        list_ms_id TEXT,
        list_name TEXT NOT NULL,
        list_alt_names TEXT NOT NULL DEFAULT '[]',
        list_category TEXT NOT NULL DEFAULT '[]',
        list_keywords TEXT NOT NULL DEFAULT '[]',
        list_is_category_default INTEGER NOT NULL DEFAULT 0 CHECK (list_is_category_default IN (0, 1)),
        list_order_index INTEGER NOT NULL,
        list_create_datetime_UTC TEXT NOT NULL,
        list_last_modified_datetime_UTC TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS draft_table (
        draft_id TEXT PRIMARY KEY,
        draft_source TEXT NOT NULL CHECK (draft_source IN ('photo', 'text', 'photo_text')),
        draft_photo_data TEXT,
        draft_photo_filename TEXT,
        draft_photo_content_type TEXT,
        draft_status TEXT NOT NULL DEFAULT 'open'
            CHECK (draft_status IN ('open', 'synced', 'abandoned')),
        draft_created_via TEXT NOT NULL DEFAULT 'web' CHECK (draft_created_via IN ('web', 'mcp')),
        draft_create_datetime_UTC TEXT NOT NULL,
        draft_last_modified_datetime_UTC TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS draft_task_table (
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
    """,
    # Registers intent to create a brand-new real Microsoft To Do list (with
    # its list_table routing config) at the moment this draft syncs --
    # decision 3's low-blast-radius/conversational-go-ahead gating, same as
    # draft tasks: nothing touches Graph until sync_draft actually runs.
    # Row is deleted once sync_draft has created the real list (see
    # sync_draft_route), so a re-run of sync on the same draft never
    # double-creates it.
    """
    CREATE TABLE IF NOT EXISTS draft_new_list_table (
        new_list_id TEXT PRIMARY KEY,
        draft_id TEXT NOT NULL REFERENCES draft_table(draft_id),
        list_name TEXT NOT NULL,
        list_alt_names TEXT NOT NULL DEFAULT '[]',
        list_category TEXT NOT NULL DEFAULT '[]',
        list_keywords TEXT NOT NULL DEFAULT '[]',
        list_is_category_default INTEGER NOT NULL DEFAULT 0 CHECK (list_is_category_default IN (0, 1)),
        new_list_create_datetime_UTC TEXT NOT NULL
    )
    """,
    # Identical shape to portfolio-management's pending_action_table --
    # decision 3 explicitly reuses that pattern rather than inventing a new
    # one.
    """
    CREATE TABLE IF NOT EXISTS pending_action_table (
        pending_id TEXT PRIMARY KEY,
        pending_summary TEXT NOT NULL,
        pending_method TEXT NOT NULL,
        pending_path TEXT NOT NULL,
        pending_payload TEXT,
        pending_source TEXT NOT NULL DEFAULT 'mcp',
        pending_status TEXT NOT NULL DEFAULT 'pending'
            CHECK (pending_status IN ('pending', 'approved', 'rejected', 'failed')),
        pending_result TEXT,
        pending_create_datetime_UTC TEXT NOT NULL,
        pending_decided_datetime_UTC TEXT
    )
    """,
    # Single-use, short-lived tokens minted for a remote LAN MCP client that
    # cannot/won't base64-encode a file itself
    # (get_task_attachment_upload_url). upload_token IS the primary key --
    # it doubles as the sole credential for POST /api/uploads/{token}, which
    # is deliberately AuthGuard-exempt (main.py), so it's generated via
    # secrets.token_urlsafe, not helpers.generate_id like every other table's
    # id. Four states, not pending_action_table's three: pending -> claimed
    # (reserved atomically the instant redemption starts, before the Graph
    # call) -> completed|failed -- the extra 'claimed' state closes the race
    # window between "check token is valid" and "do the Graph upload."
    """
    CREATE TABLE IF NOT EXISTS attachment_upload_table (
        upload_token TEXT PRIMARY KEY,
        upload_list_id TEXT NOT NULL,
        upload_task_id TEXT NOT NULL,
        upload_filename TEXT NOT NULL,
        upload_content_type TEXT NOT NULL,
        upload_status TEXT NOT NULL DEFAULT 'pending'
            CHECK (upload_status IN ('pending', 'claimed', 'completed', 'failed')),
        upload_result TEXT,
        upload_create_datetime_UTC TEXT NOT NULL,
        upload_expires_datetime_UTC TEXT NOT NULL,
        upload_completed_datetime_UTC TEXT
    )
    """,
    # Mirrors attachment_upload_table's shape/states, for the same reason
    # (get_photo_extraction_upload_url) -- minus list/task id and filename/
    # content-type (no existing task to attach to; extraction *creates* the
    # draft), plus upload_text/upload_timezone (declared at mint time,
    # since those are small and transport reliably over MCP's JSON-only
    # tools/call, unlike the photo itself). The draft is only created once
    # Poe succeeds, inside the redemption route -- not at mint time --
    # avoiding a schema change to draft_table's own draft_status CHECK
    # constraint just to add a "pending extraction" state.
    """
    CREATE TABLE IF NOT EXISTS photo_extraction_upload_table (
        upload_token TEXT PRIMARY KEY,
        upload_text TEXT,
        upload_timezone TEXT,
        upload_filename TEXT,
        upload_status TEXT NOT NULL DEFAULT 'pending'
            CHECK (upload_status IN ('pending', 'claimed', 'completed', 'failed')),
        upload_result TEXT,
        upload_create_datetime_UTC TEXT NOT NULL,
        upload_expires_datetime_UTC TEXT NOT NULL,
        upload_completed_datetime_UTC TEXT
    )
    """,
]

ALL_TABLES = [
    "user_record_table",
    "ms_token_table",
    "settings_table",
    "list_table",
    "draft_table",
    "draft_task_table",
    "draft_new_list_table",
    "pending_action_table",
    "attachment_upload_table",
    "photo_extraction_upload_table",
]

# draft_task/draft_new_list lookups are always "all rows for this draft" --
# the one lookup pattern worth indexing given the small expected row counts.
INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_draft_task_draft_id ON draft_task_table(draft_id)",
    "CREATE INDEX IF NOT EXISTS idx_draft_new_list_draft_id ON draft_new_list_table(draft_id)",
]

# Columns added to an already-existing table after its initial release --
# "CREATE TABLE IF NOT EXISTS" only creates a table the very first time and
# never alters one that already exists, so a database created before one of
# these columns existed needs it patched in explicitly. Safe to run on every
# startup: each entry is a no-op once its column is already present.
COLUMN_MIGRATIONS = [
    ("draft_table", "draft_photo_filename", "TEXT"),
    ("draft_table", "draft_photo_content_type", "TEXT"),
    ("photo_extraction_upload_table", "upload_filename", "TEXT"),
]


def _run_column_migrations(conn: sqlite3.Connection) -> None:
    for table, column, col_type in COLUMN_MIGRATIONS:
        existing_columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def get_connection() -> sqlite3.Connection:
    # check_same_thread=False: api.py's extract_route is `async def` (needs
    # `await image.read()`), and FastAPI resolves this module's sync get_db()
    # dependency via run_in_threadpool -- a different thread than the one the
    # async endpoint body runs on. sqlite3 connections are thread-affine by
    # default, which raised a real ProgrammingError in testing before this
    # was added. Safe here since each request gets its own connection,
    # opened and closed within that single request -- never shared
    # concurrently across threads. Same reasoning as portfolio-management's
    # get_connection(), which sets this for the same underlying reason.
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = get_connection()
    try:
        for ddl in TABLE_DDL:
            conn.execute(ddl)
        _run_column_migrations(conn)
        for ddl in INDEX_DDL:
            conn.execute(ddl)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
