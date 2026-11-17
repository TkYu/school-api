"""The admin UI in Japanese, Chinese and English. Every text the UI shows (templates, flash messages, mails) is a key
in `messages.MESSAGES` with all three languages; a test checks that every key used exists in each language.

The language: the `lang` cookie (the switcher), else the logged-in user's choice, else the browser's
Accept-Language, else Japanese. The public home page is Japanese only and does not use this."""
from __future__ import annotations

import logging
from typing import Any, Callable

from starlette.requests import Request

from .messages import MESSAGES

LANGS = ("ja", "zh", "en")
DEFAULT = "ja"
NAMES = {"ja": "日本語", "zh": "中文", "en": "English"}
HTML_LANG = {"ja": "ja", "zh": "zh-Hans", "en": "en"}
log = logging.getLogger("i18n")


def tr(lang: str, key: str, **kw: Any) -> str:
    entry = MESSAGES.get(key)
    if entry is None:
        log.error("missing message key %s", key)
        return key
    text = entry.get(lang) or entry[DEFAULT]
    return text.format(**kw) if kw else text


def from_accept_language(header: str) -> str | None:
    """The first of ja / zh / en in the browser's list, by weight."""
    best: list[tuple[float, str]] = []
    for part in (header or "").split(","):
        tag, _, params = part.strip().partition(";")
        q = 1.0
        if params.strip().startswith("q="):
            try:
                q = float(params.strip()[2:])
            except ValueError:
                q = 0.0
        base = tag.strip().lower().split("-")[0]
        if base in LANGS:
            best.append((q, base))
    return max(best, key=lambda x: x[0])[1] if best else None


def lang_of(request: Request) -> str:
    cookie = request.cookies.get("lang")
    if cookie in LANGS:
        return cookie
    user = getattr(request.state, "user", None)
    if user and user.get("lang") in LANGS:
        return user["lang"]
    return from_accept_language(request.headers.get("accept-language", "")) or DEFAULT


def translator(request: Request) -> Callable[..., str]:
    lang = lang_of(request)
    return lambda key, **kw: tr(lang, key, **kw)
