"""Public search for Tomo's skills.

GET /v1/search: SQL filters first (source, faculty, term, year), then BM25 (pg_search, Lindera Japanese) and vector
search (ruri-v3) over the current versions' chunks, merged with reciprocal rank fusion. Every hit names its course
or page, URL and date, for citing. Queries work best in Japanese (the data and the embedding model are Japanese).
GET /v1/courses/{code}: one course's current syllabus with structured fields, evaluation and plan.
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .auth import client_ip
from .db import tx
from .embed import embedder, literal

router = APIRouter(prefix="/v1")
RRF_K = 60
CANDIDATES = 50

# per client: at most this many searches per minute (the agent rarely needs more than a few per question)
RATE, WINDOW = 60, 60.0
_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def _limited(ip: str) -> bool:
    now = time.monotonic()
    with _lock:
        q = _hits[ip]
        while q and now - q[0] > WINDOW:
            q.popleft()
        if len(q) >= RATE:
            return True
        q.append(now)
        return False


_FILTERED = """
  SELECT cv.id AS version_id FROM current_versions cv
  JOIN document_status ds ON ds.document_id = cv.document_id AND NOT ds.gone AND NOT ds.hidden
  JOIN documents d ON d.id = cv.document_id JOIN sources s ON s.id = d.source_id
  LEFT JOIN courses co ON co.version_id = cv.id
  WHERE (%(source)s::text IS NULL OR s.name = %(source)s)
    AND (%(year)s::int IS NULL OR co.year = %(year)s)
