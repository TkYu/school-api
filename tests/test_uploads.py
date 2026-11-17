"""Uploads from the web UI: categories (built-in vs custom, home page tabs, delete only when empty), a submitter's
upload with Office files, Japanese text files and a zip, review by the admin (with a mail), DIRECT_PUBLISH, own
submissions only, size limits."""
from __future__ import annotations

import io
import re
import zipfile

import pytest

from app import processing
from app.db import tx
from tests.conftest import sent, ADMIN_EMAIL, ADMIN_PASSWORD, csrf_of, log_in, sign_up
from tests.test_formats import make_docx, make_pptx, make_xlsx

PW = "uploader password 1"


def as_admin(client):
    assert log_in(client, ADMIN_EMAIL, ADMIN_PASSWORD).status_code == 303
    return client


def as_user(client, email):
    with tx() as conn:
        known = conn.execute("SELECT 1 FROM users WHERE email = %s", (email,)).fetchone()
    if not known:
        sign_up(client, email, PW, email.split("@")[0])
    assert log_in(client, email, PW).headers["location"] == "/admin/my"
    return client


def make_category(client, label: str, name: str, show: bool = False):
    page = client.get("/admin/categories")
    data = {"label_ja": label, "name": name, "sort_order": "50", "csrf": csrf_of(page.text)}
    if show:
        data["show_on_home"] = "true"
    return client.post("/admin/categories", data=data, follow_redirects=True)


def process_batch(batch_id: int) -> None:
    with tx() as conn:
        vids = [r["id"] for r in conn.execute("SELECT id FROM versions WHERE batch_id = %s", (batch_id,))]
    for v in vids:
        processing.process_version({"version_id": v})


def newest_batch(email: str) -> dict:
    with tx() as conn:
        return conn.execute("SELECT b.* FROM batches b JOIN users u ON u.id = b.owner_id WHERE u.email = %s ORDER BY b.id DESC LIMIT 1",
                            (email,)).fetchone()


def upload(client, category: str, files: list[tuple[str, bytes, str]], **extra):
    page = client.get("/admin/my")
    return client.post("/admin/my/upload", data={"category": category, "csrf": csrf_of(page.text), **extra},
                       files=[("files", f) for f in files], follow_redirects=True)


@pytest.fixture(scope="module")
def setup_category(client):
    as_admin(client)
    r = make_category(client, "初期化", "setup-guides", show=True)
    assert "已创建分类「初期化」" in r.text
    client.cookies.clear()
    return "setup-guides"


def test_categories_page_and_home_tabs(client, setup_category):
    as_admin(client)
    page = client.get("/admin/categories")
    assert "初期化" in page.text and "setup-guides" in page.text
    r = make_category(client, "初期化2", "setup-guides")
    assert "已存在" in r.text
    r = make_category(client, "", "")
    assert "请填写日语名称" in r.text
    make_category(client, "非表示カテゴリ", "hidden-cat", show=False)
    client.cookies.clear()
    home = client.get("/?q=テスト").text
    tabs = re.findall(r'<nav class="tabs">(.*?)</nav>', home, re.S)[0]
    assert tabs.index("すべて") < tabs.index("初期化") and "非表示カテゴリ" not in tabs
    assert client.get("/?q=テスト&t=site").status_code == 200           # links from before still work


def test_built_in_categories_cannot_be_deleted_and_full_ones_wait(client, auth, setup_category):
    client.put("/v1/admin/sources/crawled", json={"name": "crawled", "kind": "site"}, headers=auth)
    as_admin(client)
    page = client.get("/admin/categories")
    r = client.post("/admin/categories/crawled/delete", data={"csrf": csrf_of(page.text)}, follow_redirects=True)
    assert "内置分类不能删除" in r.text
    make_category(client, "一時", "temp-cat")
    b = client.post("/v1/admin/batches", json={"source": "temp-cat", "label": "t", "kind": "partial"}, headers=auth).json()["id"]
    d = client.put(f"/v1/admin/batches/{b}/documents", json={"key": "t/1", "text": "一時的な資料"}, headers=auth).json()
    client.post(f"/v1/admin/batches/{b}/finish", json={"publish": True}, headers=auth)
    r = client.post("/admin/categories/temp-cat/delete", data={"csrf": csrf_of(page.text)}, follow_redirects=True)
    assert "还有 1 份资料" in r.text
    client.post(f"/v1/admin/documents/{d['document_id']}/purge", json={"reason": "テスト"}, headers=auth)
    r = client.post("/admin/categories/temp-cat/delete", data={"csrf": csrf_of(page.text)}, follow_redirects=True)
    assert "已删除分类「一時」" in r.text
    with tx() as conn:
        assert conn.execute("SELECT 1 FROM sources WHERE name = 'temp-cat'").fetchone() is None
    client.cookies.clear()


def zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in entries.items():
            z.writestr(n, d)
    return buf.getvalue()


