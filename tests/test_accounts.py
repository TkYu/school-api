"""Accounts: the first confirmed user is the admin, the allowlist, confirmation links, login rules, password reset
by mail, own password change, the admin disabling users and setting a password, and who may open what."""
from __future__ import annotations

from datetime import timedelta

from app.db import tx
from tests.conftest import sent, ADMIN_EMAIL, ADMIN_PASSWORD, csrf_of, last_link, log_in, reset_throttles, sign_up

PW = "a long password 123"


def log_admin(client):
    assert log_in(client, ADMIN_EMAIL, ADMIN_PASSWORD).status_code == 303
    return client


def submitter(client, email: str, nickname: str = "Lin"):
    sign_up(client, email, PW, nickname)
    client.cookies.clear()
    return email


def test_the_first_confirmed_user_is_the_only_admin(client):
    with tx() as conn:
        rows = conn.execute("SELECT email, role FROM users WHERE role = 'admin'").fetchall()
    assert [r["email"] for r in rows] == [ADMIN_EMAIL]
    submitter(client, "second@example.ac.jp")
    with tx() as conn:
        assert conn.execute("SELECT role FROM users WHERE email = 'second@example.ac.jp'").fetchone()["role"] == "submitter"


def test_the_allowlist_decides_who_may_sign_up(client):
    n = len(sent())
    r = sign_up(client, "someone@elsewhere.example", PW, confirm=False)
    assert "このメールアドレスでは登録できません" in r.text and len(sent()) == n
    sign_up(client, "guest@other.example", PW, confirm=False)                   # a listed address
    assert sent()[-1].to == "guest@other.example"
    # an address that has an account already: the same answer, no mail
    n = len(sent())
    r = sign_up(client, ADMIN_EMAIL.upper(), PW, confirm=False)
    assert "メールを確認してください" in r.text and len(sent()) == n


def test_unconfirmed_accounts_cannot_log_in_and_links_work_once(client):
    sign_up(client, "later@example.ac.jp", PW, confirm=False)
    link = last_link("confirm")
    r = log_in(client, "later@example.ac.jp", PW, lang="ja")
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    assert "まだ確認されていません" in client.get("/admin/login").text
    assert client.get(link).status_code == 200
    assert client.get(link).status_code == 400                                   # used
    assert log_in(client, "later@example.ac.jp", PW).headers["location"] == "/admin/my"
    # an expired link does not work
    sign_up(client, "expired@example.ac.jp", PW, confirm=False)
    link = last_link("confirm")
    with tx() as conn:
        conn.execute("UPDATE email_tokens SET expires_at = now() - %s WHERE used_at IS NULL", (timedelta(minutes=1),))
    assert client.get(link).status_code == 400
    client.cookies.clear()


