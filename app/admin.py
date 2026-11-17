"""Admin web UI (server-rendered forms, no front-end build, no third-party scripts). Behind the default-deny
middleware: every page here is the admin's (a submitter only reaches /admin/my and the account pages). Every POST
carries a CSRF token from the session. Actions call the same functions as the ingest API, so the rules (versions,
staging, audit) are the same."""
from __future__ import annotations

import base64
import difflib
import mimetypes
import re
import secrets
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from psycopg.types.json import Jsonb

from . import ingest, search, users
from .db import tx
from .web import back, check_csrf, page, safe_next, t

router = APIRouter(prefix="/admin")


def _audit(conn, request: Request, action: str, target: str, detail: dict | None = None) -> None:
    conn.execute("INSERT INTO audit_log (actor, user_id, action, target, detail) VALUES (%s, %s, %s, %s, %s)",
                 (request.state.actor, request.state.user_id, action, target, Jsonb(detail or {})))


# ---------------------------------------------------------------- overview
@router.get("", response_class=HTMLResponse)
def overview(request: Request):
    if request.state.user["role"] != "admin":
        return RedirectResponse("/admin/my", status_code=303)
    with tx() as conn:
        sources = conn.execute(
            """SELECT s.name, coalesce(s.label_ja, s.name) AS label, s.kind, count(DISTINCT d.id) AS documents,
                      count(DISTINCT d.id) FILTER (WHERE ds.current_version_id IS NOT NULL AND NOT ds.gone AND NOT ds.hidden) AS live
               FROM sources s LEFT JOIN documents d ON d.source_id = s.id LEFT JOIN document_status ds ON ds.document_id = d.id
               GROUP BY s.id ORDER BY s.sort_order, s.name""").fetchall()
        batches = conn.execute(
            """SELECT b.id, s.name AS source, b.label, b.kind, b.status, b.started_at, b.stats, u.nickname AS owner
               FROM batches b JOIN sources s ON s.id = b.source_id LEFT JOIN users u ON u.id = b.owner_id
               ORDER BY b.started_at DESC LIMIT 20""").fetchall()
        jobs = {r["status"]: r["n"] for r in conn.execute("SELECT status, count(*) AS n FROM jobs GROUP BY status")}
        failed = conn.execute("SELECT id, kind, payload, attempts, left(error, 400) AS error FROM jobs WHERE status = 'failed' ORDER BY id DESC LIMIT 10").fetchall()
        audit = conn.execute("SELECT at, actor, action, target, detail FROM audit_log ORDER BY id DESC LIMIT 15").fetchall()
    return page(request, "overview.html", sources=sources, batches=batches, jobs=jobs, failed=failed, audit=audit)


# ---------------------------------------------------------------- review queue
@router.get("/review", response_class=HTMLResponse)
def review(request: Request):
    with tx() as conn:
        rows = conn.execute(
            """SELECT b.id, b.label, b.started_at, b.stats, s.name AS source, coalesce(s.label_ja, s.name) AS category,
                      u.id AS owner_id, u.email, u.nickname, (SELECT count(*) FROM versions v WHERE v.batch_id = b.id) AS files
               FROM batches b JOIN sources s ON s.id = b.source_id LEFT JOIN users u ON u.id = b.owner_id
               WHERE b.status = 'staging' ORDER BY b.started_at""").fetchall()
    return page(request, "review.html", rows=rows)


# ---------------------------------------------------------------- batches
@router.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_page(request: Request, batch_id: int):
    with tx() as conn:
        b = conn.execute("SELECT b.*, s.name AS source, u.email AS owner_email, u.nickname AS owner_nickname FROM batches b "
                         "JOIN sources s ON s.id = b.source_id LEFT JOIN users u ON u.id = b.owner_id WHERE b.id = %s", (batch_id,)).fetchone()
        if not b:
            raise HTTPException(404, f"no batch {batch_id}")
        docs = conn.execute(
            """SELECT d.id AS document_id, d.key, v.id AS version_id, v.title, v.url, (v.batch_id = %(b)s) AS new_version,
                      (SELECT count(*) FROM chunks c WHERE c.version_id = v.id) AS chunks,
                      co.code, co.eval_sum, co.notes, v.meta->>'parse_error' AS parse_error
               FROM sightings si JOIN versions v ON v.id = si.version_id JOIN documents d ON d.id = v.document_id
               LEFT JOIN courses co ON co.version_id = v.id
               WHERE si.batch_id = %(b)s ORDER BY d.key LIMIT 2000""", {"b": batch_id}).fetchall()
        pending = conn.execute("SELECT count(*) AS n FROM jobs WHERE status IN ('queued','running') AND (payload->>'version_id')::bigint IN "
                               "(SELECT id FROM versions WHERE batch_id = %s)", (batch_id,)).fetchone()["n"]
        no_vector = conn.execute(
            """SELECT count(DISTINCT c.text_id) AS n FROM chunks c JOIN versions v ON v.id = c.version_id
               LEFT JOIN embeddings_ruri_v3 e ON e.text_id = c.text_id WHERE v.batch_id = %s AND e.text_id IS NULL""", (batch_id,)).fetchone()["n"]
    return page(request, "batch.html", b=b, docs=docs, pending=pending, no_vector=no_vector)


