"""Course lists: structured filters and plain substring matches return every match, not a ranked few."""
import psycopg
import pytest

from app import processing

COURSES = {
    # code: (name, department, grade, term, form, required, evaluation {item: percent}, text)
    "T-001": ("デザイン演習", "情報学科", "2年次", "後学期", "演習（メディア授業）", "選択",
              {"試験": 0, "成果物・発表": 60, "授業中の活動": 40}, "Ａｄｏｂｅ Illustrator を使って作品を制作する。"),
    "T-002": ("線形代数", "共通基盤科目", "1・2・3年次", "前学期", "講義", "必修",
              {"試験": 70, "予習・復習": 30}, "行列と行列式。高校数学（数学Ⅲ）の知識を前提とする。"),
    "T-003": ("映像制作", "情報学科", "3年次", "前学期（集中）", "実習", "選択",
              {"試験": 0, "成果物・発表": 100}, "adobe premiere pro で映像を編集する。100%出席が前提。"),
}


@pytest.fixture(scope="module")
def courses(client, auth, db_url):
    client.put("/v1/admin/sources/test-syllabus", json={"name": "test-syllabus", "kind": "syllabus"}, headers=auth)
    b = client.post("/v1/admin/batches", json={"source": "test-syllabus", "label": "t", "kind": "full"}, headers=auth).json()["id"]
    vids = {}
    for code, c in COURSES.items():
        r = client.put(f"/v1/admin/batches/{b}/documents", json={"key": code, "url": f"https://example-u.ac.jp/s/{code}",
                                                                   "title": c[0], "text": c[7]}, headers=auth).json()
        processing.process_version({"version_id": r["version_id"]})
        vids[code] = r["version_id"]
    with psycopg.connect(db_url) as conn:
        for code, (name, dep, grade, term, form, req, ev, _) in COURSES.items():
            v = vids[code]
            conn.execute("""INSERT INTO courses (version_id, year, code, name, faculty, department, grade, term, form, required, credits,
                                                 instructors) VALUES (%s, 2026, %s, %s, '情報学部', %s, %s, %s, %s, %s, 2, '{湘南花子}')""",
                         (v, code, name, dep, grade, term, form, req))
            for item, pct in ev.items():
                conn.execute("INSERT INTO course_evaluations (version_id, item, percent) VALUES (%s, %s, %s)", (v, item, pct))
            conn.execute("UPDATE chunks SET section = '授業の進め方' WHERE version_id = %s", (v,))
    client.post(f"/v1/admin/batches/{b}/finish", json={"publish": True}, headers=auth)
    return vids


def codes(r):
    assert r.status_code == 200, r.text
    return [x["code"] for x in r.json()["results"]]


def test_structured_filters(client, courses):
    assert codes(client.get("/v1/courses", params={"form": "メディア授業"})) == ["T-001"]
    assert codes(client.get("/v1/courses", params={"eval": "試験=0"})) == ["T-001", "T-003"]
    assert codes(client.get("/v1/courses", params={"eval": ["試験=0", "成果物・発表>=80"]})) == ["T-003"]
    assert codes(client.get("/v1/courses", params={"grade": 1})) == ["T-002"]          # 1・2・3年次
    assert codes(client.get("/v1/courses", params={"grade": 3})) == ["T-002", "T-003"]
    assert codes(client.get("/v1/courses", params={"term": "前学期"})) == ["T-002", "T-003"]  # also 前学期（集中）
    assert codes(client.get("/v1/courses", params={"department": ["情報学科", "共通基盤科目"], "required": "選択"})) == ["T-001", "T-003"]
    assert codes(client.get("/v1/courses", params={"instructor": "湘南 花子"})) == ["T-001", "T-002", "T-003"]


def test_contains_finds_every_match_ignoring_width_and_case(client, courses):
    r = client.get("/v1/courses", params={"contains": "adobe"})
    assert codes(r) == ["T-001", "T-003"]
    first = r.json()["results"][0]
    assert first["matches"][0]["section"] == "授業の進め方" and "Ａｄｏｂｅ" in first["matches"][0]["text"]
    assert first["evaluation"]["成果物・発表"] == 60
    assert codes(client.get("/v1/courses", params={"contains": ["Adobe", "映像"]})) == ["T-003"]  # all must appear
    assert codes(client.get("/v1/courses", params={"contains": "100%"})) == ["T-003"]             # % is literal
    assert codes(client.get("/v1/courses", params={"contains": "adobe", "section": "履修の条件"})) == []


def test_total_paging_and_errors(client, courses):
    r = client.get("/v1/courses", params={"department": "情報学科", "limit": 1}).json()
    assert r["total"] == 2 and r["next_offset"] == 1 and len(r["results"]) == 1
    r2 = client.get("/v1/courses", params={"department": "情報学科", "limit": 1, "offset": 1}).json()
    assert [x["code"] for x in r2["results"]] == ["T-003"] and "next_offset" not in r2
    bad = client.get("/v1/courses", params={"eval": "出席=10"})
    assert bad.status_code == 422 and "試験" in bad.text


def test_facets_list_the_real_values(client, courses):
    f = client.get("/v1/courses/facets").json()
    assert f["year"] == 2026 and f["total"] == 3
    assert f["form"]["演習（メディア授業）"] == 1 and f["department"]["情報学科"] == 2
    assert "試験" in f["evaluation_items"] and "授業の進め方" in f["sections"]
    # the course page still works next to the facets route
    assert client.get("/v1/courses/T-002").json()["name"] == "線形代数"


def test_search_shares_the_course_filters(client, courses):
    r = client.get("/v1/search", params={"q": "映像", "department": "情報学科", "eval": "試験=0"}).json()
    assert {x["course"]["code"] for x in r["results"]} <= {"T-001", "T-003"}
    assert client.get("/v1/search", params={"q": "行列", "grade": 4}).json()["results"] == []


def test_the_same_reads_take_a_json_body(client, courses):
    # a Windows terminal turns Japanese command-line arguments into ???; a here-doc body arrives as UTF-8
    r = client.post("/v1/courses", json={"form": "メディア授業", "eval": "試験=0"})
    assert r.status_code == 200 and [x["code"] for x in r.json()["results"]] == ["T-001"]
    r = client.post("/v1/courses", json={"department": ["情報学科", "共通基盤科目"], "contains": ["adobe"], "credits": 2})
    assert [x["code"] for x in r.json()["results"]] == ["T-001", "T-003"]
    s = client.post("/v1/search", json={"q": "映像", "department": "情報学科", "limit": 5})
    assert s.status_code == 200 and {x["course"]["code"] for x in s.json()["results"]} <= {"T-001", "T-003"}
    assert client.post("/v1/search", json={"q": ""}).status_code == 422
    c = client.post("/v1/calendar", json={"date": "2027-05-01", "program": "学部"})
    assert c.status_code == 200 and c.json()["year"] == 2027


def test_post_accepts_one_item_lists_for_single_value_filters(client, courses):
    # models often send ["メディア授業"] where one value is expected; a 422 there made the skill give up on the service
    one = client.post("/v1/courses", json={"form": ["メディア授業"], "limit": 3})
    plain = client.post("/v1/courses", json={"form": "メディア授業", "limit": 3})
    assert one.status_code == 200 and one.json()["total"] == plain.json()["total"]
    assert client.post("/v1/courses", json={"form": ["a", "b"]}).status_code == 422
