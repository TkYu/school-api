"""Tests run against a real PostgreSQL with pg_search + pgvector: a fresh database per test session.
TEST_PG_ADMIN_URL points at a server where the user may create databases.

Mail never leaves the process (MAIL_BACKEND=memory: app.mail.OUTBOX). The admin account is made at the start of the
session through the real sign-up and confirmation pages, so it is the first confirmed user."""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import psycopg
import pytest

ADMIN_URL = os.environ.get("TEST_PG_ADMIN_URL", "postgresql://school:school-test@localhost/postgres")
TOKEN = "test-ingest-token-" + "x" * 20
ADMIN_EMAIL, ADMIN_PASSWORD = "admin@example.ac.jp", "correct horse battery"


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def last_link(kind: str) -> str:
    """The path of the newest mailed link of this kind (confirm / reset), after the worker's part (sending)."""
    from app import mail
    mail.run_queued()
    for m in reversed(mail.OUTBOX):
        found = re.search(rf"https://testserver(/admin/{kind}\?token=[\w-]+)", m.body)
        if found:
            return found.group(1)
    raise AssertionError(f"no {kind} mail")


def sent() -> list:
    """Every mail so far, after sending what is queued (the worker's job)."""
    from app import mail
    mail.run_queued()
    return mail.OUTBOX


def reset_throttles():
    from app import accounts
    accounts._throttles.clear()


def sign_up(c, email: str, password: str, nickname: str = "", confirm: bool = True):
    reset_throttles()
    page = c.get("/admin/signup")
    r = c.post("/admin/signup", data={"email": email, "password": password, "password2": password, "nickname": nickname,
                                      "csrf": csrf_of(page.text)}, follow_redirects=True)
    if confirm:
        assert c.get(last_link("confirm")).status_code == 200
    return r


def log_in(c, email: str, password: str, lang: str = "zh"):
    c.cookies.clear()
    c.get(f"/admin/lang?l={lang}&next=/admin/login")      # the switcher sets the cookie, as in a browser
    reset_throttles()
    page = c.get("/admin/login")
    return c.post("/admin/login", data={"email": email, "password": password, "csrf": csrf_of(page.text)}, follow_redirects=False)


@pytest.fixture(scope="session")
def db_url():
    name = f"school_t_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    yield url
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def client(db_url, tmp_path_factory):
    files = tmp_path_factory.mktemp("files")
    for k in ("ADMIN_USER", "ADMIN_PASSWORD_HASH"):
        os.environ.pop(k, None)
    os.environ.update(DATABASE_URL=db_url, INGEST_TOKEN=TOKEN, SESSION_SECRET="s" * 40, FILES_DIR=str(files), EMBEDDER="fake",
                      MAIL_BACKEND="memory", PUBLIC_URL="https://testserver", SIGNUP_ALLOWLIST="@example.ac.jp, guest@other.example",
                      DIRECT_PUBLISH="")
    from app.config import settings
    settings.cache_clear()
    from app.migrate import migrate
    migrate(db_url)
    from fastapi.testclient import TestClient
    from app.main import create_app
    with TestClient(create_app(settings()), base_url="https://testserver") as c:
        c.files_dir = Path(files)
        # a batch from before any account (an existing deployment): the first admin takes it over
        bearer = {"Authorization": f"Bearer {TOKEN}"}
        c.put("/v1/admin/sources/before-accounts", json={"name": "before-accounts", "kind": "upload"}, headers=bearer)
        c.pre_admin_batch = c.post("/v1/admin/batches", json={"source": "before-accounts", "label": "old", "kind": "partial"}, headers=bearer).json()["id"]
        sign_up(c, ADMIN_EMAIL, ADMIN_PASSWORD, "Admin")
        c.cookies.clear()
        yield c


@pytest.fixture(scope="session")
def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def admin(client):
    """The client logged in as the admin, with the Chinese UI (the texts the older tests check)."""
    r = log_in(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    assert r.status_code == 303 and r.headers["location"] == "/admin", r.text
    yield client
    client.cookies.clear()


@pytest.fixture
def setting(monkeypatch):
    """Change one setting for one test: setting(direct_publish=True)."""
    from app.config import settings

    def change(**kw):
        cfg = settings()
        for k, v in kw.items():
            monkeypatch.setattr(cfg, k, v)
    return change