"""


JST = timezone(timedelta(hours=9))


def news_weight(category: str | None, url: str | None, when: date | None, today: date) -> float:
    """News stays findable but an old notice should not outrank a current page: ×0.85 after a year, ×0.6 after two."""
    if not when or not (category == "ニュース" or "/topics/" in (url or "")):
        return 1.0
    days = (today - when).days
    return 0.6 if days > 730 else 0.85 if days > 365 else 1.0


@router.get("/search")
def search(request: Request,
           q: str = Query(min_length=1, max_length=500, description="what to look for, in Japanese works best"),
           source: str | None = None, faculty: list[str] | None = Query(None), department: list[str] | None = Query(None),
           field: list[str] | None = Query(None), required: list[str] | None = Query(None),
           grade: int | None = Query(None, ge=1, le=6), term: str | None = None, form: str | None = None,
           eval: list[str] | None = Query(None), year: int | None = None, limit: int = Query(10, ge=1, le=30)):
    if _limited(client_ip(request)):
        return JSONResponse({"error": "too many searches", "hint": f"at most {RATE} per minute"}, status_code=429)
    from .courses import parse_eval
    return run(q, source=source, year=year, limit=limit, filters={
        "faculty": faculty, "department": department, "field": field, "required": required, "grade": grade, "term": term,
        "form": form, "evals": parse_eval(eval or [])})


class SearchIn(BaseModel):
    """The same search as GET, as a JSON body: a terminal on Windows mangles Japanese in command-line arguments (the
    system code page turns 奨学金 into ???), but a body sent from a here-doc arrives as UTF-8."""
    q: str = Field(min_length=1, max_length=500)
    source: str | None = None
    faculty: str | list[str] | None = None
    department: str | list[str] | None = None
    field: str | list[str] | None = None
    required: str | list[str] | None = None
    grade: int | None = Field(None, ge=1, le=6)
    term: str | list[str] | None = None
    form: str | list[str] | None = None
    eval: str | list[str] | None = None
    year: int | None = None
    limit: int = Field(10, ge=1, le=30)


def as_list(v: str | list[str] | None) -> list[str] | None:
    return None if v is None else [v] if isinstance(v, str) else v


def one_value(v: str | list[str] | None) -> str | None:
    """A filter that takes one value (term, form, name …): a model often sends ["メディア授業"]; accept a one-item list."""
    if isinstance(v, list):
        if len(v) > 1:
            raise HTTPException(422, f"one value only here, got {len(v)}: send separate requests or use contains")
        return v[0] if v else None
    return v


@router.post("/search")
def search_post(request: Request, body: SearchIn):
    return search(request, q=body.q, source=body.source, faculty=as_list(body.faculty), department=as_list(body.department),
                  field=as_list(body.field), required=as_list(body.required), grade=body.grade, term=one_value(body.term), form=one_value(body.form),
                  eval=as_list(body.eval), year=body.year, limit=body.limit)


def run(q: str, *, source: str | None = None, faculty: str | None = None, term: str | None = None, year: int | None = None,
        limit: int = 10, filters: dict | None = None) -> dict:
    """The search itself (the public endpoint adds the rate limit; the admin UI calls this directly).
    Course filters are those of /v1/courses (app.courses.course_conditions); a filter on a course field leaves out
    documents that are not courses."""
    from .courses import course_conditions
    params = {"q": q, "qv": literal(embedder().query(q)), "source": source, "year": year,
              "n": CANDIDATES, "pool": min(limit * 6, 2 * CANDIDATES), "k": RRF_K}
    f = dict(filters or {})
    if faculty:
        f["faculty"] = [faculty]
    if term:
        f["term"] = term
    extra = course_conditions(params, **{k: v for k, v in f.items() if v not in (None, [], ())})
    filtered = _FILTERED + "".join(f"    AND {c}\n" for c in extra)
    with tx() as conn:
        rows = conn.execute(f"""
          WITH f AS ({filtered}),
          bm AS (
            SELECT c.id AS chunk_id, row_number() OVER (ORDER BY pdb.score(t.id) DESC) AS r
            FROM texts t JOIN chunks c ON c.text_id = t.id JOIN f ON f.version_id = c.version_id
            WHERE t.search_text ||| %(q)s
            ORDER BY pdb.score(t.id) DESC LIMIT %(n)s),
          vec AS (
            SELECT c.id AS chunk_id, row_number() OVER (ORDER BY e.embedding <=> %(qv)s::vector) AS r
            FROM embeddings_ruri_v3 e JOIN chunks c ON c.text_id = e.text_id JOIN f ON f.version_id = c.version_id
            ORDER BY e.embedding <=> %(qv)s::vector LIMIT %(n)s),
          fused AS (
            SELECT chunk_id, sum(1.0 / (%(k)s + r)) AS score, bool_or(src = 'bm') AS lexical, bool_or(src = 'vec') AS semantic
            FROM (SELECT chunk_id, r, 'bm' AS src FROM bm UNION ALL SELECT chunk_id, r, 'vec' FROM vec) x
            GROUP BY chunk_id ORDER BY score DESC LIMIT %(pool)s)
          SELECT fu.score, fu.lexical, fu.semantic, c.section, t.text, v.document_id, v.id AS version_id, v.title, v.url,
                 v.info_date, v.meta->>'category' AS category, s.name AS source, co.code, co.name AS course, co.faculty, co.term, co.year
          FROM fused fu JOIN chunks c ON c.id = fu.chunk_id JOIN texts t ON t.id = c.text_id
          JOIN versions v ON v.id = c.version_id JOIN documents d ON d.id = v.document_id JOIN sources s ON s.id = d.source_id
          LEFT JOIN courses co ON co.version_id = v.id
          ORDER BY fu.score DESC""", params).fetchall()
    # one hit per document (its best passage; the rest counted), older news further down
    today = datetime.now(JST).date()
    best: dict[int, dict] = {}
    for r in rows:
        score = float(r["score"]) * news_weight(r["category"], r["url"], r["info_date"], today)
        cur = best.get(r["document_id"])
        if cur is None:
            best[r["document_id"]] = {"row": r, "score": score, "more": 0}
        else:
            cur["more"] += 1
            if score > cur["score"]:
                cur["row"], cur["score"] = r, score
    results = []
    for b in sorted(best.values(), key=lambda b: -b["score"])[:limit]:
        r = b["row"]
        hit = {"document_id": r["document_id"], "version_id": r["version_id"], "source": r["source"], "title": r["title"],
               "url": r["url"], "date": r["info_date"], "category": r["category"], "section": r["section"], "text": r["text"],
               "score": round(b["score"], 5), "matched": [m for m, ok in (("words", r["lexical"]), ("meaning", r["semantic"])) if ok]}
        if b["more"]:
            hit["more_in_document"] = b["more"]
        if r["code"]:
            hit["course"] = {"code": r["code"], "name": r["course"], "faculty": r["faculty"], "term": r["term"], "year": r["year"]}
        results.append(hit)
    return {"query": q, "results": results,
            "note": "One hit per document (more_in_document: other matching passages there; open it with /v1/documents/{id}). "
                    "News older than a year ranks lower; check `date` before treating a hit as current. Cite title / course "
                    "code, URL and date. Open a course with /v1/courses/{code} for its whole syllabus."}


@router.get("/courses/{code}")
def course(code: str, year: int | None = None):
    """The current syllabus of a course: fields, evaluation (as printed), plan and every section."""
    with tx() as conn:
        co = conn.execute(
            """SELECT co.*, v.document_id, v.url, v.info_date, v.title FROM courses co JOIN current_versions v ON v.id = co.version_id
               JOIN document_status ds ON ds.document_id = v.document_id AND NOT ds.gone AND NOT ds.hidden
               WHERE co.code = %s AND (%s::int IS NULL OR co.year = %s) ORDER BY co.year DESC NULLS LAST LIMIT 1""",
            (code, year, year)).fetchone()
        if not co:
            raise HTTPException(404, f"no current syllabus with the code {code}" + (f" in {year}" if year else ""))
        vid = co["version_id"]
        evaluation = conn.execute("SELECT item, percent FROM course_evaluations WHERE version_id = %s ORDER BY item", (vid,)).fetchall()
        sessions = conn.execute("SELECT no, topic, prep, review, hours FROM course_sessions WHERE version_id = %s ORDER BY no", (vid,)).fetchall()
        sections = conn.execute("SELECT c.section, t.text FROM chunks c JOIN texts t ON t.id = c.text_id WHERE c.version_id = %s ORDER BY c.ord",
                                (vid,)).fetchall()
    return {"code": co["code"], "year": co["year"], "name": co["name"], "name_en": co["name_en"], "faculty": co["faculty"],
            "department": co["department"], "field": co["field"], "form": co["form"], "grade": co["grade"], "term": co["term"],
            "required": co["required"], "credits": co["credits"], "instructors": co["instructors"],
            "evaluation": {e["item"]: e["percent"] for e in evaluation}, "evaluation_sum": co["eval_sum"], "notes": co["notes"],
            "sessions": sessions, "sections": sections, "url": co["url"], "date": co["info_date"],
            "document_id": co["document_id"], "version_id": vid}
