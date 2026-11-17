"""Worker handler `process_version`: extract text from a new version, store structured fields, cut chunks, embed.
Idempotent (a retried job replaces what an earlier attempt wrote for the same version). Texts are shared across
versions by hash, so a paragraph that did not change keeps its vector and is not embedded again."""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from psycopg.types.json import Jsonb

from .config import settings
from .db import tx
from .embed import embedder, literal
from . import files
from .parsers import bbs, calendar, office, pdf, syllabus
from .parsers import text as text_parser

log = logging.getLogger("processing")


def _file_bytes(sha: str) -> bytes:
    return (settings().files_dir / sha[:2] / sha).read_bytes()


def _plain_text_chunks(text: str, size: int = 800) -> list[tuple[str, str]]:
    """Text of a page (lines from the crawler's main column): lines joined up to ~size characters, a cut at blank
    lines when a chunk is past half its size, and a line longer than twice the size cut on its own (one chunk per
    page made texts of 50,000 characters: useless for search and too large to embed)."""
    out, cur = [], ""
    for line in (ln.strip() for ln in text.splitlines()):
        if not line:
            if len(cur) > size // 2:
                out.append(("", cur))
                cur = ""
            continue
        while len(line) > size * 2:
            if cur:
                out.append(("", cur))
                cur = ""
            out.append(("", line[:size]))
            line = line[size:]
        if cur and len(cur) + len(line) + 1 > size:
            out.append(("", cur))
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(("", cur))
    return out


def _store_syllabus(conn, version_id: int, syl: syllabus.Syllabus) -> None:
    f = syl.fields
    conn.execute("DELETE FROM courses WHERE version_id = %s", (version_id,))
    conn.execute("DELETE FROM course_evaluations WHERE version_id = %s", (version_id,))
    conn.execute("DELETE FROM course_sessions WHERE version_id = %s", (version_id,))
    instructors = [x for x in (f.get("担当教員") or "").replace("、", " ").replace(",", " ").split() if x]
    conn.execute(
        """INSERT INTO courses (version_id, year, code, numbering, name, name_en, faculty, department, field, form, grade, term,
                                required, credits, instructors, active_learning, teacher_training, practical, eval_sum, notes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (version_id, syl.year, f.get("シラバスコード", ""), f.get("ナンバリング"), f.get("科目名"), f.get("科目名英文"), f.get("学部"),
         f.get("学科・科目区分"), f.get("科目分野"), f.get("授業形態"), f.get("配当年次"), f.get("学期"), f.get("必選区分"),
         syllabus.credits(f), instructors, f.get("アクティブラーニング"), f.get("教職科目対応"), f.get("実践的教育対応"),
         syl.eval_sum, Jsonb(syl.notes)))
    for item, pct in (syl.evaluation or {}).items():
        conn.execute("INSERT INTO course_evaluations (version_id, item, percent) VALUES (%s, %s, %s)", (version_id, item, pct))
    for s in syl.sessions:
        conn.execute("INSERT INTO course_sessions (version_id, no, topic, prep, review, hours) VALUES (%s, %s, %s, %s, %s, %s) "
                     "ON CONFLICT DO NOTHING", (version_id, s["no"], s["topic"], s["prep"], s["review"], s["hours"] or None))


def _store_calendar(conn, version_id: int, cal: calendar.Calendar) -> None:
    conn.execute("DELETE FROM calendar_events WHERE version_id = %s", (version_id,))
    for i, e in enumerate(cal.events):
        conn.execute("""INSERT INTO calendar_events (version_id, ord, year, program, term, label, start_date, end_date, date_text, note)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                     (version_id, i, cal.year, cal.program, e.term, e.label, e.start, e.end, e.date_text, e.note))
    if cal.notes or not cal.events:
        conn.execute("UPDATE versions SET meta = meta || %s WHERE id = %s",
                     (Jsonb({"calendar_notes": cal.notes or ["no events found"]}), version_id))


def _store_chunks(conn, version_id: int, context: str, parts: list[tuple[str, ...]]) -> None:
    """parts: (section, text) or (section, text, what the index sees instead of the text, e.g. a translation)."""
    conn.execute("DELETE FROM chunks WHERE version_id = %s", (version_id,))
    for ord_, part in enumerate(parts):
        section, text = part[0], part[1]
        indexed = part[2] if len(part) > 2 else text
        # every chunk carries its document's identity (旧 RAG: only the first chunk named the course)
        search_text = "\n".join(x for x in (context, section, indexed) if x)
        sha = hashlib.sha256(search_text.encode("utf-8")).hexdigest()
        row = conn.execute("INSERT INTO texts (sha256, text, search_text) VALUES (%s, %s, %s) ON CONFLICT (sha256) DO NOTHING RETURNING id",
                           (sha, text, search_text)).fetchone() or conn.execute("SELECT id FROM texts WHERE sha256 = %s", (sha,)).fetchone()
        conn.execute("INSERT INTO chunks (version_id, ord, section, text_id) VALUES (%s, %s, %s, %s)", (version_id, ord_, section or None, row["id"]))


def embed_missing(version_id: int) -> int:
    """Vectors for the version's texts that have none yet (shared texts are embedded once)."""
    with tx() as conn:
        rows = conn.execute(
            """SELECT DISTINCT t.id, t.search_text FROM chunks c JOIN texts t ON t.id = c.text_id
               LEFT JOIN embeddings_ruri_v3 e ON e.text_id = t.id WHERE c.version_id = %s AND e.text_id IS NULL""", (version_id,)).fetchall()
    if not rows:
        return 0
    vectors = embedder().documents([r["search_text"] for r in rows])
    with tx() as conn:
        for r, v in zip(rows, vectors):
            conn.execute("INSERT INTO embeddings_ruri_v3 (text_id, embedding) VALUES (%s, %s::vector) ON CONFLICT DO NOTHING", (r["id"], literal(v)))
    return len(rows)


