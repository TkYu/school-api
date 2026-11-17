"""The admin UI in three languages: every message key the templates and the code use exists in ja, zh and en, the
placeholders agree, and every page renders in each language without a missing key."""
from __future__ import annotations

import re
import string
from pathlib import Path

import pytest

from app.messages import MESSAGES
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, log_in

APP = Path(__file__).resolve().parent.parent / "app"
# keys built at run time from a prefix ('state.' ~ b.status …) and the values they take
DYNAMIC = {"state.": ("open", "staging", "published", "retired"), "state_help.": ("open", "staging", "published", "retired"),
           "kind.": ("syllabus", "site", "bbs", "upload"), "role.": ("admin", "submitter"), "jobs.": ("failed", "queued", "running", "done")}


def used_keys() -> set[str]:
    keys = set()
    for f in (APP / "templates").glob("*.html"):
        keys |= set(re.findall(r"""\bt\(\s*['"]([a-z_][a-z0-9_.]*)['"]""", f.read_text()))
    for f in APP.glob("*.py"):
        src = f.read_text()
        keys |= set(re.findall(r"""\bt\(request, ["']([a-z_][a-z0-9_.]*)["']""", src))
        keys |= set(re.findall(r"""\btr\([^,]+, ["']([a-z_][a-z0-9_.]*)["']""", src))
        keys |= set(re.findall(r"""["']((?:err|skip|docs\.state|confirm)\.[a-z0-9_]+)["']""", src))
    for prefix, values in DYNAMIC.items():
        keys |= {prefix + v for v in values}
    return {k for k in keys if not k.endswith(".")}


def test_every_used_key_exists_in_three_languages():
    missing = sorted(k for k in used_keys() if k not in MESSAGES)
    assert not missing, missing
    for key, entry in MESSAGES.items():
        assert set(entry) == {"ja", "zh", "en"} and all(entry.values()), key


def test_placeholders_agree():
    fields = lambda s: {f for _, f, _, _ in string.Formatter().parse(s) if f}   # noqa: E731
    for key, entry in MESSAGES.items():
        assert fields(entry["ja"]) == fields(entry["zh"]) == fields(entry["en"]), key


PAGES = ["/admin", "/admin/review", "/admin/documents", "/admin/my", "/admin/upload", "/admin/categories", "/admin/users",
         "/admin/search", "/admin/search?q=テスト", "/admin/jobs", "/admin/log", "/admin/settings", "/admin/password"]
PUBLIC = ["/admin/login", "/admin/signup", "/admin/forgot"]


@pytest.mark.parametrize("lang", ["ja", "zh", "en"])
def test_every_page_renders_in_each_language(client, lang, caplog):
    client.cookies.clear()
    client.get(f"/admin/lang?l={lang}")
    for path in PUBLIC:
        r = client.get(path)
        assert r.status_code == 200 and f'lang="{ {"ja": "ja", "zh": "zh-Hans", "en": "en"}[lang] }"' in r.text, path
    log_in(client, ADMIN_EMAIL, ADMIN_PASSWORD, lang=lang)
    with caplog.at_level("ERROR", logger="i18n"):
        for path in PAGES:
            assert client.get(path).status_code == 200, path
        with __import__("app.db", fromlist=["tx"]).tx() as conn:
            uid = conn.execute("SELECT id FROM users ORDER BY id DESC LIMIT 1").fetchone()["id"]
            bid = conn.execute("SELECT id FROM batches ORDER BY id DESC LIMIT 1").fetchone()
        assert client.get(f"/admin/users/{uid}").status_code == 200
        if bid:
            assert client.get(f"/admin/batches/{bid['id']}").status_code == 200
    assert not [r for r in caplog.records if "missing message key" in r.getMessage()]
    client.cookies.clear()
