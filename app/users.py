"""Accounts: sign-up with e-mail confirmation, login, password reset, the admin's user management.

Roles: the first user who confirms the e-mail becomes the admin (there is only one); everyone after is a submitter.
Who may sign up is limited by SIGNUP_ALLOWLIST (domains and / or addresses); with an empty list only the very first
user may. Tokens in mail links are stored as sha256 only, used once, and expire."""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.types.json import Jsonb

from .auth import hash_password, verify_password
from .config import settings
from .db import tx

EMAIL = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,}$")
PASSWORD_MIN, PASSWORD_MAX = 10, 200
TOKEN_TTL = {"confirm": timedelta(hours=48), "reset": timedelta(hours=1)}
LANGS = ("ja", "zh", "en")


class AccountError(ValueError):
    """A message key for the person (see i18n), e.g. 'err.password_short'."""


def normalize_email(email: str) -> str:
    email = (email or "").strip()
    local, _, domain = email.rpartition("@")
    return f"{local}@{domain.lower()}" if local else email


def check_password(password: str) -> None:
    if len(password or "") < PASSWORD_MIN:
        raise AccountError("err.password_short")
    if len(password) > PASSWORD_MAX:
        raise AccountError("err.password_long")


def admin_exists(conn) -> bool:
    return conn.execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone() is not None


def allowed_to_sign_up(conn, email: str) -> bool:
    domains, addresses = settings().allowlist()
    if not domains and not addresses:
        return not admin_exists(conn)        # nobody but the first user
    e = email.lower()
    return e in addresses or e.rpartition("@")[2] in domains


def new_token(conn, user_id: int, purpose: str) -> str:
    """A new link token (older unused ones of the same purpose stop working); made by the worker when it sends."""
    token = secrets.token_urlsafe(32)
    conn.execute("UPDATE email_tokens SET used_at = now() WHERE user_id = %s AND purpose = %s AND used_at IS NULL", (user_id, purpose))
    conn.execute("INSERT INTO email_tokens (user_id, purpose, token_sha256, expires_at) VALUES (%s, %s, %s, %s)",
                 (user_id, purpose, hashlib.sha256(token.encode()).hexdigest(), datetime.now(timezone.utc) + TOKEN_TTL[purpose]))
    return token


def _use_token(conn, token: str, purpose: str) -> dict[str, Any] | None:
    row = conn.execute(
        """UPDATE email_tokens SET used_at = now()
           WHERE token_sha256 = %s AND purpose = %s AND used_at IS NULL AND expires_at > now()
           RETURNING user_id""", (hashlib.sha256((token or "").encode()).hexdigest(), purpose)).fetchone()
    return row


def sign_up(email: str, password: str, nickname: str, lang: str, base_url: str) -> str:
    """'sent' when a confirmation mail was queued; 'exists' when the address already has a confirmed account (the
    page says the same in both cases). Raises AccountError for a refused address."""
    from . import mail
    email = normalize_email(email)
    if not EMAIL.match(email):
        raise AccountError("err.email_invalid")
    check_password(password)
    lang = lang if lang in LANGS else "ja"
    with tx() as conn:
        # one sign-up at a time: the allowlist rule "only the first user" must not let two through at once
        conn.execute("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE")
        if not allowed_to_sign_up(conn, email):
            raise AccountError("err.signup_not_allowed")
        row = conn.execute("SELECT id, confirmed_at FROM users WHERE lower(email) = lower(%s)", (email,)).fetchone()
        if row and row["confirmed_at"]:
            return "exists"
        nick = (nickname or "").strip()[:60] or email.split("@")[0]
        if row:   # signed up before without confirming: new password and nickname, new link
            conn.execute("UPDATE users SET password_hash = %s, nickname = %s, lang = %s WHERE id = %s",
                         (hash_password(password), nick, lang, row["id"]))
            uid = row["id"]
        else:
            uid = conn.execute("INSERT INTO users (email, nickname, password_hash, lang) VALUES (%s, %s, %s, %s) RETURNING id",
                               (email, nick, hash_password(password), lang)).fetchone()["id"]
        mail.queue(conn, "confirm", uid, base_url)
        return "sent"


def confirm(token: str) -> dict[str, Any] | None:
    """Confirms the e-mail. The first confirmed user becomes the admin and takes over everything submitted before
    accounts existed (owner NULL). Returns the user, or None for an unknown / used / expired link."""
    with tx() as conn:
        conn.execute("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE")
        t = _use_token(conn, token, "confirm")
        if not t:
            return None
        role = "submitter" if admin_exists(conn) else "admin"
        user = conn.execute("UPDATE users SET confirmed_at = COALESCE(confirmed_at, now()), role = CASE WHEN confirmed_at IS NULL THEN %s ELSE role END "
                            "WHERE id = %s RETURNING id, email, nickname, role, lang", (role, t["user_id"])).fetchone()
        if user["role"] == "admin":
            conn.execute("UPDATE batches SET owner_id = %s WHERE owner_id IS NULL", (user["id"],))
        conn.execute("INSERT INTO audit_log (actor, user_id, action, target, detail) VALUES (%s, %s, 'user.confirm', %s, %s)",
                     (user["email"], user["id"], str(user["id"]), Jsonb({"role": user["role"]})))
        return user


