"""Ingest API (crawlers and the web UI): open a batch, put documents into it, finish it, publish or retire it.

Versioning (see sql/001_init.sql): a document put with the same content as its newest version only records a
sighting; different content becomes a new version. Nothing is overwritten or deleted except by `purge`.
"""
from __future__ import annotations

import base64
import hashlib
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from .config import settings
from .db import tx
from .users import admin_id

router = APIRouter(prefix="/v1/admin")


def _audit(conn, request: Request, action: str, target: str, detail: dict[str, Any] | None = None) -> None:
    conn.execute("INSERT INTO audit_log (actor, user_id, action, target, detail) VALUES (%s, %s, %s, %s, %s)",
                 (getattr(request.state, "actor", "?"), getattr(request.state, "user_id", None), action, target, _json(detail or {})))


def _json(value: Any):
    from psycopg.types.json import Jsonb
    return Jsonb(value)


# ---------------------------------------------------------------- sources
class SourceIn(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    kind: Literal["syllabus", "site", "bbs", "upload"]
    base_url: str | None = None
    config: dict[str, Any] = {}


@router.put("/sources/{name}")
def put_source(name: str, body: SourceIn, request: Request):
    if body.name != name:
        raise HTTPException(400, "the name in the path and the body differ")
    with tx() as conn:
        row = conn.execute(
            """INSERT INTO sources (name, kind, base_url, config) VALUES (%s, %s, %s, %s)
               ON CONFLICT (name) DO UPDATE SET kind = EXCLUDED.kind, base_url = EXCLUDED.base_url, config = EXCLUDED.config
               RETURNING id, name, kind, base_url, config""",
            (name, body.kind, body.base_url, _json(body.config))).fetchone()
        _audit(conn, request, "source.put", name)
    return row


@router.get("/sources")
def list_sources():
    with tx() as conn:
        return conn.execute("SELECT id, name, kind, base_url, config, created_at FROM sources ORDER BY name").fetchall()


# ---------------------------------------------------------------- batches
class BatchIn(BaseModel):
    source: str
    label: str = Field(min_length=1, max_length=200)
    kind: Literal["full", "partial"]
    embed: Literal["worker", "remote"] = "worker"   # remote: vectors come from outside (PUT /v1/admin/embeddings)


@router.post("/batches", status_code=201)
def open_batch(body: BatchIn, request: Request):
    with tx() as conn:
        src = conn.execute("SELECT id FROM sources WHERE name = %s", (body.source,)).fetchone()
        if not src:
            raise HTTPException(404, f"no source '{body.source}' (create it with PUT /v1/admin/sources/{{name}})")
        # the submitter's own batch; the ingest token's (and anything before an admin exists) belongs to the admin
        owner = getattr(request.state, "user_id", None) or admin_id(conn)
        row = conn.execute("INSERT INTO batches (source_id, label, kind, embed, owner_id) VALUES (%s, %s, %s, %s, %s) "
                           "RETURNING id, label, kind, embed, status, started_at, owner_id",
                           (src["id"], body.label, body.kind, body.embed, owner)).fetchone()
        _audit(conn, request, "batch.open", str(row["id"]), {"source": body.source, "label": body.label})
    return row


def _batch(conn, batch_id: int, *, for_update: bool = False) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM batches WHERE id = %s" + (" FOR UPDATE" if for_update else ""), (batch_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"no batch {batch_id}")
    return row


class DocumentIn(BaseModel):
    key: str = Field(min_length=1, max_length=2000)   # normalised URL / course key / thread id
    url: str | None = None
    title: str | None = None
    lang: str | None = None
    info_date: date | None = None
    mime: str | None = None
    text: str | None = None                            # extracted text, when the sender has it
    raw_base64: str | None = None                      # the original bytes (PDF, HTML …)
    file_name: str | None = None
    fetched_at: datetime | None = None
    meta: dict[str, Any] = {}

    @model_validator(mode="after")
    def _content(self):
        if self.text is None and self.raw_base64 is None:
            raise ValueError("send text, raw_base64 or both")
        return self


def _store_file(raw: bytes) -> str:
    sha = hashlib.sha256(raw).hexdigest()
    path: Path = settings().files_dir / sha[:2] / sha
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(raw)
        tmp.replace(path)
    return sha


@router.put("/batches/{batch_id}/documents")
def put_document(batch_id: int, body: DocumentIn, request: Request):
    raw = base64.b64decode(body.raw_base64, validate=True) if body.raw_base64 is not None else None
    content_hash = hashlib.sha256(raw if raw is not None else body.text.encode("utf-8")).hexdigest()
    seen = body.fetched_at or datetime.now(timezone.utc)
    with tx() as conn:
        batch = _batch(conn, batch_id)
        if batch["status"] != "open":
            raise HTTPException(409, f"batch {batch_id} is {batch['status']}; documents go into an open batch")
        doc = conn.execute(
            """INSERT INTO documents (source_id, key, first_seen_at, last_seen_at) VALUES (%s, %s, %s, %s)
               ON CONFLICT (source_id, key) DO UPDATE SET last_seen_at = GREATEST(documents.last_seen_at, EXCLUDED.last_seen_at)
               RETURNING id""", (batch["source_id"], body.key, seen, seen)).fetchone()
        # compare with the newest version that is not in a retired batch (open, staged or published): a re-crawl of
        # unchanged content is a sighting; content equal to a rolled-back version comes back as a new version
        newest = conn.execute(
            """SELECT v.id, v.content_hash FROM versions v JOIN batches b ON b.id = v.batch_id
               WHERE v.document_id = %s AND b.status <> 'retired' ORDER BY v.first_seen_at DESC, v.id DESC LIMIT 1""",
            (doc["id"],)).fetchone()
        if newest and newest["content_hash"] == content_hash:
            conn.execute("INSERT INTO sightings (version_id, batch_id, seen_at) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                         (newest["id"], batch_id, seen))
            return {"document_id": doc["id"], "version_id": newest["id"], "changed": False}
        ver = conn.execute(
            """INSERT INTO versions (document_id, batch_id, content_hash, title, url, lang, info_date, mime, body, meta, first_seen_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (doc["id"], batch_id, content_hash, body.title, body.url, body.lang, body.info_date, body.mime, body.text,
             _json(body.meta), seen)).fetchone()
        conn.execute("INSERT INTO sightings (version_id, batch_id, seen_at) VALUES (%s, %s, %s)", (ver["id"], batch_id, seen))
        if raw is not None:
            sha = _store_file(raw)
            conn.execute("INSERT INTO files (version_id, sha256, mime, size, name) VALUES (%s, %s, %s, %s, %s)",
                         (ver["id"], sha, body.mime, len(raw), body.file_name))
        # extraction, chunking and embedding happen in the worker
        conn.execute("INSERT INTO jobs (kind, payload) VALUES ('process_version', %s)", (_json({"version_id": ver["id"]}),))
    return {"document_id": doc["id"], "version_id": ver["id"], "changed": True}


class FinishIn(BaseModel):
    publish: bool = False   # publish at once (else it waits in staging for review)


@router.post("/batches/{batch_id}/finish")
def finish_batch(batch_id: int, body: FinishIn, request: Request):
    with tx() as conn:
        batch = _batch(conn, batch_id, for_update=True)
        if batch["status"] != "open":
            raise HTTPException(409, f"batch {batch_id} is already {batch['status']}")
        stats = conn.execute(
            """SELECT count(*) FILTER (WHERE v.batch_id = %(b)s) AS new_versions,
                      count(*) FILTER (WHERE v.batch_id <> %(b)s) AS unchanged
               FROM sightings s JOIN versions v ON v.id = s.version_id WHERE s.batch_id = %(b)s""", {"b": batch_id}).fetchone()
        status = "published" if body.publish else "staging"
        row = conn.execute(
            """UPDATE batches SET status = %s, finished_at = now(), published_at = CASE WHEN %s THEN now() END, stats = %s
               WHERE id = %s RETURNING id, label, kind, status, finished_at, stats""",
            (status, body.publish, _json(dict(stats)), batch_id)).fetchone()
        _audit(conn, request, "batch.finish", str(batch_id), {"status": status, **dict(stats)})
    return row


def _set_status(batch_id: int, request: Request, to: str, allowed_from: tuple[str, ...]) -> dict[str, Any]:
    with tx() as conn:
        batch = _batch(conn, batch_id, for_update=True)
        if batch["status"] not in allowed_from:
            raise HTTPException(409, f"batch {batch_id} is {batch['status']}; cannot become {to}")
        row = conn.execute(
            "UPDATE batches SET status = %s, published_at = CASE WHEN %s = 'published' THEN now() ELSE published_at END WHERE id = %s "
            "RETURNING id, label, kind, status, published_at", (to, to, batch_id)).fetchone()
        _audit(conn, request, f"batch.{to}", str(batch_id))
    return row


@router.post("/batches/{batch_id}/publish")
def publish_batch(batch_id: int, request: Request):
    return _set_status(batch_id, request, "published", ("staging", "retired"))


@router.post("/batches/{batch_id}/retire")
def retire_batch(batch_id: int, request: Request):
    """Takes the batch out of search (its documents fall back to their previous published version); data stays."""
    return _set_status(batch_id, request, "retired", ("staging", "published"))


@router.get("/batches")
def list_batches(source: str | None = None):
    with tx() as conn:
        return conn.execute(
            """SELECT b.id, s.name AS source, b.label, b.kind, b.status, b.started_at, b.finished_at, b.published_at, b.stats
               FROM batches b JOIN sources s ON s.id = b.source_id
               WHERE %(src)s::text IS NULL OR s.name = %(src)s ORDER BY b.started_at DESC LIMIT 200""", {"src": source}).fetchall()


# ---------------------------------------------------------------- purge (the only real deletion)
class PurgeIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@router.post("/documents/{document_id}/purge")
def purge_document(document_id: int, body: PurgeIn, request: Request):
    """Deletes a document with every version, sighting, file record, chunk and vector (e.g. personal data that must
    not stay in the history). Logged with the reason; the log does not keep the content."""
    with tx() as conn:
        doc = conn.execute("SELECT d.id, d.key, s.name AS source FROM documents d JOIN sources s ON s.id = d.source_id WHERE d.id = %s",
                           (document_id,)).fetchone()
        if not doc:
            raise HTTPException(404, f"no document {document_id}")
        shas = [r["sha256"] for r in conn.execute(
            "SELECT DISTINCT f.sha256 FROM files f JOIN versions v ON v.id = f.version_id WHERE v.document_id = %s", (document_id,))]
        conn.execute("DELETE FROM documents WHERE id = %s", (document_id,))
        # texts (and their vectors) no other version uses any more
        conn.execute("DELETE FROM texts t WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.text_id = t.id)")
        # a file may be shared with another document (same bytes): delete only the unreferenced ones
        orphans = [sha for sha in shas if not conn.execute("SELECT 1 FROM files WHERE sha256 = %s LIMIT 1", (sha,)).fetchone()]
        _audit(conn, request, "document.purge", str(document_id), {"source": doc["source"], "key": doc["key"], "reason": body.reason})
    for sha in orphans:
        (settings().files_dir / sha[:2] / sha).unlink(missing_ok=True)
    return {"purged": document_id, "files_deleted": len(orphans)}


# ---------------------------------------------------------------- per document: hide / show, process again
class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class IdsIn(BaseModel):
    document_ids: list[int] = Field(min_length=1, max_length=5000)


def hide_documents(ids: list[int], reason: str, request: Request) -> int:
    with tx() as conn:
        n = conn.execute("UPDATE documents SET hidden_at = now(), hidden_reason = %s, hidden_by = %s WHERE id = ANY(%s) AND hidden_at IS NULL",
                         (reason, getattr(request.state, "actor", "?"), ids)).rowcount
        _audit(conn, request, "documents.hide", f"{n} of {len(ids)}", {"ids": ids[:50], "reason": reason})
    return n


def show_documents(ids: list[int], request: Request) -> int:
    with tx() as conn:
        n = conn.execute("UPDATE documents SET hidden_at = NULL, hidden_reason = NULL, hidden_by = NULL WHERE id = ANY(%s) AND hidden_at IS NOT NULL",
                         (ids,)).rowcount
        _audit(conn, request, "documents.show", f"{n} of {len(ids)}", {"ids": ids[:50]})
    return n


def reprocess_versions(version_ids: list[int], request: Request, what: str) -> int:
    """Parse, chunk (and embed) again, e.g. after a parser fix. Vectors of unchanged texts are kept."""
    with tx() as conn:
        for vid in version_ids:
            conn.execute("INSERT INTO jobs (kind, payload) VALUES ('process_version', %s)", (_json({"version_id": vid}),))
        _audit(conn, request, "reprocess", what, {"versions": len(version_ids)})
    return len(version_ids)


def newest_versions(ids: list[int]) -> list[int]:
    """The newest version of each document (published or not): what a reprocess should redo."""
    with tx() as conn:
        return [r["id"] for r in conn.execute(
            "SELECT DISTINCT ON (document_id) id FROM versions WHERE document_id = ANY(%s) ORDER BY document_id, first_seen_at DESC, id DESC", (ids,))]


class HideIn(IdsIn):
    reason: str = Field(min_length=3, max_length=500)


@router.post("/documents/hide")
def api_hide(body: HideIn, request: Request):
    return {"hidden": hide_documents(body.document_ids, body.reason, request)}


@router.post("/documents/show")
def api_show(body: IdsIn, request: Request):
    return {"shown": show_documents(body.document_ids, request)}


@router.post("/documents/reprocess")
def api_reprocess(body: IdsIn, request: Request):
    return {"queued": reprocess_versions(newest_versions(body.document_ids), request, f"{len(body.document_ids)} documents")}


@router.post("/batches/{batch_id}/reprocess")
def api_reprocess_batch(batch_id: int, request: Request):
    with tx() as conn:
        _batch(conn, batch_id)
        vids = [r["id"] for r in conn.execute("SELECT id FROM versions WHERE batch_id = %s", (batch_id,))]
    return {"queued": reprocess_versions(vids, request, f"batch {batch_id}")}


@router.post("/batches/{batch_id}/purge")
def purge_batch(batch_id: int, body: PurgeIn, request: Request):
    """For a wrong import: deletes the versions this batch created and every document left without a version.
    Documents it only saw unchanged keep their older versions (the sightings of this batch go)."""
    with tx() as conn:
        b = _batch(conn, batch_id, for_update=True)
        docs = [r["document_id"] for r in conn.execute("SELECT DISTINCT document_id FROM versions WHERE batch_id = %s", (batch_id,))]
        shas = [r["sha256"] for r in conn.execute("SELECT DISTINCT f.sha256 FROM files f JOIN versions v ON v.id = f.version_id WHERE v.batch_id = %s", (batch_id,))]
        conn.execute("DELETE FROM batches WHERE id = %s", (batch_id,))  # cascades: versions, sightings, chunks, courses …
        emptied = conn.execute("DELETE FROM documents d WHERE d.id = ANY(%s) AND NOT EXISTS (SELECT 1 FROM versions v WHERE v.document_id = d.id)",
                               (docs,)).rowcount
        conn.execute("DELETE FROM texts t WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.text_id = t.id)")
        orphans = [sha for sha in shas if not conn.execute("SELECT 1 FROM files WHERE sha256 = %s LIMIT 1", (sha,)).fetchone()]
        _audit(conn, request, "batch.purge", str(batch_id), {"label": b["label"], "documents_deleted": emptied, "reason": body.reason})
    for sha in orphans:
        (settings().files_dir / sha[:2] / sha).unlink(missing_ok=True)
    return {"purged": batch_id, "documents_deleted": emptied, "files_deleted": len(orphans)}


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: int, request: Request):
    with tx() as conn:
        n = conn.execute("UPDATE jobs SET status = 'queued', attempts = 0, error = NULL, run_after = now() WHERE id = %s AND status = 'failed'",
                         (job_id,)).rowcount
        _audit(conn, request, "job.retry", str(job_id))
    if not n:
        raise HTTPException(409, f"job {job_id} is not failed")
    return {"queued": job_id}