def process_version(payload: dict[str, Any]) -> None:
    version_id = int(payload["version_id"])
    with tx() as conn:
        v = conn.execute(
            """SELECT v.id, v.body, v.mime, v.title, v.url, v.meta, s.kind AS source_kind, b.embed,
                      (SELECT f.sha256 FROM files f WHERE f.version_id = v.id ORDER BY f.id LIMIT 1) AS sha,
                      (SELECT f.name FROM files f WHERE f.version_id = v.id ORDER BY f.id LIMIT 1) AS file_name
               FROM versions v JOIN documents d ON d.id = v.document_id JOIN sources s ON s.id = d.source_id
               JOIN batches b ON b.id = v.batch_id WHERE v.id = %s""", (version_id,)).fetchone()
        if not v:
            return  # purged meanwhile
        is_pdf = (v["mime"] or "").startswith("application/pdf")
        if v["source_kind"] == "syllabus" and is_pdf and v["sha"]:
            lines, tables, words = syllabus.read_pdf(_file_bytes(v["sha"]))
            syl = syllabus.parse(lines, tables, words)
            body = "\n".join(lines)
            title = syl.fields.get("科目名") or v["title"]
            _store_syllabus(conn, version_id, syl)
            context, parts = syllabus.context_line(syl), syllabus.chunks(syl)
            conn.execute("UPDATE versions SET body = %s, title = %s, info_date = COALESCE(info_date, make_date(%s, 4, 1)) WHERE id = %s",
                         (body, title, syl.year, version_id))  # make_date(NULL, …) stays NULL
        elif v["source_kind"] == "bbs" and v["sha"]:
            # the topic's JSON is the version's file (so a reparse finds it); the body is its readable text
            topic = bbs.parse(_file_bytes(v["sha"]).decode("utf-8"))
            meta = v["meta"] or {}
            context = " ".join(x for x in (v["title"] or "", topic.get("title_ja") or "", meta.get("bbs_category") or "", "コミュニティ掲示板") if x)
            parts = bbs.chunks(topic)
            conn.execute("UPDATE versions SET body = %s WHERE id = %s", (bbs.readable(topic), version_id))
        elif is_pdf and v["sha"]:
            data = _file_bytes(v["sha"])
            pages = pdf.read_pages(data)
            body = "\n\n".join(pages)
            context = " ".join(x for x in (v["title"] or "", (v["meta"] or {}).get("category") or "") if x)
            parts = pdf.chunks(pages)
            if calendar.looks_like(pages[0] if pages else ""):
                cal = calendar.parse_pdf(data)
                _store_calendar(conn, version_id, cal)
                if cal.events:  # the events as clean lines (the page text mixes the columns)
                    parts = [(f"{cal.program} {term}", "\n".join(f"{e.start:%Y-%m-%d}" + (f"〜{e.end:%Y-%m-%d}" if e.end != e.start else "")
                                                               + f" {e.label}" for e in cal.events if e.term == term))
                             for term in dict.fromkeys(e.term for e in cal.events)]
            conn.execute("UPDATE versions SET body = %s WHERE id = %s", (body, version_id))
        elif v["sha"] and v["body"] is None and (kind := files.kind_of(v["file_name"], v["mime"])) in ("docx", "xlsx", "pptx"):
            # Office Open XML: sections by heading / sheet / slide, each cut like page text
            try:
                doc_title, sections = getattr(office, kind)(_file_bytes(v["sha"]))
            except office.OfficeError as e:
                doc_title, sections = None, []
                conn.execute("UPDATE versions SET meta = meta || %s WHERE id = %s", (Jsonb({"parse_error": str(e)[:300]}), version_id))
            body = "\n\n".join(f"{sec}\n{text}" if sec else text for sec, text in sections)
            title = v["title"] or doc_title
            conn.execute("UPDATE versions SET body = %s, title = %s WHERE id = %s", (body, title, version_id))
            context = " ".join(x for x in (title or "", (v["meta"] or {}).get("category") or "") if x)
            parts = [(sec, chunk) for sec, text in sections for _, chunk in _plain_text_chunks(text)]
        else:
            body, title = v["body"], v["title"]
            if body is None and v["sha"]:
                # text-like files (detected encoding, HTML / RTF / CSV … cleaned); anything else as best-effort UTF-8
                kind = files.kind_of(v["file_name"], v["mime"])
                data = _file_bytes(v["sha"])
                if kind and kind not in ("pdf", "zip"):
                    doc_title, body = text_parser.extract(kind, data)
                    title = title or doc_title
                else:
                    body = text_parser.clean(text_parser.decode(data))
            conn.execute("UPDATE versions SET body = %s, title = %s WHERE id = %s", (body, title, version_id))
            context = " ".join(x for x in (title or "", (v["meta"] or {}).get("category") or "") if x)
            parts = _plain_text_chunks(body or "")
        _store_chunks(conn, version_id, context, parts)
    if v["embed"] == "remote":
        log.info("version %s: %d chunks (vectors come from outside)", version_id, len(parts))
        return
    n = embed_missing(version_id)
    log.info("version %s: %d chunks, %d new vectors", version_id, len(parts), n)


def register(handlers: dict) -> None:
    handlers["process_version"] = process_version
