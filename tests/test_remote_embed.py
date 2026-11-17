"""Vectors computed outside: a batch marked remote is only parsed and chunked by the worker; the texts show up as
pending; vectors from another model or not normalised are refused; correct ones are stored and searchable."""
from app import processing
from app.embed import RURI, RURI_REVISION, FakeEmbedder


def test_remote_batch_round_trip(client, auth):
    client.put("/v1/admin/sources/remote-src", json={"name": "remote-src", "kind": "site"}, headers=auth)
    b = client.post("/v1/admin/batches", json={"source": "remote-src", "label": "r1", "kind": "partial", "embed": "remote"}, headers=auth).json()
    assert b["embed"] == "remote"
    d = client.put(f"/v1/admin/batches/{b['id']}/documents", json={"key": "k1", "title": "奨学金の案内", "text": "奨学金の申請は5月末まで。\n\n必要書類は学生課で配布。"}, headers=auth).json()
    processing.process_version({"version_id": d["version_id"]})
    page = client.get("/v1/admin/embeddings/pending", params={"limit": 500}, headers=auth).json()
    ours = [t for t in page["texts"] if "奨学金" in t["search_text"]]
    assert ours and page["model"] == RURI and page["revision"] == RURI_REVISION and page["prefix"] == "検索文書: "
    vec = FakeEmbedder().documents([ours[0]["search_text"]])[0]
    wrong = client.put("/v1/admin/embeddings", json={"model": "other/model", "revision": "x", "vectors": [{"text_id": ours[0]["text_id"], "embedding": vec}]}, headers=auth)
    assert wrong.status_code == 409
    unnorm = client.put("/v1/admin/embeddings", json={"model": RURI, "revision": RURI_REVISION, "vectors": [{"text_id": ours[0]["text_id"], "embedding": [x * 3 for x in vec]}]}, headers=auth)
    assert unnorm.status_code == 422
    vectors = [{"text_id": t["text_id"], "embedding": FakeEmbedder().documents([t["search_text"]])[0]} for t in ours]
    ok = client.put("/v1/admin/embeddings", json={"model": RURI, "revision": RURI_REVISION, "vectors": vectors}, headers=auth).json()
    assert ok["stored"] == len(ours)
    left = client.get("/v1/admin/embeddings/pending", params={"limit": 500}, headers=auth).json()["texts"]
    assert not any("奨学金" in t["search_text"] for t in left)
    client.post(f"/v1/admin/batches/{b['id']}/finish", json={"publish": True}, headers=auth)
    hits = client.get("/v1/search", params={"q": "奨学金 申請", "source": "remote-src"}).json()["results"]
    assert hits and "meaning" in hits[0]["matched"]


def test_embeddings_api_is_admin_only(client):
    assert client.get("/v1/admin/embeddings/pending").status_code == 401
    assert client.put("/v1/admin/embeddings", json={}).status_code == 401
