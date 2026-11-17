"""Ingest with history: same content = sighting, new content = new version; staging, publish, retire (rollback);
gone after a full crawl that did not see it; purge removes everything of one document."""
import base64

import pytest


@pytest.fixture(scope="module")
def source(client, auth):
    r = client.put("/v1/admin/sources/test-site", json={"name": "test-site", "kind": "site", "base_url": "https://example-u.ac.jp"}, headers=auth)
    assert r.status_code == 200, r.text
    return "test-site"


def batch(client, auth, source, label, kind="partial"):
    r = client.post("/v1/admin/batches", json={"source": source, "label": label, "kind": kind}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def put(client, auth, b, key, text, **kw):
    r = client.put(f"/v1/admin/batches/{b}/documents", json={"key": key, "text": text, **kw}, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def finish(client, auth, b, publish=True):
    r = client.post(f"/v1/admin/batches/{b}/finish", json={"publish": publish}, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def test_history_is_kept_and_current_is_the_newest(client, auth, source):
    b1 = batch(client, auth, source, "crawl 1")
    d = put(client, auth, b1, "https://example-u.ac.jp/news/1", "履修登録は4月10日まで", title="お知らせ")
    assert d["changed"]
    finish(client, auth, b1)
    b2 = batch(client, auth, source, "crawl 2")
    same = put(client, auth, b2, "https://example-u.ac.jp/news/1", "履修登録は4月10日まで")
    assert not same["changed"] and same["version_id"] == d["version_id"]  # unchanged: only a sighting
    finish(client, auth, b2)
    b3 = batch(client, auth, source, "crawl 3")
    new = put(client, auth, b3, "https://example-u.ac.jp/news/1", "履修登録は4月17日まで（延長）")
    assert new["changed"] and new["version_id"] != d["version_id"]
    finish(client, auth, b3)

    cur = client.get(f"/v1/documents/{d['document_id']}").json()
    assert cur["body"] == "履修登録は4月17日まで（延長）" and cur["superseded_at"] is None
    versions = client.get(f"/v1/documents/{d['document_id']}/versions").json()
    assert [v["version_id"] for v in versions] == [new["version_id"], d["version_id"]]
    old = client.get(f"/v1/documents/{d['document_id']}/versions/{d['version_id']}").json()
    assert old["body"] == "履修登録は4月10日まで"
    # as_of: the version current at that time
    at = client.get(f"/v1/documents/{d['document_id']}", params={"as_of": versions[1]["first_seen_at"]}).json()
    assert at["version_id"] == d["version_id"] and at["superseded_at"] is not None


def test_staging_is_invisible_until_published_and_retire_rolls_back(client, auth, source):
    b1 = batch(client, auth, source, "guide v1")
    d = put(client, auth, b1, "https://example-u.ac.jp/guide", "ガイダンス資料 第1版")
    finish(client, auth, b1)
    b2 = batch(client, auth, source, "guide v2")
    put(client, auth, b2, "https://example-u.ac.jp/guide", "ガイダンス資料 第2版")
    assert finish(client, auth, b2, publish=False)["status"] == "staging"
    assert client.get(f"/v1/documents/{d['document_id']}").json()["body"] == "ガイダンス資料 第1版"  # staged: not yet
    assert client.post(f"/v1/admin/batches/{b2}/publish", headers=auth).json()["status"] == "published"
    assert client.get(f"/v1/documents/{d['document_id']}").json()["body"] == "ガイダンス資料 第2版"
    assert client.post(f"/v1/admin/batches/{b2}/retire", headers=auth).json()["status"] == "retired"
    assert client.get(f"/v1/documents/{d['document_id']}").json()["body"] == "ガイダンス資料 第1版"  # rolled back


def test_a_full_crawl_marks_what_it_did_not_see_as_gone(client, auth):
    client.put("/v1/admin/sources/test-full", json={"name": "test-full", "kind": "site"}, headers=auth)
    b1 = batch(client, auth, "test-full", "full 1", kind="full")
    a = put(client, auth, b1, "https://example-u.ac.jp/a", "A")
    put(client, auth, b1, "https://example-u.ac.jp/b", "B")
    finish(client, auth, b1)
    b2 = batch(client, auth, "test-full", "full 2", kind="full")
    put(client, auth, b2, "https://example-u.ac.jp/b", "B")  # a is no longer on the site
    finish(client, auth, b2)
    gone = client.get(f"/v1/documents/{a['document_id']}").json()
    assert gone["gone"] is True and gone["body"] == "A"  # history kept


def test_closed_batches_take_no_documents(client, auth, source):
    b = batch(client, auth, source, "closed")
    finish(client, auth, b)
    r = client.put(f"/v1/admin/batches/{b}/documents", json={"key": "k", "text": "x"}, headers=auth)
    assert r.status_code == 409


def test_original_files_are_stored_once_and_purge_removes_everything(client, auth, source):
    raw = "シラバス PDF の代わり".encode() * 10
    b = batch(client, auth, source, "files")
    d = put(client, auth, b, "https://example-u.ac.jp/s.pdf", None, raw_base64=base64.b64encode(raw).decode(), mime="application/pdf", file_name="s.pdf")
    finish(client, auth, b)
    import hashlib
    sha = hashlib.sha256(raw).hexdigest()
    stored = client.files_dir / sha[:2] / sha
    assert stored.is_file()
    r = client.post(f"/v1/admin/documents/{d['document_id']}/purge", json={"reason": "test purge"}, headers=auth)
    assert r.status_code == 200 and r.json()["files_deleted"] == 1
    assert client.get(f"/v1/documents/{d['document_id']}").status_code == 404
    assert not stored.exists()  # the original is gone with the document
