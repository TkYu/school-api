"""sql/006_accounts.sql on a database that already holds data (an existing deployment): nothing is lost, the old home
page tabs become categories, and the first admin takes over what was there."""
from __future__ import annotations

import uuid

import psycopg
from psycopg.rows import dict_row

from app.migrate import SQL_DIR
from tests.conftest import ADMIN_URL


def test_006_on_a_database_with_data():
    name = f"school_m_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        url = ADMIN_URL.rsplit("/", 1)[0] + "/" + name
        with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
            for f in sorted(SQL_DIR.glob("00[1-5]_*.sql")):
                conn.execute(f.read_text(encoding="utf-8"))
            for n, kind in (("shonan-it-site", "site"), ("shonan-it-syllabus", "syllabus"), ("sitoss-bbs", "bbs"), ("notes", "upload")):
                conn.execute("INSERT INTO sources (name, kind) VALUES (%s, %s)", (n, kind))
            sid = conn.execute("SELECT id FROM sources WHERE name = 'notes'").fetchone()["id"]
            bid = conn.execute("INSERT INTO batches (source_id, label, kind, status) VALUES (%s, 'old', 'partial', 'published') RETURNING id",
                               (sid,)).fetchone()["id"]
            conn.execute("INSERT INTO audit_log (actor, action, target) VALUES ('admin', 'batch.open', %s)", (str(bid),))

            conn.execute((SQL_DIR / "006_accounts.sql").read_text(encoding="utf-8"))

            rows = {r["name"]: r for r in conn.execute("SELECT name, label_ja, show_on_home, sort_order FROM sources")}
            assert [rows[n]["label_ja"] for n in ("shonan-it-site", "shonan-it-syllabus", "sitoss-bbs")] == ["公式サイト", "シラバス", "掲示板"]
            assert all(rows[n]["show_on_home"] for n in ("shonan-it-site", "shonan-it-syllabus", "sitoss-bbs"))
            assert rows["notes"]["show_on_home"] is False and rows["notes"]["label_ja"] is None
            b = conn.execute("SELECT owner_id, status, label FROM batches WHERE id = %s", (bid,)).fetchone()
            assert b == {"owner_id": None, "status": "published", "label": "old"}
            assert conn.execute("SELECT actor, user_id FROM audit_log").fetchone() == {"actor": "admin", "user_id": None}
            # the unique index allows one admin only
            conn.execute("INSERT INTO users (email, password_hash, role, confirmed_at) VALUES ('a@example.ac.jp', 'h', 'admin', now())")
            try:
                conn.execute("INSERT INTO users (email, password_hash, role, confirmed_at) VALUES ('b@example.ac.jp', 'h', 'admin', now())")
                raise AssertionError("a second admin was accepted")
            except psycopg.errors.UniqueViolation:
                pass
            try:
                conn.execute("INSERT INTO users (email, password_hash) VALUES ('A@EXAMPLE.ac.jp', 'h')")
                raise AssertionError("the same address in other case was accepted")
            except psycopg.errors.UniqueViolation:
                pass
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def test_the_first_admin_takes_over_batches_from_before_accounts(client):
    from app.db import tx
    with tx() as conn:
        row = conn.execute("SELECT u.email FROM batches b JOIN users u ON u.id = b.owner_id WHERE b.id = %s", (client.pre_admin_batch,)).fetchone()
    assert row["email"] == "admin@example.ac.jp"      # made with the ingest token before anyone had signed up