@router.post("/batches/{batch_id}/publish")
def batch_publish(request: Request, batch_id: int, csrf: str = Form(""), next: str = Form("")):
    return batch_action(request, batch_id, "publish", csrf, next)


@router.post("/batches/{batch_id}/retire")
def batch_retire(request: Request, batch_id: int, csrf: str = Form(""), next: str = Form("")):
    return batch_action(request, batch_id, "retire", csrf, next)


def batch_action(request: Request, batch_id: int, action: str, csrf: str, next: str = ""):
    check_csrf(request, csrf)
    to = safe_next(next, f"/admin/batches/{batch_id}")
    try:
        (ingest.publish_batch if action == "publish" else ingest.retire_batch)(batch_id, request)
    except HTTPException as e:
        return back(request, to, t(request, "err.not_done", why=e.detail))
    return back(request, to, t(request, "batch.published" if action == "publish" else "batch.retired"))


# ---------------------------------------------------------------- advanced upload (any category, technical options)
@router.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request):
    with tx() as conn:
        sources = conn.execute("SELECT name, kind FROM sources ORDER BY name").fetchall()
    return page(request, "upload.html", sources=sources)


@router.post("/upload")
def upload(request: Request, files: list[UploadFile] = File(default=[]), source: str = Form(...), kind: str = Form("upload"),
           label: str = Form(...), key_prefix: str = Form(""), url_template: str = Form(""),
           full: bool = Form(False), publish: bool = Form(False), csrf: str = Form("")):
    check_csrf(request, csrf)
    # a plain def: FastAPI runs it in a thread, so storing a large upload does not hold up other requests
    contents = [(f.filename or "file", f.file.read(), f.content_type) for f in files if f.filename]
    if not contents:
        return back(request, "/admin/upload", t(request, "upload.err_no_files"))
    try:
        ingest.put_source(source, ingest.SourceIn(name=source, kind=kind), request)
        b = ingest.open_batch(ingest.BatchIn(source=source, label=label, kind="full" if full else "partial"), request)
        for name, data, ctype in contents:
            stem = Path(name).stem
            ingest.put_document(b["id"], ingest.DocumentIn(
                key=key_prefix + stem, file_name=name, mime=ctype or mimetypes.guess_type(name)[0] or "application/octet-stream",
                raw_base64=base64.b64encode(data).decode(), url=url_template.format(stem=stem, name=name) if url_template else None), request)
        ingest.finish_batch(b["id"], ingest.FinishIn(publish=publish), request)
    except (HTTPException, ValueError, KeyError) as e:
        return back(request, "/admin/upload", t(request, "err.not_done", why=getattr(e, "detail", e)))
    return back(request, f"/admin/batches/{b['id']}", t(request, "upload.imported_published" if publish else "upload.imported_waiting", n=len(contents)))