def test_submitters_reach_only_their_pages(client, auth):
    email = submitter(client, "sub@example.ac.jp")
    assert log_in(client, email, PW).headers["location"] == "/admin/my"
    for path in ("/admin", "/admin/users", "/admin/categories", "/admin/review", "/admin/documents", "/admin/batches/1", "/admin/upload"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/admin/my", path
    assert client.get("/admin/my").status_code == 200 and client.get("/admin/settings").status_code == 200
    assert client.get("/v1/admin/sources").status_code == 403                    # the ingest API is the admin's
    page = client.get("/admin/my")
    assert client.post("/admin/users/1/disable", data={"csrf": csrf_of(page.text)}).status_code == 403
    client.cookies.clear()


def test_forgot_and_reset_password(client):
    email = submitter(client, "forgetful@example.ac.jp")
    other = client.__class__(client.app, base_url="https://testserver")         # a second browser, logged in
    assert log_in(other, email, PW).status_code == 303
    reset_throttles()
    page = client.get("/admin/forgot")
    n = len(sent())
    r = client.post("/admin/forgot", data={"email": "nobody@example.ac.jp", "csrf": csrf_of(page.text)})
    assert "メールを確認してください" in r.text and len(sent()) == n        # unknown address: same page, no mail
    client.post("/admin/forgot", data={"email": email, "csrf": csrf_of(page.text)})
    link = last_link("reset")
    form = client.get(link)
    assert form.status_code == 200
    token = link.split("token=")[1]
    new = "another long password"
    r = client.post("/admin/reset", data={"token": token, "password": new, "password2": new, "csrf": csrf_of(form.text)}, follow_redirects=True)
    assert "パスワードを変更しました" in r.text
    assert client.get(link).status_code == 400                                   # used
    assert log_in(client, email, PW).headers["location"] == "/admin/login"       # the old password is gone
    assert log_in(client, email, new).headers["location"] == "/admin/my"
    assert other.get("/admin/my", follow_redirects=False).headers["location"] == "/admin/login"   # other sessions end
    client.cookies.clear()


def test_change_own_password(client):
    email = submitter(client, "changer@example.ac.jp")
    log_in(client, email, PW)
    page = client.get("/admin/password")
    r = client.post("/admin/password", data={"current": "wrong", "password": "new password 456", "password2": "new password 456",
                                             "csrf": csrf_of(page.text)}, follow_redirects=True)
    assert "当前密码不正确" in r.text
    r = client.post("/admin/password", data={"current": PW, "password": "short", "password2": "short", "csrf": csrf_of(page.text)}, follow_redirects=True)
    assert "至少需要 10 个字符" in r.text
    r = client.post("/admin/password", data={"current": PW, "password": "new password 456", "password2": "new password 456",
                                             "csrf": csrf_of(page.text)}, follow_redirects=True)
    assert "密码已修改" in r.text and client.get("/admin/my").status_code == 200   # this session stays
    client.cookies.clear()


def test_admin_disables_and_enables_a_user(client):
    submitter(client, "disable-me@example.ac.jp")
    with tx() as conn:
        uid = conn.execute("SELECT id FROM users WHERE email = 'disable-me@example.ac.jp'").fetchone()["id"]
        admin_id = conn.execute("SELECT id FROM users WHERE role = 'admin'").fetchone()["id"]
    user_browser = client.__class__(client.app, base_url="https://testserver")
    assert log_in(user_browser, "disable-me@example.ac.jp", PW).status_code == 303
    admin = log_admin(client)
    page = admin.get(f"/admin/users/{uid}")
    assert "disable-me@example.ac.jp" in page.text
    r = admin.post(f"/admin/users/{uid}/disable", data={"csrf": csrf_of(page.text)}, follow_redirects=True)
    assert "已停用" in r.text
    assert user_browser.get("/admin/my", follow_redirects=False).headers["location"] == "/admin/login"   # session ended
    assert log_in(user_browser, "disable-me@example.ac.jp", PW).headers["location"] == "/admin/login"
    assert "停用" in user_browser.get("/admin/login").text
    r = admin.post(f"/admin/users/{admin_id}/disable", data={"csrf": csrf_of(page.text)}, follow_redirects=True)
    assert "管理员不能被停用" in r.text
    r = admin.post(f"/admin/users/{uid}/enable", data={"csrf": csrf_of(page.text)}, follow_redirects=True)
    assert "已恢复启用" in r.text and log_in(user_browser, "disable-me@example.ac.jp", PW).headers["location"] == "/admin/my"
    client.cookies.clear()


def test_admin_sets_a_password_shown_once_and_the_user_must_change_it(client):
    submitter(client, "reset-me@example.ac.jp")
    with tx() as conn:
        uid = conn.execute("SELECT id FROM users WHERE email = 'reset-me@example.ac.jp'").fetchone()["id"]
    admin = log_admin(client)
    page = admin.get(f"/admin/users/{uid}")
    r = admin.post(f"/admin/users/{uid}/reset-password", data={"csrf": csrf_of(page.text)}, follow_redirects=True)
    import re
    password = re.search(r'<p class="secret">([^<]+)</p>', r.text).group(1)
    assert '<p class="secret">' not in admin.get(f"/admin/users/{uid}").text       # only once
    user = client.__class__(client.app, base_url="https://testserver")
    assert log_in(user, "reset-me@example.ac.jp", PW).headers["location"] == "/admin/login"   # the old one is gone
    assert log_in(user, "reset-me@example.ac.jp", password).headers["location"] == "/admin/password"
    assert user.get("/admin/my", follow_redirects=False).headers["location"] == "/admin/password"
    page = user.get("/admin/password")
    assert "管理员重置了你的密码" in page.text
    r = user.post("/admin/password", data={"current": password, "password": PW, "password2": PW, "csrf": csrf_of(page.text)}, follow_redirects=False)
    assert r.headers["location"] == "/admin/my" and user.get("/admin/my").status_code == 200
    client.cookies.clear()


def test_the_language_follows_the_switcher_the_user_and_the_browser(client):
    client.cookies.clear()
    assert "Log in" in client.get("/admin/login", headers={"Accept-Language": "en-US,en;q=0.9"}).text
    assert "登录" in client.get("/admin/login", headers={"Accept-Language": "fr, zh-CN;q=0.8"}).text
    client.get("/admin/lang?l=en")
    assert "Forgot your password?" in client.get("/admin/login", headers={"Accept-Language": "ja"}).text   # the switcher wins
    r = client.get("/admin/lang?l=ja&next=https://evil.example/", follow_redirects=False)
    assert r.headers["location"] == "/admin"                                    # no open redirect
    client.cookies.clear()


def test_sign_up_without_mail_set_up_says_so(client, setting):
    setting(mail_backend="smtp", smtp_host="", smtp_from="")
    page = client.get("/admin/signup")
    assert "メール送信が設定されていない" in page.text
    reset_throttles()
    r = client.post("/admin/signup", data={"email": "x@example.ac.jp", "password": PW, "password2": PW, "csrf": csrf_of(page.text)}, follow_redirects=True)
    assert "メール送信が設定されていない" in r.text
    client.cookies.clear()


def test_ingest_token_batches_belong_to_the_admin(client, auth):
    client.put("/v1/admin/sources/owned", json={"name": "owned", "kind": "upload"}, headers=auth)
    b = client.post("/v1/admin/batches", json={"source": "owned", "label": "x", "kind": "partial"}, headers=auth).json()
    with tx() as conn:
        assert conn.execute("SELECT u.email FROM batches b JOIN users u ON u.id = b.owner_id WHERE b.id = %s", (b["id"],)).fetchone()["email"] == ADMIN_EMAIL
        assert conn.execute("SELECT actor FROM audit_log WHERE action = 'batch.open' AND target = %s", (str(b["id"]),)).fetchone()["actor"] == "ingest-token"


def test_pages_only_queue_mail_and_the_worker_makes_the_token(client):
    from app import mail
    before = len(sent())
    sign_up(client, "queued@example.ac.jp", PW, confirm=False)
    assert len(mail.OUTBOX) == before                                            # the page sent nothing itself
    with tx() as conn:
        job = conn.execute("SELECT payload FROM jobs WHERE kind = 'send_mail' AND status = 'queued' ORDER BY id DESC LIMIT 1").fetchone()
        tokens = conn.execute("SELECT count(*) AS n FROM email_tokens t JOIN users u ON u.id = t.user_id WHERE u.email = 'queued@example.ac.jp'").fetchone()["n"]
    assert job["payload"]["mail"] == "confirm" and "token" not in str(job["payload"]) and tokens == 0
    link = last_link("confirm")                                                  # the "worker" sends it now
    with tx() as conn:
        stored = conn.execute("SELECT token_sha256 FROM email_tokens ORDER BY id DESC LIMIT 1").fetchone()["token_sha256"]
    assert link.split("token=")[1] not in stored and len(stored) == 64          # only the hash is kept
    from app.worker import HANDLERS
    from app import mail as m
    m.register(HANDLERS)
    assert HANDLERS["send_mail"] is m.send_job


def test_a_changed_password_ends_open_reset_links(client):
    email = submitter(client, "tidy@example.ac.jp")
    reset_throttles()
    page = client.get("/admin/forgot")
    client.post("/admin/forgot", data={"email": email, "csrf": csrf_of(page.text)})
    link = last_link("reset")
    log_in(client, email, PW)
    page = client.get("/admin/password")
    client.post("/admin/password", data={"current": PW, "password": "changed password 9", "password2": "changed password 9", "csrf": csrf_of(page.text)})
    client.cookies.clear()
    assert client.get(link).status_code == 400
