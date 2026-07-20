"""Shared utilities for crud.py -- id generation, timestamps, password
hashing, and the MS refresh-token encryption key. Mirrors
portfolio-management's helpers.py conventions but kept tasksnap-specific
(not imported cross-repo).
"""

import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from cryptography.fernet import Fernet

BASE_DIR = Path(__file__).parent

_PBKDF2_ITERATIONS = 200_000


class ValidationError(ValueError):
    """Raised for data-integrity problems caught in the service layer."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def generate_id(conn: sqlite3.Connection, table: str, id_column: str, prefix: str) -> str:
    """Next sequential id for `table`, formatted as `<prefix><5-digit seq>`.

    Looks at the highest existing suffix rather than a row count, so a
    deleted row's id is never reused (matches portfolio-management's
    helpers.generate_id).
    """
    row = conn.execute(
        f"SELECT {id_column} FROM {table} WHERE {id_column} LIKE ? ORDER BY {id_column} DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()
    next_seq = int(row[0][len(prefix):]) + 1 if row else 1
    return f"{prefix}{next_seq:05d}"


# ---------------------------------------------------------------------------
# Password hashing -- byte-for-byte the same scheme as portfolio-management
# and the other three apps' auth.py (PBKDF2-HMAC-SHA256, 200k iterations,
# per-password random salt, constant-time compare). Reused deliberately for
# consistency, not reinvented.
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"{_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    iterations_str, salt_hex, digest_hex = stored.split("$")
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations_str)
    )
    return hmac.compare_digest(digest.hex(), digest_hex)


# ---------------------------------------------------------------------------
# Secret files -- same persisted-file pattern as portfolio-management's
# _ensure_secret_file (session cookie key, API key, etc.): generate once on
# first use, persist with owner-only permissions, reuse thereafter.
# ---------------------------------------------------------------------------

def _ensure_secret_file(path: Path, generator: Callable[[], str] = lambda: secrets.token_urlsafe(32)) -> str:
    if path.exists():
        return path.read_text().strip()
    value = generator()
    path.write_text(value + "\n")
    path.chmod(0o600)
    return value


def get_session_secret() -> str:
    return _ensure_secret_file(BASE_DIR / ".session-secret")


def get_api_key() -> str:
    """Shared key every machine caller (MCP server, the pending-action
    replay, any future cron script) sends as X-API-Key instead of a browser
    session -- same two-shared-secrets model as portfolio-management (§2).
    TASKSNAP_API_KEY overrides the persisted file (.tasksnap-api-key)."""
    return os.environ.get("TASKSNAP_API_KEY") or _ensure_secret_file(BASE_DIR / ".tasksnap-api-key")


# ---------------------------------------------------------------------------
# MS refresh-token encryption at rest (decision 1) -- Fernet key in its own
# secret file. A leaked tasksnap.db alone (e.g. a misconfigured backup)
# doesn't hand over a live MS credential; the db file and this key file
# would both need to leak together.
#
# Note: uses its own generator (Fernet.generate_key(), not
# secrets.token_urlsafe) because Fernet requires the key to be the exact
# base64-with-padding encoding of 32 random bytes -- token_urlsafe strips
# padding and would fail Fernet's own validation.
# ---------------------------------------------------------------------------

def _get_fernet() -> Fernet:
    key = _ensure_secret_file(
        BASE_DIR / ".ms-token-key",
        generator=lambda: Fernet.generate_key().decode(),
    )
    return Fernet(key.encode())


def encrypt_ms_refresh_token(token: str) -> str:
    return _get_fernet().encrypt(token.encode()).decode()


def decrypt_ms_refresh_token(token_encrypted: str) -> str:
    return _get_fernet().decrypt(token_encrypted.encode()).decode()