# ---------------------------------------------------------------- documents
@router.get("/documents/{document_id}", response_class=HTMLResponse)
def document_page(request: Request, document_id: int):
    with tx() as conn:
        d = conn.execute("SELECT d.*, s.name AS source, ds.gone, ds.current_version_id FROM documents d JOIN sources s ON s.id = d.source_id "
                         "JOIN document_status ds ON ds.document_id = d.id WHERE d.id = %s", (document_id,)).fetchone()
        if not d:
            raise HTTPException(404, f"no document {document_id}")
        versions = conn.execute(
            """SELECT v.id, v.title, v.url, v.first_seen_at, b.id AS batch_id, b.label, b.status,
                      (SELECT max(seen_at) FROM sightings WHERE version_id = v.id) AS last_seen
               FROM versions v JOIN batches b ON b.id = v.batch_id WHERE v.document_id = %s ORDER BY v.first_seen_at DESC, v.id DESC""",
            (document_id,)).fetchall()
        show = d["current_version_id"] or (versions[0]["id"] if versions else None)
        chunks = conn.execute("SELECT c.ord, c.section, t.text, (e.text_id IS NOT NULL) AS embedded FROM chunks c JOIN texts t ON t.id = c.text_id "
                              "LEFT JOIN embeddings_ruri_v3 e ON e.text_id = t.id WHERE c.version_id = %s ORDER BY c.ord", (show,)).fetchall() if show else []
    return page(request, "document.html", d=d, versions=versions, chunks=chunks, shown=show)


@router.post("/documents/{document_id}/purge")
def purge(request: Request, document_id: int, reason: str = Form(""), confirm: str = Form(""), csrf: str = Form("")):
    check_csrf(request, csrf)
    if confirm.strip() != str(document_id):
        return back(request, f"/admin/documents/{document_id}", t(request, "doc.purge_confirm_wrong"))
    try:
        ingest.purge_document(document_id, ingest.PurgeIn(reason=reason), request)
    except (HTTPException, ValueError) as e:
        return back(request, f"/admin/documents/{document_id}", t(request, "err.not_done", why=getattr(e, "detail", e)))
    return back(request, "/admin", t(request, "doc.purged", id=document_id))


# ---------------------------------------------------------------- try a search
@router.get("/search", response_class=HTMLResponse)
def try_search(request: Request, q: str = "", source: str = ""):
    result = search.run(q, source=source or None, limit=15) if q.strip() else None
    return page(request, "search.html", q=q, source=source, result=result)


# ---------------------------------------------------------------- document list, bulk actions
STATES = {
    "": ("docs.state_all", "true"),
    "live": ("docs.state_live", "ds.current_version_id IS NOT NULL AND NOT ds.gone AND NOT ds.hidden"),
    "hidden": ("docs.state_hidden", "ds.hidden"),
    "gone": ("docs.state_gone", "ds.gone"),
    "unpublished": ("docs.state_unpublished", "ds.current_version_id IS NULL"),
    "issues": ("docs.state_issues", "(co.notes ? 'eval_sum_not_100' OR co.notes ? 'no_evaluation_table' OR co.notes ? 'evaluation_from_positions')"),
}
PAGE = 50


