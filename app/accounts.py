"""Account pages: log in / out, sign up with e-mail confirmation, forgot / reset password, own settings and password,
the language switcher. Every form carries the session's CSRF token; answers about addresses never reveal whether an
account exists (only the allowlist, which is no secret, is named)."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import i18n, mail, users
from .auth import LoginThrottle, client_ip
from .config import settings
from .web import back, check_csrf, page, safe_next, t

router = APIRouter(prefix="/admin")
_throttles: dict[str, LoginThrottle] = {}


def throttle(kind: str = "login") -> LoginThrottle:
    """Failed logins, and every sign-up / reset request (they send mail), per client."""
    if kind not in _throttles:
        cfg = settings()
        _throttles[kind] = LoginThrottle(cfg.login_max_failures if kind == "login" else 10, cfg.login_window_s)
    return _throttles[kind]


def base_url(request: Request) -> str:
    return (settings().public_url or str(request.base_url)).rstrip("/")


def _home_of(user) -> str:
    if user["must_change_password"]:
        return "/admin/password"
    return "/admin" if user["role"] == "admin" else "/admin/my"


# ---------------------------------------------------------------- log in / out
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return page(request, "login.html", first=_no_admin_yet())


def _no_admin_yet() -> bool:
    from .db import tx
    with tx() as conn:
        return not users.admin_exists(conn)


@router.post("/login")
def login(request: Request, email: str = Form(""), password: str = Form(""), csrf: str = Form("")):
    check_csrf(request, csrf)
    ip = client_ip(request)
    if throttle().locked(ip):
        return back(request, "/admin/login", t(request, "err.login_locked"))
    user, err = users.authenticate(email, password)
    if not user:
        if err == "err.login_wrong":
            throttle().failed(ip)
        return back(request, "/admin/login", t(request, err))
    throttle().reset(ip)
    request.session.clear()
    request.session.update(uid=user["id"], sv=user["session_version"])
    return RedirectResponse(_home_of(user), status_code=303)


@router.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    check_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


# ---------------------------------------------------------------- sign up, confirm
@router.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request):
    return page(request, "signup.html", mail_ok=mail.configured(), first=_no_admin_yet())


@router.post("/signup")
def signup(request: Request, email: str = Form(""), password: str = Form(""), password2: str = Form(""),
           nickname: str = Form(""), csrf: str = Form("")):
    check_csrf(request, csrf)
    ip = client_ip(request)
    if throttle("mail").locked(ip):
        return back(request, "/admin/signup", t(request, "err.too_many_requests"))
    throttle("mail").failed(ip)
    if password != password2:
        return back(request, "/admin/signup", t(request, "err.password_mismatch"))
    if not mail.configured():
        return back(request, "/admin/signup", t(request, "err.mail_not_configured"))
    try:
        users.sign_up(email, password, nickname, i18n.lang_of(request), base_url(request))
    except users.AccountError as e:
        return back(request, "/admin/signup", t(request, str(e)))
    # the same answer when the address already has an account
    return page(request, "message.html", title=t(request, "signup.sent_title"), text=t(request, "signup.sent_text"))


@router.get("/confirm", response_class=HTMLResponse)
def confirm(request: Request, token: str = ""):
    user = users.confirm(token)
    if not user:
        return page(request, "message.html", status_code=400, title=t(request, "confirm.bad_title"), text=t(request, "confirm.bad_text"),
                    link=("/admin/signup", t(request, "signup.title")))
    key = "confirm.ok_admin" if user["role"] == "admin" else "confirm.ok_text"
    return page(request, "message.html", title=t(request, "confirm.ok_title"), text=t(request, key), link=("/admin/login", t(request, "login.title")))


# ---------------------------------------------------------------- forgot / reset
@router.get("/forgot", response_class=HTMLResponse)
def forgot_form(request: Request):
    return page(request, "forgot.html")


@router.post("/forgot")
def forgot(request: Request, email: str = Form(""), csrf: str = Form("")):
    check_csrf(request, csrf)
    ip = client_ip(request)
    if throttle("mail").locked(ip):
        return back(request, "/admin/forgot", t(request, "err.too_many_requests"))
    throttle("mail").failed(ip)
    if not mail.configured():
        return back(request, "/admin/forgot", t(request, "err.mail_not_configured"))
    users.start_reset(email, base_url(request))
    return page(request, "message.html", title=t(request, "forgot.sent_title"), text=t(request, "forgot.sent_text"))


@router.get("/reset", response_class=HTMLResponse)
def reset_form(request: Request, token: str = ""):
    if not users.reset_token_valid(token):
        return page(request, "message.html", status_code=400, title=t(request, "reset.bad_title"), text=t(request, "reset.bad_text"),
                    link=("/admin/forgot", t(request, "forgot.title")))
    return page(request, "reset.html", token=token)


@router.post("/reset")
def reset(request: Request, token: str = Form(""), password: str = Form(""), password2: str = Form(""), csrf: str = Form("")):
    check_csrf(request, csrf)
    if password != password2:
        return back(request, f"/admin/reset?token={token}", t(request, "err.password_mismatch"))
    try:
        ok = users.finish_reset(token, password)
    except users.AccountError as e:
        return back(request, f"/admin/reset?token={token}", t(request, str(e)))
    if not ok:
        return page(request, "message.html", status_code=400, title=t(request, "reset.bad_title"), text=t(request, "reset.bad_text"),
                    link=("/admin/forgot", t(request, "forgot.title")))
    request.session.clear()
    return back(request, "/admin/login", t(request, "reset.done"))


# ---------------------------------------------------------------- own settings, own password
@router.get("/settings", response_class=HTMLResponse)
def settings_form(request: Request):
    return page(request, "settings.html")


@router.post("/settings")
def settings_save(request: Request, nickname: str = Form(""), lang: str = Form("ja"), csrf: str = Form("")):
    check_csrf(request, csrf)
    users.update_profile(request.state.user["id"], nickname, lang)
    r = back(request, "/admin/settings", i18n.tr(lang if lang in i18n.LANGS else "ja", "settings.saved"))
    r.set_cookie("lang", lang if lang in i18n.LANGS else "ja", max_age=365 * 86400, secure=True, samesite="lax")
    return r


@router.get("/password", response_class=HTMLResponse)
def password_form(request: Request):
    return page(request, "password.html", forced=request.state.user["must_change_password"])


@router.post("/password")
def password_save(request: Request, current: str = Form(""), password: str = Form(""), password2: str = Form(""), csrf: str = Form("")):
    check_csrf(request, csrf)
    if password != password2:
        return back(request, "/admin/password", t(request, "err.password_mismatch"))
    try:
        version = users.change_password(request.state.user["id"], current, password)
    except users.AccountError as e:
        return back(request, "/admin/password", t(request, str(e)))
    request.session["sv"] = version     # this session stays, every other one ends
    return back(request, "/admin" if request.state.user["role"] == "admin" else "/admin/my", t(request, "password.changed"))


# ---------------------------------------------------------------- language
@router.get("/lang")
def set_lang(request: Request, l: str = "ja", next: str = "/admin"):   # noqa: E741 (the query name)
    r = RedirectResponse(safe_next(next), status_code=303)
    r.set_cookie("lang", l if l in i18n.LANGS else "ja", max_age=365 * 86400, secure=True, samesite="lax")
    return r
