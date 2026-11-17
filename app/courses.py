"""Course lists for questions that are conditions or need every match, not the best few.

GET /v1/courses: every current course that meets all filters, sorted by code, with the total; nothing is ranked or
cut off (page with offset). Structured filters (faculty, department, grade, term, form, evaluation weights …) and
`contains`, a plain substring match over the syllabus text (full/half width and case ignored), like SQL LIKE.
GET /v1/courses/facets: the values each field really has, with counts, so a question can be turned into filters
with the exact spelling (e.g. form 講義（メディア授業）).

/v1/search (ranked, meaning-based) stays for vague questions; it shares the filters built here.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .auth import client_ip
from .db import tx

router = APIRouter(prefix="/v1")

EVAL_ITEMS = ("受講態度", "授業中の活動", "予習・復習", "成果物・発表", "試験", "学修の振り返り")
_EVAL_RE = re.compile(r"^\s*(.+?)\s*(<=|>=|=|<|>)\s*(\d{1,3})\s*$")
_OPS = {"=": "=", "<": "<", ">": ">", "<=": "<=", ">=": ">="}

# the current, visible courses of the newest syllabus year (or the year asked for)
_CURRENT = """
  FROM current_versions cv
  JOIN document_status ds ON ds.document_id = cv.document_id AND NOT ds.gone AND NOT ds.hidden
  JOIN courses co ON co.version_id = cv.id
