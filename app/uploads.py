"""Uploads from the web UI, for every account: drop files (or a zip) into a category, see one's own submissions.

One upload = one batch owned by the uploader, labelled "nickname date time"; each file becomes a document keyed
`u<user id>/<path>` inside the category, so the same person uploading the same file name again adds a new version of
it, and two people never overwrite each other. A submitter's batch is published at once with DIRECT_PUBLISH, else it
waits for the admin's review (the admin gets a mail). The admin chooses per upload."""
from __future__ import annotations

import base64
import posixpath
from datetime import datetime

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse

from . import files as fx, ingest, mail
from .config import settings
from .db import tx
from .web import back, check_csrf, page, t

router = APIRouter(prefix="/admin/my")


def _categories(conn):
    """Categories anyone may upload into: the generic-document ones (kind upload)."""
    return conn.execute("SELECT name, coalesce(label_ja, name) AS label FROM sources WHERE kind = 'upload' ORDER BY sort_order, label_ja NULLS LAST, name").fetchall()


def _own_batches(conn, user_id: int, limit: int = 100):
    return conn.execute(
        """SELECT b.id, b.label, b.status, b.started_at, b.stats, s.name AS source, coalesce(s.label_ja, s.name) AS category,
                  (SELECT count(*) FROM versions v WHERE v.batch_id = b.id) AS files
           FROM batches b JOIN sources s ON s.id = b.source_id WHERE b.owner_id = %s ORDER BY b.started_at DESC LIMIT %s""",
        (user_id, limit)).fetchall()


@router.get("", response_class=HTMLResponse)
def my_uploads(request: Request):
    user = request.state.user
    with tx() as conn:
        cats = _categories(conn)
        batches = _own_batches(conn, user["id"])
    cfg = settings()
    return page(request, "my.html", categories=cats, batches=batches, direct=cfg.direct_publish,
                max_file=cfg.upload_max_file_mb, max_total=cfg.upload_max_total_mb)


@router.post("/upload")
def upload(request: Request, files: list[UploadFile] = File(default=[]), category: str = Form(""),
           publish: bool = Form(False), csrf: str = Form("")):
    # a plain def: FastAPI runs it in a thread, so storing a large upload does not hold up other requests
    check_csrf(request, csrf)
    user, cfg = request.state.user, settings()
    max_file, max_total = cfg.upload_max_file_mb * 1024 * 1024, cfg.upload_max_total_mb * 1024 * 1024
    with tx() as conn:
        if category not in {c["name"] for c in _categories(conn)}:
            return back(request, "/admin/my", t(request, "upload.err_category"))
    chosen = [f for f in files if f.filename]
    if not chosen:
        return back(request, "/admin/my", t(request, "upload.err_no_files"))
    # sizes first (the multipart parser spooled the files to disk; nothing big is in memory yet)
    if sum(f.size or 0 for f in chosen) > max_total:
        return back(request, "/admin/my", t(request, "upload.err_total", mb=cfg.upload_max_total_mb))
    out = fx.Expanded()
    for f in chosen:
        if (f.size or 0) > max_file:
            out.skipped.append((fx.clean_path(f.filename), "skip.too_large"))
            continue
        fx.add(out, f.filename, f.file.read(max_file + 1), max_file, max_total)
    if not out.items:
        return back(request, "/admin/my", _summary(request, 0, out.skipped))
    publish_now = publish if user["role"] == "admin" else cfg.direct_publish
    label = f"{user['nickname'] or user['email']} {datetime.now():%Y-%m-%d %H:%M}"
    try:
        b = ingest.open_batch(ingest.BatchIn(source=category, label=label, kind="partial"), request)
        for item in out.items:
            ingest.put_document(b["id"], ingest.DocumentIn(
                key=f"u{user['id']}/{item.path}", file_name=posixpath.basename(item.path), mime=item.mime,
                title=posixpath.splitext(posixpath.basename(item.path))[0],
                raw_base64=base64.b64encode(item.data).decode()), request)
        ingest.finish_batch(b["id"], ingest.FinishIn(publish=publish_now), request)
    except (HTTPException, ValueError) as e:
        return back(request, "/admin/my", t(request, "upload.err_failed", why=getattr(e, "detail", e)))
    from psycopg.types.json import Jsonb
    with tx() as conn:   # what was left out, for the submissions list
        conn.execute("UPDATE batches SET stats = stats || %s WHERE id = %s",
                     (Jsonb({"files": len(out.items), "skipped": [[p, r] for p, r in out.skipped[:200]]}), b["id"]))
    if not publish_now and user["role"] != "admin":
        _notify_admin(request, b["id"], user, category, len(out.items))
    msg = _summary(request, len(out.items), out.skipped)
    msg += " " + t(request, "upload.published" if publish_now else "upload.waiting")
    return back(request, "/admin/my", msg)


def _summary(request: Request, n: int, skipped: list[tuple[str, str]]) -> str:
    text = t(request, "upload.done", n=n)
    if skipped:
        shown = "; ".join(f"{p} ({t(request, r)})" for p, r in skipped[:10])
        more = t(request, "upload.more", n=len(skipped) - 10) if len(skipped) > 10 else ""
        text += " " + t(request, "upload.skipped", n=len(skipped), list=shown) + more
    return text


def _notify_admin(request: Request, batch_id: int, user, category: str, n: int) -> None:
    """One mail to the admin per submission waiting for review (queued; the worker sends it)."""
    from .accounts import base_url
    with tx() as conn:
        admin = conn.execute("SELECT id FROM users WHERE role = 'admin'").fetchone()
        label = conn.execute("SELECT coalesce(label_ja, name) AS l FROM sources WHERE name = %s", (category,)).fetchone()["l"]
        if admin and mail.configured():
            mail.queue(conn, "review", admin["id"], base_url(request), batch_id=batch_id, n=n, category=label,
                       who=f"{user['nickname']} <{user['email']}>")


@router.get("/batches/{batch_id}", response_class=HTMLResponse)
def my_batch(request: Request, batch_id: int):
    user = request.state.user
    with tx() as conn:
        b = conn.execute("""SELECT b.*, coalesce(s.label_ja, s.name) AS category FROM batches b JOIN sources s ON s.id = b.source_id
                            WHERE b.id = %s AND b.owner_id = %s""", (batch_id, user["id"])).fetchone()
        if not b:
            raise HTTPException(404, f"no batch {batch_id} of yours")
        docs = conn.execute(
            """SELECT d.key, v.title, f.name AS file_name, f.size, (SELECT count(*) FROM chunks c WHERE c.version_id = v.id) AS chunks,
                      v.meta->>'parse_error' AS parse_error
               FROM versions v JOIN documents d ON d.id = v.document_id LEFT JOIN files f ON f.version_id = v.id
               WHERE v.batch_id = %s ORDER BY d.key""", (batch_id,)).fetchall()
    return page(request, "my_batch.html", b=b, docs=docs)


@router.post("/batches/{batch_id}/retire")
def my_retire(request: Request, batch_id: int, csrf: str = Form("")):
    """Take one's own submission out of search (published) or withdraw it (waiting); the data stays for the admin."""
    check_csrf(request, csrf)
    with tx() as conn:
        own = conn.execute("SELECT 1 FROM batches WHERE id = %s AND owner_id = %s", (batch_id, request.state.user["id"])).fetchone()
    if not own:
        raise HTTPException(404, f"no batch {batch_id} of yours")
    try:
        ingest.retire_batch(batch_id, request)
    except HTTPException as e:
        return back(request, "/admin/my", t(request, "err.not_done", why=e.detail))
    return back(request, "/admin/my", t(request, "my.retired"))