def test_a_submitter_uploads_and_the_admin_reviews(client, setup_category):
    email = "teacher@example.ac.jp"
    as_user(client, email)
    n_mail = len(sent())
    archive = zip_bytes({"環境/手順.txt": "手順：まず Git をインストールする。".encode("cp932"), "環境/photo.png": b"\x89PNG"})
    r = upload(client, setup_category, [("guide.docx", make_docx(), "application/octet-stream"),
                                        ("timetable.xlsx", make_xlsx(), "application/octet-stream"),
                                        ("intro.pptx", make_pptx(), "application/octet-stream"),
                                        ("notice.html", "<html><title>お知らせ</title><p>研究室の鍵は事務室で借りる</p></html>".encode(), "text/html"),
                                        ("set.zip", archive, "application/zip"), ("old.doc", b"\xd0\xcf", "application/msword")])
    assert "已导入 5 个文件" in r.text and "没有导入" in r.text and "old.doc" in r.text and "photo.png" in r.text
    assert "等待审核后发布" in r.text
    b = newest_batch(email)
    assert b["status"] == "staging" and b["stats"]["files"] == 5 and len(b["stats"]["skipped"]) == 2
    assert sent()[-1].to == ADMIN_EMAIL and len(sent()) == n_mail + 1 and f"/admin/batches/{b['id']}" in sent()[-1].body
    process_batch(b["id"])
    # not searchable before review
    assert client.get("/v1/search", params={"q": "Git インストール", "source": setup_category}).json()["results"] == []
    own = client.get(f"/admin/my/batches/{b['id']}").text
    assert "guide.docx" in own and "set/環境/手順.txt" in own
    client.cookies.clear()
    # the admin sees it in the queue and publishes it
    as_admin(client)
    queue = client.get("/admin/review")
    assert f"/admin/batches/{b['id']}" in queue.text and "teacher" in queue.text
    r = client.post(f"/admin/batches/{b['id']}/publish", data={"csrf": csrf_of(queue.text), "next": "/admin/review"}, follow_redirects=True)
    assert "已发布" in r.text and r.url.path == "/admin/review"
    client.cookies.clear()
    hits = client.get("/v1/search", params={"q": "Git インストール", "source": setup_category}).json()["results"]
    assert any("Git" in h["text"] for h in hits)
    for q in ("VS Code", "情報リテラシー", "Moodle", "研究室の鍵"):        # docx, xlsx, pptx, html
        assert client.get("/v1/search", params={"q": q, "source": setup_category}).json()["results"], q
    with tx() as conn:
        titles = {r["title"] for r in conn.execute("SELECT v.title FROM versions v WHERE v.batch_id = %s", (b["id"],))}
    assert "初期設定ガイド" not in titles and "guide" in titles                 # the file name is the title


def test_direct_publish_and_own_submissions_only(client, setup_category, setting):
    setting(direct_publish=True)
    as_user(client, "fast@example.ac.jp")
    r = upload(client, setup_category, [("memo.md", "# メモ\n研究室の場所は3号館".encode(), "text/markdown")])
    assert "已发布" in r.text
    b = newest_batch("fast@example.ac.jp")
    assert b["status"] == "published"
    # someone else's batch is not visible or retirable
    other = newest_batch("teacher@example.ac.jp")
    assert client.get(f"/admin/my/batches/{other['id']}").status_code == 404
    page = client.get("/admin/my")
    assert client.post(f"/admin/my/batches/{other['id']}/retire", data={"csrf": csrf_of(page.text)}).status_code == 404
    r = client.post(f"/admin/my/batches/{b['id']}/retire", data={"csrf": csrf_of(page.text)}, follow_redirects=True)
    assert "已下架" in r.text and newest_batch("fast@example.ac.jp")["status"] == "retired"
    client.cookies.clear()


def test_submitters_cannot_upload_into_built_in_categories(client, auth):
    client.put("/v1/admin/sources/crawled", json={"name": "crawled", "kind": "site"}, headers=auth)
    as_user(client, "teacher@example.ac.jp")
    r = upload(client, "crawled", [("a.txt", b"x", "text/plain")])
    assert "请选择分类" in r.text
    client.cookies.clear()


def test_size_limits(client, setup_category, setting):
    setting(upload_max_file_mb=1, upload_max_total_mb=2)
    as_user(client, "teacher@example.ac.jp")
    big = b"a" * (1024 * 1024 + 10)
    r = upload(client, setup_category, [("big.txt", big, "text/plain"), ("ok.txt", "小さい".encode(), "text/plain")])
    assert "已导入 1 个文件" in r.text and "big.txt" in r.text and "文件太大" in r.text
    r = upload(client, setup_category, [(f"p{i}.txt", b"b" * 900_000, "text/plain") for i in range(3)])
    assert "超过上限" in r.text
    client.cookies.clear()


def test_the_admin_sees_each_users_submissions(client):
    as_admin(client)
    users = client.get("/admin/users").text
    assert "teacher@example.ac.jp" in users and "fast@example.ac.jp" in users
    with tx() as conn:
        uid = conn.execute("SELECT id FROM users WHERE email = 'teacher@example.ac.jp'").fetchone()["id"]
    page = client.get(f"/admin/users/{uid}").text
    assert "初期化" in page
    docs = client.get("/admin/documents", params={"owner": uid}).text
    assert "u%d/guide" % uid in docs and "fast" not in docs
    client.cookies.clear()


def test_admin_upload_can_publish_at_once(client, setup_category):
    as_admin(client)
    r = upload(client, setup_category, [("admin-note.txt", "管理者のお知らせ".encode(), "text/plain")], publish="true")
    assert "已发布" in r.text and newest_batch(ADMIN_EMAIL)["status"] == "published"
    client.cookies.clear()