"""


def norm(s: str) -> str:
    """What matching compares: NFKC (Ａ→A, ｶ→カ, １→1) and lower case; the database side uses the same."""
    return unicodedata.normalize("NFKC", s).lower()


def _like(s: str) -> str:
    return "%" + norm(s).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def parse_eval(conds: list[str]) -> list[tuple[str, str, int]]:
    out = []
    for c in conds:
        m = _EVAL_RE.match(unicodedata.normalize("NFKC", c))
        if not m or m[1] not in EVAL_ITEMS:
            raise HTTPException(422, {"error": f"cannot read eval={c!r}",
                                      "hint": "item, operator, percent, e.g. eval=試験=0 or eval=成果物・発表>=40",
                                      "items": EVAL_ITEMS})
        out.append((m[1], _OPS[m[2]], int(m[3])))
    return out


def course_conditions(p: dict[str, Any], *, faculty: list[str] | None = None, department: list[str] | None = None,
                      field: list[str] | None = None, required: list[str] | None = None, credits: list[int] | None = None,
                      grade: int | None = None, term: str | None = None, form: str | None = None, name: str | None = None,
                      instructor: str | None = None, evals: list[tuple[str, str, int]] = (), contains: list[str] | None = None,
                      section: str | None = None) -> list[str]:
    """SQL conditions on `co` (courses) for the filters given; their values go into p. Lists mean "any of these"."""
    w: list[str] = []

    def add(sql: str, **vals: Any) -> None:
        for k, v in vals.items():
            p[k] = v
        w.append(sql)

    if faculty:
        add("co.faculty = ANY(%(f_faculty)s)", f_faculty=faculty)
    if department:
        add("co.department = ANY(%(f_department)s)", f_department=department)
    if field:
        add("co.field = ANY(%(f_field)s)", f_field=field)
    if required:
        add("co.required = ANY(%(f_required)s)", f_required=required)
    if credits:
        add("co.credits = ANY(%(f_credits)s)", f_credits=credits)
    if grade is not None:  # 配当年次 as printed: 2年次, 1・2・3年次 …
        add(r"normalize(co.grade, NFKC) ~ %(f_grade)s", f_grade=rf"(^|[^0-9]){grade}([^0-9]|$)")
    for key, col, val in (("term", "co.term", term), ("form", "co.form", form), ("name", "co.name", name)):
        if val:
            add(f"lower(normalize(coalesce({col}, ''), NFKC)) LIKE %(f_{key})s", **{f"f_{key}": _like(val)})
    if instructor:
        add("EXISTS (SELECT 1 FROM unnest(co.instructors) i WHERE lower(normalize(i, NFKC)) LIKE %(f_instructor)s)",
            f_instructor=_like(instructor.replace(" ", "").replace("　", "")))
    for i, (item, op, pct) in enumerate(evals):
        add(f"EXISTS (SELECT 1 FROM course_evaluations e WHERE e.version_id = co.version_id AND e.item = %(f_ei{i})s "
            f"AND e.percent {op} %(f_ep{i})s)", **{f"f_ei{i}": item, f"f_ep{i}": pct})
    for i, term_ in enumerate(contains or []):
        add(f"""EXISTS (SELECT 1 FROM chunks c JOIN texts t ON t.id = c.text_id WHERE c.version_id = co.version_id
                  AND (%(f_section)s::text IS NULL OR c.section = %(f_section)s)
                  AND lower(normalize(t.text, NFKC)) LIKE %(f_c{i})s)""", **{f"f_c{i}": _like(term_), "f_section": section})
    return w


def _latest_year(conn) -> int | None:
    row = conn.execute(f"SELECT max(co.year) AS y {_CURRENT}").fetchone()
    return row["y"] if row else None


def _snippets(text: str, terms: list[str], width: int = 50) -> list[str]:
    """The words around each match, cut from the original text (normalising keeps the length for almost all text;
    where it does not, the normalised text is shown)."""
    t = norm(text)
    src = text if len(t) == len(text) else t
    out = []
    for term_ in terms:
        n = norm(term_)
        i = t.find(n)
        if i >= 0:
            a, b = max(0, i - width), min(len(t), i + len(n) + width)
            out.append(("…" if a else "") + " ".join(src[a:b].split()) + ("…" if b < len(t) else ""))
    return out


def _limited(request: Request) -> JSONResponse | None:
    from .search import RATE, _limited as limited  # one budget for all public queries
    if limited(client_ip(request)):
        return JSONResponse({"error": "too many requests", "hint": f"at most {RATE} per minute"}, status_code=429)
    return None


@router.get("/courses")
def list_courses(request: Request,
                 faculty: list[str] | None = Query(None, description="学部, e.g. 情報学部 (repeat for any of several)"),
                 department: list[str] | None = Query(None, description="学科・科目区分 as printed, e.g. 情報学科, 共通基盤科目"),
                 field: list[str] | None = Query(None, description="科目分野, e.g. 専門基礎"),
                 required: list[str] | None = Query(None, description="必修 / 選択 / 選択必修"),
                 credits: list[int] | None = Query(None),
                 grade: int | None = Query(None, ge=1, le=6, description="courses open to this year of study (配当年次)"),
                 term: str | None = Query(None, description="contained in 学期, e.g. 前学期 (also matches 前学期（集中）)"),
                 form: str | None = Query(None, description="contained in 授業形態, e.g. メディア授業, 演習"),
                 name: str | None = Query(None, description="contained in the course name"),
                 instructor: str | None = None,
                 eval: list[str] | None = Query(None, description="evaluation weight, e.g. 試験=0, 成果物・発表>=40 (repeat: all must hold)"),
                 contains: list[str] | None = Query(None, description="text the syllabus contains, like SQL LIKE; repeat: all must appear"),
                 section: str | None = Query(None, description="look for `contains` only in this section, e.g. 履修の条件"),
                 year: int | None = Query(None, description="syllabus year; default the newest"),
                 offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    if (resp := _limited(request)) is not None:
        return resp
    evals = parse_eval(eval or [])
    p: dict[str, Any] = {"offset": offset, "limit": limit}
    with tx() as conn:
        p["year"] = year or _latest_year(conn)
        w = ["co.year = %(year)s"] + course_conditions(
            p, faculty=faculty, department=department, field=field, required=required, credits=credits, grade=grade,
            term=term, form=form, name=name, instructor=instructor, evals=evals, contains=contains, section=section)
        rows = conn.execute(
            f"""SELECT co.*, cv.document_id, cv.url, cv.info_date, count(*) OVER () AS total {_CURRENT}
                WHERE {' AND '.join(w)} ORDER BY co.code, co.version_id OFFSET %(offset)s LIMIT %(limit)s""", p).fetchall()
        total = rows[0]["total"] if rows else conn.execute(
            f"SELECT count(*) AS n {_CURRENT} WHERE {' AND '.join(w)}", p).fetchone()["n"]
        vids = [r["version_id"] for r in rows]
        evaluation: dict[int, dict[str, int]] = {}
        for e in conn.execute("SELECT version_id, item, percent FROM course_evaluations WHERE version_id = ANY(%s)", (vids,)):
            evaluation.setdefault(e["version_id"], {})[e["item"]] = e["percent"]
        matches: dict[int, list[dict[str, str]]] = {}
        if contains:
            for c in conn.execute(
                    """SELECT c.version_id, c.section, t.text FROM chunks c JOIN texts t ON t.id = c.text_id
                       WHERE c.version_id = ANY(%s) AND (%s::text IS NULL OR c.section = %s) ORDER BY c.version_id, c.ord""",
                    (vids, section, section)):
                found = _snippets(c["text"], contains)
                if found and len(matches.setdefault(c["version_id"], [])) < 3:
                    matches[c["version_id"]].append({"section": c["section"], "text": " / ".join(found)})
    results = []
    for r in rows:
        item = {"code": r["code"], "name": r["name"], "faculty": r["faculty"], "department": r["department"], "field": r["field"],
                "grade": r["grade"], "term": r["term"], "form": r["form"], "required": r["required"], "credits": r["credits"],
                "instructors": r["instructors"], "evaluation": evaluation.get(r["version_id"], {}), "url": r["url"],
                "date": r["info_date"]}
        if contains:
            item["matches"] = matches.get(r["version_id"], [])
        results.append(item)
    out = {"year": p["year"], "total": total, "offset": offset, "results": results,
           "note": "Complete list, not ranked: every current course that meets all filters, sorted by code. "
                   "Courses with the same name in several departments are separate courses (different codes). "
                   "Open one with /v1/courses/{code}."}
    if offset + len(rows) < total:
        out["next_offset"] = offset + len(rows)
    return out


class CoursesIn(BaseModel):
    """GET /v1/courses as a JSON body (Japanese values survive a Windows terminal only in a body; see SearchIn)."""
    faculty: str | list[str] | None = None
    department: str | list[str] | None = None
    field: str | list[str] | None = None
    required: str | list[str] | None = None
    credits: int | list[int] | None = None
    grade: int | None = Field(None, ge=1, le=6)
    term: str | list[str] | None = None
    form: str | list[str] | None = None
    name: str | list[str] | None = None
    instructor: str | list[str] | None = None
    eval: str | list[str] | None = None
    contains: str | list[str] | None = None
    section: str | list[str] | None = None
    year: int | None = None
    offset: int = Field(0, ge=0)
    limit: int = Field(50, ge=1, le=200)


@router.post("/courses")
def list_courses_post(request: Request, body: CoursesIn):
    from .search import as_list, one_value
    credits = None if body.credits is None else [body.credits] if isinstance(body.credits, int) else body.credits
    return list_courses(request, faculty=as_list(body.faculty), department=as_list(body.department), field=as_list(body.field),
                        required=as_list(body.required), credits=credits, grade=body.grade, term=one_value(body.term),
                        form=one_value(body.form), name=one_value(body.name), instructor=one_value(body.instructor),
                        eval=as_list(body.eval), contains=as_list(body.contains), section=one_value(body.section),
                        year=body.year, offset=body.offset, limit=body.limit)


@router.get("/courses/facets")
def facets(request: Request, year: int | None = None):
    """Every value the filterable fields have (with counts), to turn a question into exact filters."""
    if (resp := _limited(request)) is not None:
        return resp
    out: dict[str, Any] = {}
    with tx() as conn:
        y = year or _latest_year(conn)
        out["year"] = y
        out["years"] = [r["year"] for r in conn.execute(f"SELECT DISTINCT co.year {_CURRENT} WHERE co.year IS NOT NULL ORDER BY 1 DESC")]
        out["total"] = conn.execute(f"SELECT count(*) AS n {_CURRENT} WHERE co.year = %s", (y,)).fetchone()["n"]
        for col in ("faculty", "department", "field", "grade", "term", "form", "required", "credits"):
            out[col] = {str(r["v"]): r["n"] for r in conn.execute(
                f"SELECT co.{col} AS v, count(*) AS n {_CURRENT} WHERE co.year = %s AND co.{col} IS NOT NULL "
                f"GROUP BY 1 ORDER BY 2 DESC, 1", (y,))}
        out["sections"] = [r["section"] for r in conn.execute(
            f"""SELECT c.section, min(c.ord) AS o {_CURRENT} JOIN chunks c ON c.version_id = co.version_id
                WHERE co.year = %s AND c.section IS NOT NULL GROUP BY 1 ORDER BY 2""", (y,))]
    out["evaluation_items"] = list(EVAL_ITEMS)
    out["usage"] = ("GET /v1/courses?faculty=情報学部&department=情報学科&department=共通基盤科目&grade=2&term=後学期"
                    "&eval=試験=0&form=メディア授業&contains=Adobe&section=履修の条件 — lists every match (not ranked). "
                    "department/faculty/field/required/credits: exact values from this list, repeat for any of several; "
                    "term/form/name/instructor/contains: contained (width and case ignored); "
                    "grade: courses open to that year; eval: 試験=0, 試験<=20, 成果物・発表>=40 (all must hold).")
    return out
