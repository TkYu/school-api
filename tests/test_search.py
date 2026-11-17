"""Search end to end with the stand-in embedder: ingest, process (the worker's handler), publish, search."""
import pytest

from app import processing


@pytest.fixture(scope="module")
def site(client, auth):
    client.put("/v1/admin/sources/search-site", json={"name": "search-site", "kind": "site"}, headers=auth)
    b = client.post("/v1/admin/batches", json={"source": "search-site", "label": "s1", "kind": "partial"}, headers=auth).json()["id"]
    docs = {
        "reg": "履修登録期間のお知らせ\n\n後学期の履修登録は9月20日から9月27日までです。期間外の登録はできません。",
        "lib": "図書館の開館時間\n\n平日は9時から21時まで開館しています。",
        "exam": "定期試験について\n\n期末試験は1月下旬に行います。試験時間割は掲示板で確認してください。",
    }
    ids = {}
    for k, text in docs.items():
        r = client.put(f"/v1/admin/batches/{b}/documents", json={"key": f"https://example-u.ac.jp/{k}", "url": f"https://example-u.ac.jp/{k}",
                                                                   "title": text.split("\n")[0], "text": text}, headers=auth).json()
        processing.process_version({"version_id": r["version_id"]})
        ids[k] = r["document_id"]
    client.post(f"/v1/admin/batches/{b}/finish", json={"publish": True}, headers=auth)
    return ids


def test_finds_by_words_and_cites_the_source(client, site):
    r = client.get("/v1/search", params={"q": "履修登録 期間", "source": "search-site"}).json()
    top = r["results"][0]
    assert top["document_id"] == site["reg"] and top["url"] == "https://example-u.ac.jp/reg"
    assert "words" in top["matched"] and top["title"] == "履修登録期間のお知らせ"


def test_filters_and_validation(client, site):
    assert client.get("/v1/search", params={"q": "試験", "source": "no-such-source"}).json()["results"] == []
    assert client.get("/v1/search", params={"q": ""}).status_code == 422


def test_search_is_public_but_admin_is_not(client):
    assert client.get("/v1/search", params={"q": "図書館"}).status_code == 200
    assert client.get("/v1/admin/batches").status_code == 401


def test_one_hit_per_document_with_the_rest_counted(client, auth):
    client.put("/v1/admin/sources/collapse-site", json={"name": "collapse-site", "kind": "site"}, headers=auth)
    b = client.post("/v1/admin/batches", json={"source": "collapse-site", "label": "c", "kind": "partial"}, headers=auth).json()["id"]
    long_page = "\n".join(f"駐輪場の案内 その{i}。" + "自転車で通学する学生は駐輪場を使います。" * 30 for i in range(4))
    r = client.put(f"/v1/admin/batches/{b}/documents", headers=auth, json={"key": "https://example-u.ac.jp/bike", "url": "https://example-u.ac.jp/bike",
                                                                          "title": "通学について", "text": long_page}).json()
    processing.process_version({"version_id": r["version_id"]})
    client.post(f"/v1/admin/batches/{b}/finish", json={"publish": True}, headers=auth)
    hits = client.get("/v1/search", params={"q": "駐輪場", "source": "collapse-site"}).json()["results"]
    assert [h["document_id"] for h in hits] == [r["document_id"]]
    assert hits[0]["more_in_document"] >= 2


def test_old_news_ranks_lower():
    from datetime import date
    from app.search import news_weight
    today = date(2026, 11, 9)
    assert news_weight("ニュース", None, date(2026, 6, 1), today) == 1.0
    assert news_weight("ニュース", None, date(2025, 6, 1), today) == 0.85
    assert news_weight(None, "https://www.example-u.ac.jp/topics/20200301_01/", date(2020, 3, 1), today) == 0.6
    assert news_weight("学生生活", None, date(2015, 1, 1), today) == 1.0   # a standing page is not news
    assert news_weight("ニュース", None, None, today) == 1.0


def test_site_dates_come_from_the_news_url_or_the_sitemap():
    from app.cli import site_date
    assert site_date("https://www.example-u.ac.jp/topics/20211102_05/", "2026-10-16", "2026-10-16T14:54:48+09:00") == "2021-11-02"
    assert site_date("https://www.example-u.ac.jp/topics/20230050/", "2023-05-01", None) == "2023-05-01"   # no such day
    assert site_date("https://www.example-u.ac.jp/campuslife/jasso/", "2004-04-01", "2017-05-27T10:22:01+09:00") == "2017-05-27"
    assert site_date("https://www.example-u.ac.jp/campuslife/x/", "2004-04-01", None) is None


def test_home_page_searches_in_japanese_and_escapes(client, site):
    from app.db import tx
    with tx() as conn:   # the admin shows the category on the home page (Categories page)
        conn.execute("UPDATE sources SET label_ja = '公式サイト', show_on_home = true WHERE name = 'search-site'")
    home = client.get("/")
    assert home.status_code == 200 and 'name="q"' in home.text and "SIT" in home.text and 'lang="ja"' in home.text
    r = client.get("/", params={"q": "履修登録 期間"})
    assert r.status_code == 200 and "履修登録期間のお知らせ" in r.text and "<b>履修登録</b>" in r.text
    assert "すべて" in r.text and "公式サイト" in r.text
    evil = client.get("/", params={"q": "<script>alert(1)</script>"})
    assert evil.status_code == 200 and "<script>alert(1)</script>" not in evil.text
    assert client.get("/", params={"q": "図書館", "t": "syllabus"}).status_code == 200
