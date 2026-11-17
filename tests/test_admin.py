"""Admin web UI: login (wrong password, lockout), pages behind the session, CSRF on every form, upload and publish."""
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, csrf_of, log_in, reset_throttles


def test_pages_need_a_login(client):
    client.cookies.clear()
    for path in ("/admin", "/admin/upload", "/admin/search", "/admin/batches/1"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/admin/login", path


def test_wrong_password_and_missing_csrf(client):
    client.cookies.clear()
    page = client.get("/admin/login")
    r = client.post("/admin/login", data={"email": ADMIN_EMAIL, "password": "wrong", "csrf": csrf_of(page.text)}, follow_redirects=True)
    assert "メールアドレスまたはパスワードが正しくありません" in r.text      # no language chosen: Japanese
    assert client.post("/admin/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}).status_code == 403  # no CSRF token


def test_lockout_after_too_many_failures(client):
    log_in(client, "nobody@example.ac.jp", "x")   # Chinese UI, throttles reset
    for _ in range(5):
        page = client.get("/admin/login")
        client.post("/admin/login", data={"email": ADMIN_EMAIL, "password": "nope", "csrf": csrf_of(page.text)})
    page = client.get("/admin/login")
    r = client.post("/admin/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "csrf": csrf_of(page.text)}, follow_redirects=True)
    assert "登录失败次数过多" in r.text  # even the right password waits out the lockout
    reset_throttles()
    client.cookies.clear()


def test_upload_stage_and_publish(admin):
    from app import processing
    page = admin.get("/admin/upload")
    assert page.status_code == 200
    r = admin.post("/admin/upload", data={"source": "admin-upload", "kind": "upload", "label": "guide", "csrf": csrf_of(page.text)},
                   files=[("files", ("guide.txt", "履修ガイド\n\n後学期の履修登録は9月です。".encode(), "text/plain"))], follow_redirects=False)
    assert r.status_code == 303 and "/admin/batches/" in r.headers["location"]
    batch_page = admin.get(r.headers["location"])
    assert "等待审核后发布" in batch_page.text and "staging" in batch_page.text
    # process like the worker would, then publish from the page
    from app.db import tx
    with tx() as conn:
        vid = conn.execute("SELECT v.id FROM versions v JOIN documents d ON d.id = v.document_id WHERE d.key = 'guide'").fetchone()["id"]
    processing.process_version({"version_id": vid})
    bid = r.headers["location"].rsplit("/", 1)[1]
    r = admin.post(f"/admin/batches/{bid}/publish", data={"csrf": csrf_of(batch_page.text)}, follow_redirects=True)
    assert "已发布" in r.text
    found = admin.get("/admin/search", params={"q": "履修登録", "source": "admin-upload"})
    assert "後学期の履修登録は9月です" in found.text


def test_overview_shows_sources_and_log(admin):
    r = admin.get("/admin")
    assert r.status_code == 200 and "分类" in r.text and "操作日志" in r.text