def authenticate(email: str, password: str) -> tuple[dict[str, Any] | None, str | None]:
    """(user, None) or (None, message key). A wrong address and a wrong password give the same message."""
    with tx() as conn:
        u = conn.execute("SELECT * FROM users WHERE lower(email) = lower(%s)", (normalize_email(email),)).fetchone()
        # verify even without a user, so a missing account takes as long as a wrong password
        ok = verify_password(u["password_hash"] if u else _DUMMY, password or "")
        if not u or not ok:
            return None, "err.login_wrong"
        if not u["confirmed_at"]:
            return None, "err.login_unconfirmed"
        if u["disabled_at"]:
            return None, "err.login_disabled"
        conn.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (u["id"],))
        return u, None


_DUMMY = hash_password(secrets.token_hex(16))


def session_user(user_id: int, version: int) -> dict[str, Any] | None:
    """The logged-in user for a request, or None when the account was disabled or its sessions were ended."""
    with tx() as conn:
        u = conn.execute("SELECT id, email, nickname, role, lang, must_change_password, session_version FROM users "
                         "WHERE id = %s AND confirmed_at IS NOT NULL AND disabled_at IS NULL", (user_id,)).fetchone()
    return u if u and u["session_version"] == version else None


def start_reset(email: str, base_url: str) -> bool:
    """Queues a reset mail for a confirmed, enabled account (the page says the same either way)."""
    from . import mail
    with tx() as conn:
        u = conn.execute("SELECT id FROM users WHERE lower(email) = lower(%s) AND confirmed_at IS NOT NULL AND disabled_at IS NULL",
                         (normalize_email(email),)).fetchone()
        if u:
            mail.queue(conn, "reset", u["id"], base_url)
        return bool(u)


def reset_token_valid(token: str) -> bool:
    with tx() as conn:
        return conn.execute("SELECT 1 FROM email_tokens WHERE token_sha256 = %s AND purpose = 'reset' AND used_at IS NULL AND expires_at > now()",
                            (hashlib.sha256((token or "").encode()).hexdigest(),)).fetchone() is not None


def finish_reset(token: str, password: str) -> bool:
    check_password(password)
    with tx() as conn:
        t = _use_token(conn, token, "reset")
        if not t:
            return False
        conn.execute("UPDATE users SET password_hash = %s, must_change_password = false, session_version = session_version + 1 WHERE id = %s",
                     (hash_password(password), t["user_id"]))
        _end_reset_links(conn, t["user_id"])
        return True


def change_password(user_id: int, current: str, new: str) -> int:
    """Returns the new session version (the current session stays valid, every other one ends)."""
    check_password(new)
    with tx() as conn:
        u = conn.execute("SELECT password_hash FROM users WHERE id = %s", (user_id,)).fetchone()
        if not u or not verify_password(u["password_hash"], current or ""):
            raise AccountError("err.password_current_wrong")
        if verify_password(u["password_hash"], new):
            raise AccountError("err.password_same")
        _end_reset_links(conn, user_id)
        return conn.execute("UPDATE users SET password_hash = %s, must_change_password = false, session_version = session_version + 1 "
                            "WHERE id = %s RETURNING session_version", (hash_password(new), user_id)).fetchone()["session_version"]


def _end_reset_links(conn, user_id: int) -> None:
    """A password that was just changed makes every reset link still in a mailbox useless."""
    conn.execute("UPDATE email_tokens SET used_at = now() WHERE user_id = %s AND purpose = 'reset' AND used_at IS NULL", (user_id,))


def update_profile(user_id: int, nickname: str, lang: str) -> None:
    with tx() as conn:
        conn.execute("UPDATE users SET nickname = %s, lang = %s WHERE id = %s",
                     ((nickname or "").strip()[:60] or "?", lang if lang in LANGS else "ja", user_id))


# ---------------------------------------------------------------- the admin's user management
def set_disabled(user_id: int, disabled: bool) -> dict[str, Any]:
    with tx() as conn:
        u = conn.execute("SELECT id, role FROM users WHERE id = %s", (user_id,)).fetchone()
        if not u:
            raise AccountError("err.no_user")
        if u["role"] == "admin":
            raise AccountError("err.admin_cannot_be_disabled")
        return conn.execute(
            "UPDATE users SET disabled_at = CASE WHEN %s THEN COALESCE(disabled_at, now()) END, session_version = session_version + 1 "
            "WHERE id = %s RETURNING id, email, disabled_at", (disabled, user_id)).fetchone()


def admin_reset_password(user_id: int) -> tuple[dict[str, Any], str]:
    """A new random password (shown to the admin once, no mail); the user must change it at the next login."""
    password = secrets.token_urlsafe(9)      # 12 characters
    with tx() as conn:
        u = conn.execute("UPDATE users SET password_hash = %s, must_change_password = true, session_version = session_version + 1 "
                         "WHERE id = %s AND role <> 'admin' RETURNING id, email", (hash_password(password), user_id)).fetchone()
        if u:
            _end_reset_links(conn, user_id)
    if not u:
        raise AccountError("err.no_user")
    return u, password


def admin_email() -> str | None:
    with tx() as conn:
        r = conn.execute("SELECT email FROM users WHERE role = 'admin'").fetchone()
    return r["email"] if r else None


def admin_id(conn) -> int | None:
    r = conn.execute("SELECT id FROM users WHERE role = 'admin'").fetchone()
    return r["id"] if r else None
