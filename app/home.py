"""The public home page: a plain search page in Japanese (DuckDuckGo-like) over the same search the API serves.
`/` alone shows the search box; `/?q=…&t=<category>` the results (tabs: the categories the admin shows on the home page). Server-rendered, no script, no cookies;
the same rate limit as /v1/search."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from .auth import client_ip
from . import search as search_mod
from .db import tx

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# links from before the tabs came from the database (?t=site …) keep working
OLD_TABS = {"site": "shonan-it-site", "syllabus": "shonan-it-syllabus", "bbs": "sitoss-bbs"}


def tabs() -> dict[str, tuple[str, str | None]]:
    """「すべて」 first (always), then the categories the admin shows on the home page, in their order:
    tab key (the source name) -> (label, source name)."""
    with tx() as conn:
        rows = conn.execute("SELECT name, coalesce(label_ja, name) AS label FROM sources WHERE show_on_home ORDER BY sort_order, name").fetchall()
    return {"all": ("すべて", None), **{r["name"]: (r["label"], r["name"]) for r in rows}}


def labels() -> dict[str, str]:
    with tx() as conn:
        return {r["name"]: r["label_ja"] for r in conn.execute("SELECT name, label_ja FROM sources WHERE label_ja IS NOT NULL")}


def snippet(text: str, q: str, width: int = 120) -> Markup:
    """About 2×width characters around the first query word, the query words in bold (escaped first)."""
    flat = " ".join(text.split())
    words = [w for w in re.split(r"[\s　]+", q) if w]
    at = min([i for i in (flat.lower().find(w.lower()) for w in words) if i >= 0] or [0])
    start = max(0, at - width // 2)
    part = flat[start:start + width * 2]
    html = str(escape(part))
    for w in sorted(set(words), key=len, reverse=True):
        ew = str(escape(w))
        if ew:
            html = re.sub(re.escape(ew), lambda m: f"<b>{m.group(0)}</b>", html, flags=re.IGNORECASE)
    return Markup(("…" if start else "") + html + ("…" if start + width * 2 < len(flat) else ""))


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def home(request: Request, q: str = Query("", max_length=200), t: str = "all", more: int = 0):
    q = q.strip()
    tab_list = tabs()
    t = OLD_TABS.get(t, t)
    tab = t if t in tab_list else "all"
    if not q:
        return templates.TemplateResponse(request, "home.html", {"q": "", "tab": tab, "tabs": tab_list})
    if search_mod._limited(client_ip(request)):
        return templates.TemplateResponse(request, "home.html", {"q": q, "tab": tab, "tabs": tab_list, "error":
                                          "検索が多すぎます。1分ほど待ってからもう一度お試しください。"}, status_code=429)
    data = search_mod.run(q, source=tab_list[tab][1], limit=30 if more else 10)
    names = labels()
    hits = []
    for h in data["results"]:
        badge = names.get(h["source"], h["source"])
        if h.get("course"):
            badge = f"{badge} · {h['course']['code']}"
        elif h.get("category") and h["source"] != "sitoss-bbs":
            badge = f"{badge} · {h['category']}"
        hits.append({**h, "badge": badge, "snippet": snippet(h["text"], q), "date": str(h["date"] or "")[:10]})
    return templates.TemplateResponse(request, "home.html", {"q": q, "tab": tab, "tabs": tab_list, "hits": hits, "more": bool(more)})
