"""Public reads of one document: its current version (or the one valid at `as_of`) and its history."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from .db import tx

router = APIRouter(prefix="/v1")

_FIELDS = "v.id AS version_id, v.document_id, v.title, v.url, v.lang, v.info_date, v.mime, v.body, v.meta, v.first_seen_at"


@router.get("/documents/{document_id}")
def get_document(document_id: int, as_of: datetime | None = None):
    """The current version; with `as_of`, the published version that was current at that time."""
    with tx() as conn:
        status = conn.execute("SELECT gone, hidden FROM document_status WHERE document_id = %s", (document_id,)).fetchone()
        if not status or status["hidden"]:  # hidden documents are not served (the admin UI still shows them)
            raise HTTPException(404, f"no document {document_id}")
        if as_of is None:
            row = conn.execute(f"SELECT {_FIELDS} FROM current_versions v WHERE v.document_id = %s", (document_id,)).fetchone()
        else:
            row = conn.execute(f"SELECT {_FIELDS} FROM published_versions v WHERE v.document_id = %s AND v.first_seen_at <= %s "
                               "ORDER BY v.first_seen_at DESC, v.id DESC LIMIT 1", (document_id, as_of)).fetchone()
        if not row:
            raise HTTPException(404, f"document {document_id} has no published version" + (f" before {as_of}" if as_of else ""))
        superseded = conn.execute("SELECT min(first_seen_at) AS at FROM published_versions WHERE document_id = %s AND first_seen_at > %s",
                                  (document_id, row["first_seen_at"])).fetchone()["at"]
    return {**row, "gone": status["gone"], "superseded_at": superseded}


def _visible(conn, document_id: int) -> None:
    row = conn.execute("SELECT hidden FROM document_status WHERE document_id = %s", (document_id,)).fetchone()
    if not row or row["hidden"]:
        raise HTTPException(404, f"no document {document_id}")


@router.get("/documents/{document_id}/versions")
def list_versions(document_id: int):
    """Every published version, newest first, with when it was first and last seen."""
    with tx() as conn:
        _visible(conn, document_id)
        rows = conn.execute(
            """SELECT v.id AS version_id, v.title, v.url, v.info_date, v.first_seen_at,
                      (SELECT max(s.seen_at) FROM sightings s WHERE s.version_id = v.id) AS last_seen_at,
                      length(v.body) AS chars
               FROM published_versions v WHERE v.document_id = %s ORDER BY v.first_seen_at DESC, v.id DESC""",
            (document_id,)).fetchall()
    if not rows:
        raise HTTPException(404, f"document {document_id} has no published version")
    return rows


@router.get("/documents/{document_id}/versions/{version_id}")
def get_version(document_id: int, version_id: int):
    with tx() as conn:
        _visible(conn, document_id)
        row = conn.execute(f"SELECT {_FIELDS} FROM published_versions v WHERE v.document_id = %s AND v.id = %s",
                           (document_id, version_id)).fetchone()
    if not row:
        raise HTTPException(404, f"no published version {version_id} of document {document_id}")
    return row
