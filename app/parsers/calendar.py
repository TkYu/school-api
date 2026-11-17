"""The school calendar (学年暦) PDF: one page, 前学期 on the left half, 後学期 on the right. Each half has a month
column (4月 … 3月, the label centred in its block), a small month grid, then 期日又は期間 and 行事. Dates are printed
without their month ("7日(火)~15日(水)"), so each row's month comes from the weekday (which months of the term
have that day on that weekday) and, between those, the month label nearest to the row. Word positions come from
PyMuPDF; nothing is guessed from the reading order, which mixes the columns.

Returns events as printed (label, the date text) with real dates; a row whose weekday fits no month keeps the
nearest label's month and a note, so the admin review sees it.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta

WEEKDAYS = {"月": 0, "火": 1, "水": 2, "木": 3, "金": 4, "土": 5, "日": 6}
# 7日(火) / 10月1日(木), joined by ~ (a period) or ・ (two days)
_DAY = r"(?:(\d{1,2})月)?(\d{1,2})日\(([月火水木金土日])\)"
DATE_RE = re.compile(rf"^{_DAY}(?:\s*([~・])\s*{_DAY})?")
TITLE_RE = re.compile(r"令和\s*\d+\s*\((\d{4})\)\s*年度")
TERM_RE = re.compile(r"(前学期|後学期)\s*(大学院)?\s*学年暦")
MONTH_RE = re.compile(r"^(\d{1,2})月$")


@dataclass
class Event:
    term: str              # 前学期 / 後学期
    label: str             # 行事 as printed
    start: date
    end: date
    date_text: str         # 期日又は期間 as printed
    note: str | None = None


@dataclass
class Calendar:
    year: int | None = None          # 年度 (令和8(2026)年度 → 2026)
    program: str = "学部"            # 学部 / 大学院
    events: list[Event] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _n(s: str) -> str:
    return unicodedata.normalize("NFKC", s).replace("〜", "~").replace("～", "~")


def looks_like(text: str) -> bool:
    t = _n(text)
    return "学年暦" in t and "期日又は期間" in t


def _year_of(fiscal: int, month: int) -> int:
    return fiscal if month >= 4 else fiscal + 1


def _resolve(day: int, wd: int, months: list[tuple[int, float]], y: float, fiscal: int) -> tuple[int, str | None]:
    """The month of `day` with weekday `wd`: among the half's months whose day has that weekday, the label nearest y."""
    fits = []
    for m, label_y in months:
        try:
            if date(_year_of(fiscal, m), m, day).weekday() == wd:
                fits.append((abs(label_y - y), m))
        except ValueError:
            continue
    if fits:
        return min(fits)[1], None
    nearest = min(months, key=lambda x: abs(x[1] - y))[0]
    return nearest, f"weekday does not match: month {nearest} assumed"


def parse_page(words: list[tuple], width: float) -> Calendar:
    """`words`: PyMuPDF get_text("words") tuples (x0, y0, x1, y1, text, …) of the calendar page."""
    cal = Calendar()
    halves = ([w for w in words if w[0] < width / 2], [w for w in words if w[0] >= width / 2])
    for half in halves:
        ws = [(w[0], w[1], w[2], w[3], _n(w[4])) for w in half]
        head = " ".join(t for _, y0, _, _, t in sorted(ws, key=lambda w: (round(w[1]), w[0])) if y0 < 50)
        if cal.year is None and (m := TITLE_RE.search(head)):
            cal.year = int(m[1])
        tm = TERM_RE.search(head.replace(" ", ""))
        if not tm:
            continue
        term = tm[1]
        if tm[2]:
            cal.program = "大学院"
        hdr_date = next((w for w in ws if w[4] == "期日又は期間"), None)
        hdr_day = next((w for w in ws if w[4] == "日" and w[1] < 65), None)
        if not hdr_date or cal.year is None:
            cal.notes.append(f"{term}: table header not found")
            continue
        event_x = hdr_date[2] + 25            # the 行事 column starts right of the date column
        date_x = (hdr_day[0] if hdr_day else hdr_date[0] - 100) + 115   # right of the month grid
        bottom = min([w[1] for w in ws if w[4].startswith("◎")] or [10_000])
        months = sorted({(int(m[1]), w[1]) for w in ws if (m := MONTH_RE.match(w[4])) and hdr_day and w[2] < hdr_day[0]},
                        key=lambda x: x[1])
        if not months:
            cal.notes.append(f"{term}: no month labels")
            continue
        body = [w for w in ws if hdr_date[3] < w[1] < bottom - 2]
        rows: list[dict] = []
        # date cells (sometimes one word with the event: "24日(木)~10月1日(木)履修登録期間")
        for x0, y0, _, _, t in sorted((w for w in body if date_x <= w[0] < event_x), key=lambda w: w[1]):
            if not (m := DATE_RE.match(t.replace(" ", ""))):
                continue
            rows.append({"y": y0, "m": m, "date_text": m[0], "label": [t.replace(" ", "")[m.end():]] if m.end() < len(t.replace(" ", "")) else []})
        # 行事 words go to the date row on their line, else they continue the row above (two-line events)
        for x0, y0, _, _, t in sorted((w for w in body if w[0] >= event_x), key=lambda w: (w[1], w[0])):
            if not rows:
                continue
            near = min(rows, key=lambda r: abs(r["y"] - y0))
            if abs(near["y"] - y0) > 4:
                above = [r for r in rows if r["y"] < y0]
                near = above[-1] if above else near
            near["label"].append(t)
        for r in rows:
            m = r["m"]
            d1, wd1 = int(m[2]), WEEKDAYS[m[3]]
            month1, note = (int(m[1]), None) if m[1] else _resolve(d1, wd1, months, r["y"], cal.year)
            start = date(_year_of(cal.year, month1), month1, d1)
            end = start
            if m[5]:
                d2 = int(m[6])
                month2 = int(m[5]) if m[5] else (month1 if d2 >= d1 else month1 % 12 + 1)
                end = date(_year_of(cal.year, month2), month2, d2)
                if end < start:
                    note = (note + "; " if note else "") + "end before start"
                    end = start
                elif end - start > timedelta(days=200):
                    note = (note + "; " if note else "") + "period longer than a term"
            elif m[6]:   # "7日(火)~15日(水)" without a month on the second day
                d2 = int(m[6])
                month2 = month1 if d2 >= d1 else month1 % 12 + 1
                end = date(_year_of(cal.year, month2), month2, d2)
            label = "".join(r["label"]).strip()
            if not label:
                note = (note + "; " if note else "") + "no event text"
            cal.events.append(Event(term, label, start, end, r["date_text"], note))
    return cal


def parse_pdf(data: bytes) -> Calendar:
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        page = doc[0]
        return parse_page(page.get_text("words"), page.rect.width)


def summary(cal: Calendar) -> dict:
    """Registration, correction and first class day per term (what Tomo's skills ask for)."""
    out: dict[str, dict] = {}
    for e in cal.events:
        t = out.setdefault(e.term, {})
        if "履修登録期間" in e.label:
            t["registration"] = {"start": e.start.isoformat(), "end": e.end.isoformat()}
        elif "履修訂正期間" in e.label:
            t["correction"] = {"start": e.start.isoformat(), "end": e.end.isoformat()}
        elif "授業開始" in e.label:
            t["classes_start"] = e.start.isoformat()
    return out
