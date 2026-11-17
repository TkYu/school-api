"""GET /v1/calendar: the school calendar (学年暦) of the academic year a date falls in, from the current calendar PDF.

For Tomo's skills: which events cover the date, and per term the registration period (履修登録期間), the correction
period (履修訂正期間) and the first class day, so a skill can tell whether registrations may still change without
reading the PDF. `found: false` when the year's calendar is not in the API (yet): the skill then reads the
official page itself.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .auth import client_ip
from .db import tx

router = APIRouter(prefix="/v1")
JST = timezone(timedelta(hours=9))
OFFICIAL = "https://www.shonan-it.ac.jp/campuslife/schedule/"


def academic_year(d: date) -> int:
    return d.year if d.month >= 4 else d.year - 1


@router.get("/calendar")
def calendar(request: Request, on: date | None = Query(None, alias="date", description="YYYY-MM-DD, default today (Japan)"),
             program: str = Query("学部", description="学部 or 大学院")):
    from .search import RATE, _limited
    if _limited(client_ip(request)):
        return JSONResponse({"error": "too many requests", "hint": f"at most {RATE} per minute"}, status_code=429)
    d = on or datetime.now(JST).date()
    year = academic_year(d)
    with tx() as conn:
        rows = conn.execute(
            """SELECT e.*, v.url, v.title, v.info_date, v.document_id FROM calendar_events e
               JOIN current_versions v ON v.id = e.version_id
               JOIN document_status ds ON ds.document_id = v.document_id AND NOT ds.gone AND NOT ds.hidden
               WHERE e.year = %s AND e.program = %s ORDER BY e.start_date, e.ord""", (year, program)).fetchall()
    out: dict = {"date": d, "year": year, "program": program, "found": bool(rows)}
    if not rows:
        out["note"] = f"The {year} calendar is not in the API. Read the 学年暦 PDF on {OFFICIAL}"
        return out
    events = [{"term": r["term"], "label": r["label"], "start": r["start_date"], "end": r["end_date"], "as_printed": r["date_text"],
               **({"note": r["note"]} if r["note"] else {})} for r in rows]
    terms: dict[str, dict] = {}
    for e in events:
        t = terms.setdefault(e["term"], {})
        if "履修登録期間" in e["label"]:
            t["registration"] = {"start": e["start"], "end": e["end"]}
        elif "履修訂正期間" in e["label"]:
            t["correction"] = {"start": e["start"], "end": e["end"]}
        elif "授業開始" in e["label"]:
            t["classes_start"] = e["start"]
    # registrations may change from a term's registration start to its correction end
    changing = [term for term, t in terms.items() if "registration" in t
                and t["registration"]["start"] <= d <= (t.get("correction") or t["registration"])["end"]]
    src = rows[0]
    out.update({
        "today": [e for e in events if e["start"] <= d <= e["end"]],
        "terms": terms,
        "registration_open": changing[0] if changing else None,
        "events": events,
        "source": {"title": src["title"], "url": src["url"], "date": src["info_date"], "document_id": src["document_id"]},
        "note": "Dates from the school's 学年暦 PDF; cite its title and URL. registration_open: the term whose registration "
                "or correction period includes the date (registrations may still change), else null.",
    })
    return out


class CalendarIn(BaseModel):
    """GET /v1/calendar as a JSON body (program=学部 survives a Windows terminal only in a body)."""
    on: date | None = Field(None, alias="date")
    program: str = "学部"


@router.post("/calendar")
def calendar_post(request: Request, body: CalendarIn):
    return calendar(request, on=body.on, program=body.program)
