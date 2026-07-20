"""MS OAuth (decision 1): authorization-code flow against a *confidential*
client -- the reconfigured Azure AD app has a client secret and platform
'Web' (not the SPA/public-client registration the current tasksnap
index.html uses via MSAL.js). Raw HTTP calls via `requests`, same shape as
namecard-extract's Google OAuth flow (not the `msal` library's
PublicClientApplication pattern namecard uses for MS -- that pattern is
for a *public* client with no secret; a plain authorization-code +
refresh-token grant is standard OAuth2 regardless of provider, and this
way crud.py's ms_token_table stores discrete fields (access_token,
refresh_token, expires_at, account info) instead of an opaque MSAL cache
blob, matching the plan's §5 data model as originally written).
"""

import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

import crud
import database

AUTHORITY = "https://login.microsoftonline.com/common"
AUTHORIZE_URL = f"{AUTHORITY}/oauth2/v2.0/authorize"
TOKEN_URL = f"{AUTHORITY}/oauth2/v2.0/token"
GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me"

# offline_access must be requested explicitly to get a refresh_token back --
# MSAL.js's PublicClientApplication (the current client-side flow) adds
# this reserved scope automatically; a raw server-side authorization-code
# flow has to ask for it itself.
SCOPES = ["openid", "profile", "offline_access", "Tasks.ReadWrite", "User.Read"]

# Matches the resolved Traefik subdomain (plan §4); override for local/dev.
BASE_URL = os.environ.get("TASKSNAP_BASE_URL", "https://tasksnap.tytserver.duckdns.org").rstrip("/")
REDIRECT_URI = f"{BASE_URL}/auth/callback"

# Refresh this many seconds before actual expiry, so a Graph call never
# races an access token expiring mid-request (same 5-minute buffer as
# namecard-extract's get_valid_google_token).
_EXPIRY_BUFFER_SECONDS = 300

# In-memory CSRF-state store for the redirect round trip. Short-lived and
# single-process -- same assumption namecard-extract's own _ms_auth_flows
# dict already makes (this app runs one uvicorn --reload worker). Pruned
# on each use so an abandoned flow (started but never completed) doesn't
# accumulate forever.
_pending_states: dict[str, float] = {}
_STATE_TTL_SECONDS = 600


class MsAuthError(Exception):
    """Raised for any failure in the OAuth exchange or the Graph /me
    lookup -- main.py's /auth/callback route catches this and redirects
    to /settings with the error message rather than a raw 500."""


def _client_id() -> str:
    client_id = os.environ.get("MS_CLIENT_ID")
    if not client_id:
        raise MsAuthError("MS_CLIENT_ID is not set (systemd EnvironmentFile or environment)")
    return client_id


def _client_secret() -> str:
    client_secret = os.environ.get("MS_CLIENT_SECRET")
    if not client_secret:
        raise MsAuthError(
            "MS_CLIENT_SECRET is not set -- required since decision 1 reconfigures the Azure AD "
            "app as a confidential client"
        )
    return client_secret


def _prune_expired_states() -> None:
    now = time.time()
    expired = [s for s, created in _pending_states.items() if now - created > _STATE_TTL_SECONDS]
    for s in expired:
        _pending_states.pop(s, None)


def build_authorize_url() -> str:
    """Starts the authorization-code flow -- generates and remembers a
    CSRF state token, returns the URL to redirect the browser to."""
    _prune_expired_states()
    state = os.urandom(24).hex()
    _pending_states[state] = time.time()
    params = {
        "client_id": _client_id(),
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "response_mode": "query",
        "scope": " ".join(SCOPES),
        "state": state,
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


def _utc_expiry(expires_in_seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)).isoformat(timespec="seconds")


def _still_valid(expires_at_iso: Optional[str]) -> bool:
    if not expires_at_iso:
        return False
    expires_at = datetime.fromisoformat(expires_at_iso)
    return expires_at > datetime.now(timezone.utc) + timedelta(seconds=_EXPIRY_BUFFER_SECONDS)


def _raise_for_token_error(resp: requests.Response) -> None:
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error_description", resp.text)
        except ValueError:
            detail = resp.text
        raise MsAuthError(f"Microsoft token endpoint error ({resp.status_code}): {detail}")


def _fetch_account_info(access_token: str) -> dict:
    resp = requests.get(GRAPH_ME_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    if resp.status_code >= 400:
        raise MsAuthError(f"Microsoft Graph /me lookup failed ({resp.status_code}): {resp.text}")
    return resp.json()


def handle_callback(code: str, state: str) -> None:
    """Exchanges the authorization code for tokens, fetches the account's
    id/display name from Graph, and persists everything via
    crud.save_ms_token. Raises MsAuthError on any failure (bad/reused/
    expired state, token exchange rejected, missing refresh_token, Graph
    lookup failed) -- every failure path leaves no half-written token."""
    _prune_expired_states()
    if state not in _pending_states:
        raise MsAuthError("invalid or expired state (possible CSRF, or a stale/replayed callback)")
    _pending_states.pop(state, None)

    resp = requests.post(TOKEN_URL, data={
        "client_id": _client_id(),
        "client_secret": _client_secret(),
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
        "scope": " ".join(SCOPES),
    }, timeout=30)
    _raise_for_token_error(resp)
    result = resp.json()

    access_token = result["access_token"]
    refresh_token = result.get("refresh_token")
    if not refresh_token:
        raise MsAuthError(
            "Microsoft did not return a refresh_token -- check that offline_access is consented "
            "and the Azure AD app's platform is 'Web' (not 'Single-page application')"
        )

    account = _fetch_account_info(access_token)

    conn = database.get_connection()
    try:
        crud.save_ms_token(
            conn,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_datetime_utc=_utc_expiry(result.get("expires_in", 3600)),
            account_id=account.get("id"),
            account_name=account.get("userPrincipalName") or account.get("displayName"),
        )
    finally:
        conn.close()


def _refresh(conn, token: dict) -> str:
    resp = requests.post(TOKEN_URL, data={
        "client_id": _client_id(),
        "client_secret": _client_secret(),
        "refresh_token": token["ms_token_refresh_token"],
        "grant_type": "refresh_token",
        "scope": " ".join(SCOPES),
    }, timeout=30)
    _raise_for_token_error(resp)
    result = resp.json()
    access_token = result["access_token"]
    # Microsoft may rotate the refresh token on use -- always persist
    # whatever comes back, falling back to the existing one if absent.
    refresh_token = result.get("refresh_token", token["ms_token_refresh_token"])
    crud.save_ms_token(
        conn,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_datetime_utc=_utc_expiry(result.get("expires_in", 3600)),
        account_id=token["ms_token_account_id"],
        account_name=token["ms_token_account_name"],
    )
    return access_token


def get_valid_access_token() -> str:
    """Returns a currently-valid access token, transparently refreshing via
    the stored refresh_token if the cached one is expired/near expiry.
    This is the one place token lifecycle is handled -- graph_client.py
    (once built) calls this before every Graph request and never sees an
    expired token itself. Raises MsAuthError if no account is linked yet
    or the refresh itself fails (e.g. the refresh_token was revoked)."""
    conn = database.get_connection()
    try:
        token = crud.get_ms_token(conn)
        if token is None:
            raise MsAuthError("no Microsoft account linked -- visit /auth/login first")
        if _still_valid(token["ms_token_expires_datetime_UTC"]):
            return token["ms_token_access_token"]
        return _refresh(conn, token)
    finally:
        conn.close()
