"""Community BBS topics: one chunk per post; the index sees the Japanese translation, the agent reads the original;
no user names; a reparse finds the topic again (its JSON is the version's file)."""
import base64
import json

from app import processing
from app.parsers import bbs

TOPIC = {"title": "关于奖学金", "title_ja": "奨学金について", "category": "🌏 国際交流 / 中文", "posts": [
    {"post_number": 1, "created_at": "2026-11-10T00:00:00Z", "staff": False, "lang": "zh",
     "raw": "[quote=\"linmei, post:3\"]谢谢[/quote]\n@linmei 留学生也可以申请JASSO的奖学金，一个月4万多。\n![图](upload://abc.png)",
     "ja": "留学生でも JASSO の奨学金を申請でき、月4万円あまりです。"},
    {"post_number": 2, "created_at": "2026-11-10T01:00:00Z", "staff": True, "lang": "ja", "raw": "学生課でも案内しています。"},
]}


def test_clean_removes_names_and_images():
    out = bbs.clean(TOPIC["posts"][0]["raw"])
    assert "linmei" not in out and "@…" in out and "[quote]" in out and "[画像]" in out and "upload://" not in out


def test_chunks_index_the_translation_and_keep_the_original():
    parts = bbs.chunks(TOPIC)
    assert [s for s, _, _ in parts] == ["#1", "#2 · 運営"]
    assert "留学生也可以申请" in parts[0][1] and parts[0][2].startswith("留学生でも JASSO")
    assert parts[1][1] == parts[1][2] == "学生課でも案内しています。"


def test_a_chinese_post_is_found_in_japanese_and_read_in_chinese(client, auth):
    client.put("/v1/admin/sources/test-bbs", json={"name": "test-bbs", "kind": "bbs"}, headers=auth)
    b = client.post("/v1/admin/batches", json={"source": "test-bbs", "label": "b1", "kind": "partial"}, headers=auth).json()["id"]
    raw = json.dumps(TOPIC, ensure_ascii=False).encode()
    r = client.put(f"/v1/admin/batches/{b}/documents", headers=auth, json={
        "key": "https://bbs.example-u.ac.jp/t/86", "url": "https://bbs.example-u.ac.jp/t/x/86", "title": TOPIC["title"], "mime": "application/json",
        "file_name": "topic-86.json", "raw_base64": base64.b64encode(raw).decode(), "text": bbs.readable(TOPIC),
        "meta": {"category": "コミュニティ（BBS）", "bbs_category": TOPIC["category"]}}).json()
    processing.process_version({"version_id": r["version_id"]})
    processing.process_version({"version_id": r["version_id"]})  # a reparse still finds the topic
    client.post(f"/v1/admin/batches/{b}/finish", json={"publish": True}, headers=auth)
    hits = client.get("/v1/search", params={"q": "奨学金 申請 留学生", "source": "test-bbs"}).json()["results"]
    assert hits and hits[0]["category"] == "コミュニティ（BBS）"
    assert "留学生也可以申请" in hits[0]["text"] and "linmei" not in json.dumps(hits, ensure_ascii=False)
    doc = client.get(f"/v1/documents/{r['document_id']}").json()
    assert doc["body"].startswith("关于奖学金") and "#2 (2026-11-10 · staff)" in doc["body"]
