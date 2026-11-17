"""Admin maintenance: find documents, hide / show (reversible), process again, look at a version's diff, delete a
batch created by mistake, retry a failed job."""
import pytest

from app import processing
from app.db import tx
from tests.conftest import csrf_of


@pytest.fixture
def docs(client, auth):
    client.put("/v1/admin/sources/maint", json={"name": "maint", "kind": "site"}, headers=auth)
    b = client.post("/v1/admin/batches", json={"source": "maint", "label": "m1", "kind": "partial"}, headers=auth).json()["id"]
    ids = []
    for k, text in (("a", "寮の申し込みは3月まで"), ("b", "駐輪場の登録について")):
        r = client.put(f"/v1/admin/batches/{b}/documents", json={"key": f"maint/{k}", "title": text, "text": text}, headers=auth).json()
        processing.process_version({"version_id": r["version_id"]})
        ids.append(r["document_id"])
    client.post(f"/v1/admin/batches/{b}/finish", json={"publish": True}, headers=auth)
    return {"batch": b, "ids": ids}


def test_find_hide_and_show(admin, docs):
    page = admin.get("/admin/documents", params={"source": "maint", "q": "maint/a"})
    assert "maint/a" in page.text and "maint/b" not in page.text
    a = docs["ids"][0]
    r = admin.post("/admin/documents/bulk", data={"action": "hide", "ids": [a], "reason": "内容を確認中", "csrf": csrf_of(page.text)}, follow_redirects=True)
    assert "已隐藏 1 份资料" in r.text
    assert admin.get(f"/v1/documents/{a}").status_code == 404                     # not served
    hits = admin.get("/v1/search", params={"q": "寮 申し込み", "source": "maint"}).json()["results"]
    assert a not in [h["document_id"] for h in hits]                               # not searched
    assert "maint/a" in admin.get("/admin/documents", params={"source": "maint", "state": "hidden"}).text
    r = admin.post(f"/admin/documents/{a}/show", data={"csrf": csrf_of(r.text)}, follow_redirects=True)
    assert "已恢复" in r.text and admin.get(f"/v1/documents/{a}").status_code == 200


def test_bulk_purge_needs_the_confirmation_text(admin, docs):
    page = admin.get("/admin/documents", params={"source": "maint"})
    b = docs["ids"][1]
    r = admin.post("/admin/documents/bulk", data={"action": "purge", "ids": [b], "reason": "テスト", "confirm": "", "csrf": csrf_of(page.text)}, follow_redirects=True)
    assert "删除1份" in r.text and admin.get(f"/admin/documents/{b}").status_code == 200   # still there
    r = admin.post("/admin/documents/bulk", data={"action": "purge", "ids": [b], "reason": "テスト", "confirm": "删除1份", "csrf": csrf_of(page.text)}, follow_redirects=True)
    assert "已彻底删除 1 份" in r.text and admin.get(f"/admin/documents/{b}").status_code == 404


def test_reprocess_queues_jobs(admin, docs):
    page = admin.get(f"/admin/documents/{docs['ids'][0]}")
    with tx() as conn:
        before = conn.execute("SELECT count(*) AS n FROM jobs WHERE status = 'queued'").fetchone()["n"]
    r = admin.post(f"/admin/documents/{docs['ids'][0]}/reprocess", data={"csrf": csrf_of(page.text)}, follow_redirects=True)
    assert "已排入重新解析任务" in r.text
    with tx() as conn:
        assert conn.execute("SELECT count(*) AS n FROM jobs WHERE status = 'queued'").fetchone()["n"] == before + 1


def test_version_page_shows_what_changed(admin, client, auth, docs):
    b = client.post("/v1/admin/batches", json={"source": "maint", "label": "m2", "kind": "partial"}, headers=auth).json()["id"]
    new = client.put(f"/v1/admin/batches/{b}/documents", json={"key": "maint/a", "text": "寮の申し込みは4月10日まで（延長）"}, headers=auth).json()
    client.post(f"/v1/admin/batches/{b}/finish", json={"publish": True}, headers=auth)
    page = admin.get(f"/admin/documents/{new['document_id']}/versions/{new['version_id']}").text
    assert '+寮の申し込みは4月10日まで（延長）' in page and '-寮の申し込みは3月まで' in page


def test_delete_a_batch_created_by_mistake(admin, client, auth):
    client.put("/v1/admin/sources/oops", json={"name": "oops", "kind": "upload"}, headers=auth)
    b = client.post("/v1/admin/batches", json={"source": "oops", "label": "wrong", "kind": "partial"}, headers=auth).json()["id"]
    d = client.put(f"/v1/admin/batches/{b}/documents", json={"key": "oops/1", "text": "間違えたファイル"}, headers=auth).json()
    client.post(f"/v1/admin/batches/{b}/finish", json={"publish": False}, headers=auth)
    page = admin.get(f"/admin/batches/{b}")
    r = admin.post(f"/admin/batches/{b}/purge-batch", data={"reason": "誤ってアップロード", "confirm": f"删除批次{b}", "csrf": csrf_of(page.text)}, follow_redirects=True)
    assert f"批次 {b} 已删除" in r.text
    assert admin.get(f"/admin/documents/{d['document_id']}").status_code == 404


def test_retry_a_failed_job(admin):
    with tx() as conn:
        jid = conn.execute("INSERT INTO jobs (kind, payload, status, attempts, error) VALUES ('process_version', '{\"version_id\": -1}', 'failed', 5, 'boom') RETURNING id").fetchone()["id"]
    page = admin.get("/admin/jobs")
    assert "boom" in page.text
    r = admin.post(f"/admin/jobs/{jid}/retry", data={"csrf": csrf_of(page.text)}, follow_redirects=True)
    assert f"任务 {jid} 已重新排队" in r.text
    with tx() as conn:
        assert conn.execute("SELECT status FROM jobs WHERE id = %s", (jid,)).fetchone()["status"] == "queued"
        conn.execute("DELETE FROM jobs WHERE id = %s", (jid,))


def test_log_page(admin):
    assert "操作日志" in admin.get("/admin/log").text