@router.get("/documents", response_class=HTMLResponse)
def documents_page(request: Request, source: str = "", q: str = "", state: str = "", owner: int | None = None,
                   pg: int = Query(1, alias="page")):
    pg = max(pg, 1)
    where = STATES.get(state, STATES[""])[1]
    params = {"source": source or None, "q": f"%{q.strip()}%" if q.strip() else None, "off": (pg - 1) * PAGE, "n": PAGE, "owner": owner}
    base = f"""FROM documents d JOIN sources s ON s.id = d.source_id JOIN document_status ds ON ds.document_id = d.id
               LEFT JOIN LATERAL (SELECT v.*, b.owner_id FROM versions v JOIN batches b ON b.id = v.batch_id WHERE v.document_id = d.id
                                  ORDER BY v.first_seen_at DESC, v.id DESC LIMIT 1) nv ON true
               LEFT JOIN courses co ON co.version_id = nv.id
               WHERE (%(source)s::text IS NULL OR s.name = %(source)s)
                 AND (%(q)s::text IS NULL OR d.key ILIKE %(q)s OR nv.title ILIKE %(q)s OR co.code ILIKE %(q)s)
                 AND (%(owner)s::bigint IS NULL OR nv.owner_id = %(owner)s)
                 AND {where}"""
    with tx() as conn:
        total = conn.execute(f"SELECT count(*) AS n {base}", params).fetchone()["n"]
        rows = conn.execute(
            f"""SELECT d.id, d.key, d.last_seen_at, d.hidden_reason, s.name AS source, nv.title, nv.url, ds.gone, ds.hidden,
                       ds.current_version_id, co.eval_sum, co.notes,
                       (SELECT count(*) FROM versions v WHERE v.document_id = d.id) AS versions
                {base} ORDER BY d.key LIMIT %(n)s OFFSET %(off)s""", params).fetchall()
        sources = conn.execute("SELECT name FROM sources ORDER BY name").fetchall()
    return page(request, "documents.html", rows=rows, total=total, page=pg, pages=max(1, -(-total // PAGE)),
                source=source, q=q, state=state, states=STATES, sources=sources, owner=owner)


@router.post("/documents/bulk")
def documents_bulk(request: Request, action: str = Form(...), ids: list[int] = Form(default=[]), reason: str = Form(""),
                   confirm: str = Form(""), back_to: str = Form("/admin/documents", alias="back"), csrf: str = Form("")):
    check_csrf(request, csrf)
    to = safe_next(back_to, "/admin/documents")
    if not ids:
        return back(request, to, t(request, "docs.none_selected"))
    if action in ("hide", "purge") and len(reason.strip()) < 3:
        return back(request, to, t(request, "docs.reason_needed"))
    if action == "hide":
        n = ingest.hide_documents(ids, reason.strip(), request)
        return back(request, to, t(request, "docs.hidden_n", n=n))
    if action == "show":
        return back(request, to, t(request, "docs.shown_n", n=ingest.show_documents(ids, request)))
    if action == "reprocess":
        n = ingest.reprocess_versions(ingest.newest_versions(ids), request, f"{len(ids)} documents")
        return back(request, to, t(request, "docs.reprocess_n", n=n))
    if action == "purge":
        phrase = t(request, "docs.purge_phrase", n=len(ids))
        if confirm.strip() != phrase:
            return back(request, to, t(request, "docs.purge_type", phrase=phrase))
        for i in ids:
            ingest.purge_document(i, ingest.PurgeIn(reason=reason.strip()), request)
        return back(request, to, t(request, "docs.purged_n", n=len(ids)))
    raise HTTPException(404, "unknown action")


# ---------------------------------------------------------------- one version, and what changed
@router.get("/documents/{document_id}/versions/{version_id}", response_class=HTMLResponse)
def version_page(request: Request, document_id: int, version_id: int):
    with tx() as conn:
        v = conn.execute("SELECT v.*, b.label, b.status FROM versions v JOIN batches b ON b.id = v.batch_id WHERE v.id = %s AND v.document_id = %s",
                         (version_id, document_id)).fetchone()
        if not v:
            raise HTTPException(404, f"no version {version_id} of document {document_id}")
        prev = conn.execute("SELECT id, body, first_seen_at FROM versions WHERE document_id = %s AND (first_seen_at, id) < (%s, %s) "
                            "ORDER BY first_seen_at DESC, id DESC LIMIT 1", (document_id, v["first_seen_at"], version_id)).fetchone()
        chunks = conn.execute("SELECT c.ord, c.section, t.text FROM chunks c JOIN texts t ON t.id = c.text_id WHERE c.version_id = %s ORDER BY c.ord",
                              (version_id,)).fetchall()
        events = conn.execute("SELECT * FROM calendar_events WHERE version_id = %s ORDER BY ord", (version_id,)).fetchall()
    diff = None
    if prev:
        diff = list(difflib.unified_diff((prev["body"] or "").splitlines(), (v["body"] or "").splitlines(),
                                         f"v{prev['id']}", f"v{version_id}", lineterm="", n=2))
    return page(request, "version.html", v=v, prev=prev, diff=diff, chunks=chunks, events=events, document_id=document_id)


@router.post("/documents/{document_id}/{action}")
def document_action(request: Request, document_id: int, action: str, reason: str = Form(""), csrf: str = Form("")):
    check_csrf(request, csrf)
    to = f"/admin/documents/{document_id}"
    if action == "hide":
        if len(reason.strip()) < 3:
            return back(request, to, t(request, "docs.reason_needed"))
        ingest.hide_documents([document_id], reason.strip(), request)
        return back(request, to, t(request, "doc.hidden"))
    if action == "show":
        ingest.show_documents([document_id], request)
        return back(request, to, t(request, "doc.shown"))
    if action == "reprocess":
        ingest.reprocess_versions(ingest.newest_versions([document_id]), request, f"document {document_id}")
        return back(request, to, t(request, "doc.reprocess_queued"))
    raise HTTPException(404, "unknown action")


# ---------------------------------------------------------------- batch: process again, delete it
@router.post("/batches/{batch_id}/reprocess-all")
def batch_reprocess(request: Request, batch_id: int, csrf: str = Form("")):
    check_csrf(request, csrf)
    n = ingest.api_reprocess_batch(batch_id, request)["queued"]
    return back(request, f"/admin/batches/{batch_id}", t(request, "docs.reprocess_n", n=n))


@router.post("/batches/{batch_id}/purge-batch")
def batch_purge(request: Request, batch_id: int, reason: str = Form(""), confirm: str = Form(""), csrf: str = Form("")):
    check_csrf(request, csrf)
    phrase = t(request, "batch.purge_phrase", id=batch_id)
    if confirm.strip() != phrase:
        return back(request, f"/admin/batches/{batch_id}", t(request, "docs.purge_type", phrase=phrase))
    try:
        r = ingest.purge_batch(batch_id, ingest.PurgeIn(reason=reason.strip()), request)
    except (HTTPException, ValueError) as e:
        return back(request, f"/admin/batches/{batch_id}", t(request, "err.not_done", why=getattr(e, "detail", e)))
    return back(request, "/admin", t(request, "batch.purged", id=batch_id, n=r["documents_deleted"]))


# ---------------------------------------------------------------- jobs, audit log
@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, status: str = "failed"):
    with tx() as conn:
        counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, count(*) AS n FROM jobs GROUP BY status")}
        rows = conn.execute("SELECT id, kind, payload, status, attempts, left(error, 1500) AS error, run_after, created_at FROM jobs "
                            "WHERE status = %s ORDER BY id DESC LIMIT 200", (status,)).fetchall()
    return page(request, "jobs.html", rows=rows, counts=counts, status=status)


@router.post("/jobs/{job_id}/{action}")
def job_action(request: Request, job_id: int, action: str, csrf: str = Form("")):
    check_csrf(request, csrf)
    if action == "retry":
        try:
            ingest.retry_job(job_id, request)
        except HTTPException as e:
            return back(request, "/admin/jobs", t(request, "err.not_done", why=e.detail))
        return back(request, "/admin/jobs", t(request, "jobs.requeued", id=job_id))
    if action == "delete":
        with tx() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s AND status IN ('failed', 'done')", (job_id,))
        return back(request, "/admin/jobs", t(request, "jobs.deleted", id=job_id))
    raise HTTPException(404, "unknown action")


@router.get("/log", response_class=HTMLResponse)
def log_page(request: Request, action: str = "", actor: str = "", pg: int = Query(1, alias="page")):
    pg = max(pg, 1)
    params = {"a": f"{action}%" if action else None, "u": actor or None, "off": (pg - 1) * 100}
    with tx() as conn:
        rows = conn.execute("""SELECT at, actor, action, target, detail FROM audit_log
                               WHERE (%(a)s::text IS NULL OR action LIKE %(a)s) AND (%(u)s::text IS NULL OR actor = %(u)s)
                               ORDER BY id DESC LIMIT 100 OFFSET %(off)s""", params).fetchall()
        actions = conn.execute("SELECT DISTINCT split_part(action, '.', 1) AS a FROM audit_log ORDER BY 1").fetchall()
    return page(request, "log.html", rows=rows, action=action, actor=actor, page=pg, actions=actions)


# ---------------------------------------------------------------- users
@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request):
    with tx() as conn:
        rows = conn.execute(
            """SELECT u.id, u.email, u.nickname, u.role, u.confirmed_at, u.disabled_at, u.last_login_at, u.created_at,
                      count(b.id) AS batches, count(b.id) FILTER (WHERE b.status = 'staging') AS waiting
               FROM users u LEFT JOIN batches b ON b.owner_id = u.id GROUP BY u.id ORDER BY u.role, u.created_at""").fetchall()
    return page(request, "users.html", rows=rows)


