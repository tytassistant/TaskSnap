"""FastAPI app for TaskSnap. api.py (the JSON API) is the single source of
data access; the HTML pages here are thin shells -- settings.html is still
a placeholder (decision 6's rule-editing UI, deliberately deferred).
"""

import secrets
import sqlite3
import time
import urllib.parse
from pathlib import Path

# Loaded before the local module imports below on purpose: auth_ms.py and
# api.py each read an env var at MODULE-IMPORT time (auth_ms.BASE_URL,
# api.SELF_BASE), not lazily inside a function -- so .env must already be
# in os.environ before those imports run, not after. python-dotenv never
# overrides a var systemd's EnvironmentFile already set (default
# override=False), so this is safe to keep even once this app is deployed
# as a systemd service and doesn't rely on a .env file at all.
#
# Lives in config/, not FastAPI/ root -- any future non-secret config JSON
# goes here too. Deliberately its own folder rather than the other three
# apps' "helpers/" convention: TaskSnap already has a helpers.py *module*
# (mirroring portfolio-management's), and a helpers/ *directory* alongside
# it would collide.
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / "config" / ".env")

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

import auth_ms
import crud
import database
import graph_client
import helpers
import poe_client
from api import router as api_router

BASE_DIR = Path(__file__).parent

app = FastAPI(title="TaskSnap")

database.init_db()

STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app.include_router(api_router)


# ---------------------------------------------------------------------------
# Login (decision 5). Browsers authenticate with a signed session cookie;
# machine callers (the MCP server, the pending-action approval replay) send
# X-API-Key instead. Until the first active user is created (via
# create_user.py, console/SSH only -- no web route creates users), the
# guard stays open, so enabling auth can never lock anyone out of a fresh
# install. Byte-for-byte the same shape as portfolio-management's
# AuthGuard (§2).
# ---------------------------------------------------------------------------

API_KEY = helpers.get_api_key()

_AUTH_EXEMPT_PATHS = {"/login", "/health"}
# /api/uploads/ (trailing slash deliberate, so this can never accidentally
# also exempt some future unrelated /api/upload-something route): the
# get_task_attachment_upload_url redemption route (api.py) authenticates
# solely via the single-use, short-lived token in its own URL path -- the
# remote LAN MCP client redeeming it has neither an X-API-Key nor a session
# cookie. The token-minting route stays under normal AuthGuard; only
# redemption is exempt.
_AUTH_EXEMPT_PREFIXES = ("/static/", "/api/uploads/")


def _db_check(fn, *args) -> bool:
    conn = database.get_connection()
    try:
        return fn(conn, *args)
    finally:
        conn.close()


class AuthGuard(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in _AUTH_EXEMPT_PATHS or path.startswith(_AUTH_EXEMPT_PREFIXES):
            return await call_next(request)
        supplied_key = request.headers.get("X-API-Key")
        if supplied_key and secrets.compare_digest(supplied_key, API_KEY):
            return await call_next(request)
        user_id = request.session.get("user_id")
        if user_id and await run_in_threadpool(_db_check, crud.session_user_ok, user_id):
            return await call_next(request)
        if not await run_in_threadpool(_db_check, crud.any_active_users):
            return await call_next(request)  # auth not enabled yet -- see note above
        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"detail": "authentication required"})
        return RedirectResponse(url="/login", status_code=303)


# Middleware runs outermost-last-added: SessionMiddleware must be added
# AFTER AuthGuard so it wraps it and request.session exists in dispatch().
app.add_middleware(AuthGuard)
app.add_middleware(
    SessionMiddleware,
    secret_key=helpers.get_session_secret(),
    max_age=30 * 24 * 3600,  # 30 days
    same_site="lax",
)


@app.get("/login")
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = database.get_connection()
    try:
        user = crud.authenticate_user(conn, username, password)
    finally:
        conn.close()
    if user is None:
        time.sleep(0.5)  # blunt but effective brute-force damper
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Invalid username or password"}, status_code=401,
        )
    request.session["user_id"] = user["user_id"]
    return RedirectResponse(url="/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


# ---------------------------------------------------------------------------
# MS OAuth (decision 1) -- authorization-code flow against a confidential
# client (auth_ms.py). Reached from Settings by an already-logged-in user
# (single-user app, decision 5), so these don't need to be AuthGuard-exempt:
# hitting them while logged out correctly redirects to /login first.
# ---------------------------------------------------------------------------


@app.get("/auth/login")
def ms_oauth_login():
    try:
        return RedirectResponse(url=auth_ms.build_authorize_url(), status_code=303)
    except auth_ms.MsAuthError as exc:
        return RedirectResponse(url=f"/settings?ms_error={urllib.parse.quote(str(exc))}", status_code=303)


@app.get("/auth/callback")
def ms_oauth_callback(code: str = Query(None), state: str = Query(None), error: str = Query(None)):
    if error:
        return RedirectResponse(url=f"/settings?ms_error={urllib.parse.quote(error)}", status_code=303)
    if not code or not state:
        return RedirectResponse(url="/settings?ms_error=missing_code_or_state", status_code=303)
    try:
        auth_ms.handle_callback(code, state)
    except auth_ms.MsAuthError as exc:
        return RedirectResponse(url=f"/settings?ms_error={urllib.parse.quote(str(exc))}", status_code=303)
    return RedirectResponse(url="/settings?ms=ok", status_code=303)


@app.get("/auth/logout")
def ms_oauth_logout():
    """Disconnects the linked MS account -- just forgets the stored token
    (crud.clear_ms_token). Re-linking goes through /auth/login."""
    conn = database.get_connection()
    try:
        crud.clear_ms_token(conn)
    finally:
        conn.close()
    return RedirectResponse(url="/settings", status_code=303)


@app.exception_handler(helpers.ValidationError)
def handle_validation_error(request, exc):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(sqlite3.IntegrityError)
def handle_integrity_error(request, exc):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(auth_ms.MsAuthError)
def handle_ms_auth_error(request, exc):
    """Covers routes that call graph_client.py (which calls
    auth_ms.get_valid_access_token() for every request) before any MS
    account has been linked, or after the refresh token was revoked."""
    return JSONResponse(status_code=401, content={"detail": str(exc)})


@app.exception_handler(graph_client.GraphError)
def handle_graph_error(request, exc):
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.exception_handler(poe_client.PoeClientError)
def handle_poe_client_error(request, exc):
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# HTML pages -- thin shells. index.html (the wizard) and settings.html are
# placeholders until ported from the current tasksnap/index.html (plan §7).
# ---------------------------------------------------------------------------


@app.get("/")
def index_page(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/settings")
def settings_page(request: Request):
    return templates.TemplateResponse(request=request, name="settings.html")


@app.get("/drafts")
def drafts_page(request: Request):
    return templates.TemplateResponse(request=request, name="drafts.html")
