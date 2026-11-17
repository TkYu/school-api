"""What every page of the admin UI shares: templates, the translator, CSRF tokens in the session, flash messages."""
from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import i18n
from .db import tx

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def csrf(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = request.session["csrf"] = secrets.token_urlsafe(32)
    return tok


def check_csrf(request: Request, token: str) -> None:
    if not token or not secrets.compare_digest(token, request.session.get("csrf", "")):
        raise HTTPException(403, "the form expired; reload the page and try again")


def t(request: Request, key: str, **kw: Any) -> str:
    return i18n.tr(i18n.lang_of(request), key, **kw)


def page(request: Request, name: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
    user = getattr(request.state, "user", None)
    lang = i18n.lang_of(request)
    pending = 0
    if user and user["role"] == "admin":
        with tx() as conn:
            pending = conn.execute("SELECT count(*) AS n FROM batches WHERE status = 'staging'").fetchone()["n"]
    ctx.update(request=request, csrf=csrf(request), user=user, flash=request.session.pop("flash", None),
               t=lambda key, **kw: i18n.tr(lang, key, **kw), lang=lang, html_lang=i18n.HTML_LANG[lang],
               langs=i18n.NAMES, pending_reviews=pending)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def back(request: Request, url: str, flash: str) -> RedirectResponse:
    request.session["flash"] = flash
    return RedirectResponse(url, status_code=303)


def safe_next(url: str, default: str = "/admin") -> str:
    """Only paths inside the admin UI (no open redirect to another site)."""
    return url if url.startswith("/admin") and not url.startswith("//") else default
