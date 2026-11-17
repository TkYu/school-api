"""Who may call what. Default deny: only the routes listed in PUBLIC are open; everything else needs either the
ingest token (Authorization: Bearer, the admin's scripts and crawlers) or a logged-in account. The admin may use
every route; a submitter only those in SUBMITTER (own uploads and settings). A new route that nobody listed is
therefore admin-only, never accidentally open.

The service sits behind nginx on a Unix socket, so the client address comes from nginx (which takes it from
Cloudflare's CF-Connecting-IP, trusting only Cloudflare's ranges).
"""
from __future__ import annotations

import hmac
import re
import threading
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from .config import Settings

# (method, path pattern) open to everyone; the query API for Tomo's skills, the health check and the login page
PUBLIC: list[tuple[str, re.Pattern[str]]] = [
    ("GET", re.compile(r"/")),                     # the public search page
    ("GET", re.compile(r"/v1/health")),
    ("GET", re.compile(r"/v1/search")),
    ("GET", re.compile(r"/v1/documents/[^/]+(/versions(/[^/]+)?)?")),
    ("GET", re.compile(r"/v1/courses(/[^/]+)?")),
    ("GET", re.compile(r"/v1/calendar")),
    # the same reads with a JSON body (Japanese in a Windows terminal's arguments arrives as ???)
    ("POST", re.compile(r"/v1/(search|courses|calendar)")),
    # accounts: log in, sign up, confirm the e-mail, forgot / reset password, choose the language
    ("GET", re.compile(r"/admin/(login|signup|forgot|reset|confirm|lang)")),
    ("POST", re.compile(r"/admin/(login|signup|forgot|reset)")),
]

# what a submitter may open besides the public routes (any method)
SUBMITTER: list[re.Pattern[str]] = [re.compile(p) for p in (
    r"/admin", r"/admin/my(/.*)?", r"/admin/settings", r"/admin/password", r"/admin/logout")]
# all a user who must change the password (after the admin set one) may open
MUST_CHANGE: list[re.Pattern[str]] = [re.compile(p) for p in (r"/admin/password", r"/admin/logout")]

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    if not stored_hash:
        return False
    try:
        return _hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def is_public(method: str, path: str) -> bool:
    m = "GET" if method == "HEAD" else method
    return any(m == meth and pat.fullmatch(path) for meth, pat in PUBLIC)


def bearer_ok(request: Request, token: str) -> bool:
    header = request.headers.get("authorization", "")
    if not token or not header.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(header[7:].strip().encode(), token.encode())


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


class LoginThrottle:
    """Failed logins per client; too many within the window lock the client out (per process)."""

    def __init__(self, max_failures: int, window_s: int):
        self.max, self.window = max_failures, window_s
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, ip: str, now: float) -> deque[float]:
        q = self._fails[ip]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def locked(self, ip: str) -> bool:
        with self._lock:
            return len(self._trim(ip, time.monotonic())) >= self.max

    def failed(self, ip: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._trim(ip, now).append(now)

    def reset(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings):
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next) -> Response:
        path, method = request.url.path, request.method
        request.state.user = None
        if is_public(method, path):
            # the login page shows "logged in as …" and the language of a known user too
            request.state.user = await _session_user(request)
            return await call_next(request)
        if bearer_ok(request, self.settings.ingest_token):
            request.state.actor, request.state.user_id, request.state.role = "ingest-token", None, "admin"
            return await call_next(request)
        user = await _session_user(request)
        if user:
            request.state.user, request.state.actor, request.state.user_id, request.state.role = user, user["email"], user["id"], user["role"]
            if user["must_change_password"] and not any(p.fullmatch(path) for p in MUST_CHANGE):
                return _deny(method, path, "/admin/password", 403, "change your password first")
            if user["role"] == "admin" or any(p.fullmatch(path) for p in SUBMITTER):
                return await call_next(request)
            return _deny(method, path, "/admin/my", 403, "this needs the admin")
        # the web UI sends people to the login page; APIs answer 401
        if method == "GET" and path.startswith("/admin"):
            return RedirectResponse("/admin/login", status_code=303)
        return JSONResponse({"error": "unauthorized", "hint": "admin routes need the ingest token or an admin login"}, status_code=401)


def _deny(method: str, path: str, page: str, status: int, why: str) -> Response:
    if method == "GET" and path.startswith("/admin"):
        return RedirectResponse(page, status_code=303)
    return JSONResponse({"error": "forbidden", "hint": why}, status_code=status)


async def _session_user(request: Request):
    """The account behind the session cookie, checked against the database on every request (a disabled account or a
    changed password ends old sessions at once)."""
    if "session" not in request.scope:
        return None
    uid, version = request.session.get("uid"), request.session.get("sv")
    if not isinstance(uid, int) or not isinstance(version, int):
        return None
    from starlette.concurrency import run_in_threadpool
    from .users import session_user
    user = await run_in_threadpool(session_user, uid, version)
    if not user:
        request.session.clear()
    return user