@router.get("/users/{user_id}", response_class=HTMLResponse)
def user_page(request: Request, user_id: int):
    with tx() as conn:
        u = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
        if not u:
            raise HTTPException(404, f"no user {user_id}")
        batches = conn.execute(
            """SELECT b.id, b.label, b.status, b.started_at, b.stats, coalesce(s.label_ja, s.name) AS category,
                      (SELECT count(*) FROM versions v WHERE v.batch_id = b.id) AS files
               FROM batches b JOIN sources s ON s.id = b.source_id WHERE b.owner_id = %s ORDER BY b.started_at DESC LIMIT 300""",
            (user_id,)).fetchall()
    return page(request, "user.html", u=u, batches=batches, new_password=request.session.pop("new_password", None))


@router.post("/users/{user_id}/{action}")
def user_action(request: Request, user_id: int, action: str, csrf: str = Form("")):
    check_csrf(request, csrf)
    to = f"/admin/users/{user_id}"
    try:
        if action in ("disable", "enable"):
            u = users.set_disabled(user_id, action == "disable")
            with tx() as conn:
                _audit(conn, request, f"user.{action}", str(user_id), {"email": u["email"]})
            return back(request, to, t(request, "user.disabled" if action == "disable" else "user.enabled"))
        if action == "reset-password":
            u, password = users.admin_reset_password(user_id)
            with tx() as conn:
                _audit(conn, request, "user.reset_password", str(user_id), {"email": u["email"]})
            request.session["new_password"] = password      # shown once on the next page, then gone
            return back(request, to, t(request, "user.password_reset"))
    except users.AccountError as e:
        return back(request, to, t(request, str(e)))
    raise HTTPException(404, "unknown action")


