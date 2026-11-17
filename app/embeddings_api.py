"""Vectors computed outside (a machine with a GPU, for large imports): fetch the texts that have none, send vectors
back. The vectors must come from the very model and revision the server uses for queries (else document and query
vectors would not match), so both are checked, and every vector must be 768 numbers of length 1."""
from __future__ import annotations

import math

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .db import tx
from .embed import DIM, RURI, RURI_REVISION, literal

router = APIRouter(prefix="/v1/admin/embeddings")


@router.get("/pending")
def pending(after_id: int = 0, limit: int = 256):
    """Texts used by a chunk that have no vector yet, in id order (page with after_id = the last id you got)."""
    limit = max(1, min(limit, 1000))
    with tx() as conn:
        rows = conn.execute(
            """SELECT t.id AS text_id, t.search_text FROM texts t
               WHERE t.id > %s AND NOT EXISTS (SELECT 1 FROM embeddings_ruri_v3 e WHERE e.text_id = t.id)
                 AND EXISTS (SELECT 1 FROM chunks c WHERE c.text_id = t.id)
               ORDER BY t.id LIMIT %s""", (after_id, limit)).fetchall()
        left = conn.execute("""SELECT count(*) AS n FROM texts t WHERE NOT EXISTS (SELECT 1 FROM embeddings_ruri_v3 e WHERE e.text_id = t.id)
                               AND EXISTS (SELECT 1 FROM chunks c WHERE c.text_id = t.id)""").fetchone()["n"]
    return {"model": RURI, "revision": RURI_REVISION, "dim": DIM, "prefix": "検索文書: ", "pending": left, "texts": rows}


class Vector(BaseModel):
    text_id: int
    embedding: list[float] = Field(min_length=DIM, max_length=DIM)


class VectorsIn(BaseModel):
    model: str
    revision: str
    vectors: list[Vector] = Field(min_length=1, max_length=1000)


@router.put("")
def put_vectors(body: VectorsIn, request: Request):
    if body.model != RURI or body.revision != RURI_REVISION:
        raise HTTPException(409, f"the server searches with {RURI}@{RURI_REVISION}; vectors from {body.model}@{body.revision} would not match")
    for v in body.vectors:
        norm = math.sqrt(sum(x * x for x in v.embedding))
        if not 0.98 < norm < 1.02:
            raise HTTPException(422, f"text {v.text_id}: the vector is not normalised (length {norm:.3f}); use normalize_embeddings=True")
    with tx() as conn:
        stored = 0
        for v in body.vectors:
            r = conn.execute("INSERT INTO embeddings_ruri_v3 (text_id, embedding) SELECT id, %s::vector FROM texts WHERE id = %s "
                             "ON CONFLICT (text_id) DO NOTHING", (literal(v.embedding), v.text_id))
            stored += r.rowcount
        conn.execute("INSERT INTO audit_log (actor, action, target, detail) VALUES (%s, 'embeddings.put', %s, '{}')",
                     (getattr(request.state, "actor", "?"), f"{stored} of {len(body.vectors)}"))
    return {"stored": stored, "received": len(body.vectors)}
