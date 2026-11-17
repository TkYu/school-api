"""Applies sql/NNN_*.sql in order, once each (recorded in schema_migrations)."""
from __future__ import annotations

from pathlib import Path

import psycopg

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def migrate(url: str) -> list[str]:
    applied: list[str] = []
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, at timestamptz NOT NULL DEFAULT now())")
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        for file in sorted(SQL_DIR.glob("[0-9][0-9][0-9]_*.sql")):
            if file.name in done:
                continue
            with conn.transaction():
                conn.execute(file.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (file.name,))
            applied.append(file.name)
    return applied
