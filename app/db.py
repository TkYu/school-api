"""Database access: one connection pool; plain SQL (search queries are hand-tuned)."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def open_pool(url: str) -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(url, min_size=1, max_size=8, kwargs={"row_factory": dict_row}, open=True)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def tx() -> Iterator[psycopg.Connection]:
    """A connection in one transaction (committed on success, rolled back on error)."""
    assert _pool is not None, "pool not open"
    with _pool.connection() as conn:
        with conn.transaction():
            yield conn