# ---------------------------------------------------------------- categories
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


@router.get("/categories", response_class=HTMLResponse)
def categories_page(request: Request):
    with tx() as conn:
        rows = conn.execute(
            """SELECT s.name, s.label_ja, s.kind, s.show_on_home, s.sort_order, (s.kind <> 'upload') AS builtin,
                      (SELECT count(*) FROM documents d WHERE d.source_id = s.id) AS documents
               FROM sources s ORDER BY s.sort_order, s.name""").fetchall()
    return page(request, "categories.html", rows=rows)


@router.post("/categories")
def category_create(request: Request, label_ja: str = Form(""), name: str = Form(""), show_on_home: bool = Form(False),
                    sort_order: int = Form(100), csrf: str = Form("")):
    check_csrf(request, csrf)
    label = label_ja.strip()[:40]
    if not label:
        return back(request, "/admin/categories", t(request, "cat.err_label"))
    name = name.strip().lower() or f"cat-{secrets.token_hex(3)}"
    if not NAME.match(name):
        return back(request, "/admin/categories", t(request, "cat.err_name"))
    with tx() as conn:
        if conn.execute("SELECT 1 FROM sources WHERE name = %s", (name,)).fetchone():
            return back(request, "/admin/categories", t(request, "cat.err_exists", name=name))
        conn.execute("INSERT INTO sources (kind, name, label_ja, show_on_home, sort_order) VALUES ('upload', %s, %s, %s, %s)",
                     (name, label, show_on_home, sort_order))
        _audit(conn, request, "category.create", name, {"label_ja": label})
    return back(request, "/admin/categories", t(request, "cat.created", label=label))


@router.post("/categories/{name}")
def category_update(request: Request, name: str, label_ja: str = Form(""), show_on_home: bool = Form(False),
                    sort_order: int = Form(100), csrf: str = Form("")):
    check_csrf(request, csrf)
    with tx() as conn:
        n = conn.execute("UPDATE sources SET label_ja = %s, show_on_home = %s, sort_order = %s WHERE name = %s",
                         (label_ja.strip()[:40] or None, show_on_home, sort_order, name)).rowcount
        if not n:
            raise HTTPException(404, f"no category {name}")
        _audit(conn, request, "category.update", name, {"label_ja": label_ja.strip(), "show_on_home": show_on_home, "sort_order": sort_order})
    return back(request, "/admin/categories", t(request, "cat.saved"))


@router.post("/categories/{name}/delete")
def category_delete(request: Request, name: str, csrf: str = Form("")):
    """Only a generic-document category, and only once every document in it is purged (its empty batches go too)."""
    check_csrf(request, csrf)
    with tx() as conn:
        s = conn.execute("SELECT id, kind, label_ja FROM sources WHERE name = %s FOR UPDATE", (name,)).fetchone()
        if not s:
            raise HTTPException(404, f"no category {name}")
        if s["kind"] != "upload":
            return back(request, "/admin/categories", t(request, "cat.err_builtin"))
        n = conn.execute("SELECT count(*) AS n FROM documents WHERE source_id = %s", (s["id"],)).fetchone()["n"]
        if n:
            return back(request, "/admin/categories", t(request, "cat.err_not_empty", n=n))
        conn.execute("DELETE FROM sources WHERE id = %s", (s["id"],))
        _audit(conn, request, "category.delete", name, {"label_ja": s["label_ja"]})
    return back(request, "/admin/categories", t(request, "cat.deleted", label=s["label_ja"] or name))
