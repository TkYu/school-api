"""The school calendar: the 学年暦 PDF (the school's public 令和8年度 学部 calendar, kept out of git) parsed into events
with real dates, stored with the PDF's version, served by /v1/calendar with the registration windows."""
import base64
from datetime import date
from pathlib import Path

import pytest

from app import processing
from app.parsers import calendar, pdf

# not in git (the school's document): put the 令和8年度 学部 学年暦 PDF from the school's site here to run these tests
PDF_PATH = Path(__file__).parent / "fixtures" / "schedule2026_AB.pdf"
PDF = PDF_PATH.read_bytes() if PDF_PATH.exists() else b""
needs_pdf = pytest.mark.skipif(not PDF, reason="tests/fixtures/schedule2026_AB.pdf is not there (not in git)")


@needs_pdf
def test_parses_every_event_with_its_month_from_the_weekday_and_the_month_label():
    cal = calendar.parse_pdf(PDF)
    assert (cal.year, cal.program, len(cal.events), cal.notes) == (2026, "学部", 42, [])
    assert not [e for e in cal.events if e.note]
    by = {(e.term, e.label): e for e in cal.events}
    assert by[("前学期", "履修登録期間")].start == date(2026, 4, 7) and by[("前学期", "履修登録期間")].end == date(2026, 4, 15)
    # 30日(木) is April, not July (both Thursdays): the row sits next to the 4月 label
    assert by[("前学期", "4月15日授業分の振替休日")].start == date(2026, 4, 30)
    # one word holding date and event, a period across months
    assert (by[("後学期", "履修登録期間")].start, by[("後学期", "履修登録期間")].end) == (date(2026, 9, 24), date(2026, 10, 1))
    # January–March belong to the next calendar year; 2日(火) is February, not March
    assert (by[("後学期", "春季休業期間")].start, by[("後学期", "春季休業期間")].end) == (date(2027, 2, 2), date(2027, 3, 31))
    # an event written on two lines
    assert ("後学期", "10月12日、11月3日、11月23日授業分の振替休日") in by
    assert calendar.summary(cal)["後学期"] == {"registration": {"start": "2026-09-24", "end": "2026-10-01"}, "classes_start": "2026-09-25",
                                              "correction": {"start": "2026-10-02", "end": "2026-10-05"}}


@needs_pdf
def test_pdf_chunks_keep_page_numbers():
    parts = pdf.chunks(["第1章\n\n" + "あ" * 900, "二ページ目"])
    assert [s for s, _ in parts] == ["p.1", "p.1", "p.2"]


@pytest.fixture(scope="module")
def published(client, auth):
    client.put("/v1/admin/sources/cal-site", json={"name": "cal-site", "kind": "site"}, headers=auth)
    b = client.post("/v1/admin/batches", json={"source": "cal-site", "label": "c1", "kind": "partial"}, headers=auth).json()["id"]
    r = client.put(f"/v1/admin/batches/{b}/documents", headers=auth, json={
        "key": "https://example-u.ac.jp/media/schedule2026_AB.pdf", "url": "https://example-u.ac.jp/media/schedule2026_AB.pdf",
        "title": "R8学年暦（学部）", "mime": "application/pdf", "file_name": "schedule2026_AB.pdf",
        "raw_base64": base64.b64encode(PDF).decode(), "meta": {"category": "学生生活"}}).json()
    processing.process_version({"version_id": r["version_id"]})
    client.post(f"/v1/admin/batches/{b}/finish", json={"publish": True}, headers=auth)
    return r


@needs_pdf
def test_calendar_api_knows_the_registration_windows(client, published):
    r = client.get("/v1/calendar", params={"date": "2026-09-28"}).json()
    assert r["found"] and r["year"] == 2026 and r["registration_open"] == "後学期"
    assert "履修登録期間" in [e["label"] for e in r["today"]]
    assert r["terms"]["後学期"]["correction"] == {"start": "2026-10-02", "end": "2026-10-05"}
    assert r["source"]["title"] == "R8学年暦（学部）"
    assert client.get("/v1/calendar", params={"date": "2026-10-05"}).json()["registration_open"] == "後学期"   # correction, last day
    assert client.get("/v1/calendar", params={"date": "2026-11-09"}).json()["registration_open"] is None
    # March 2027 is still the 2026 academic year; May 2027 has no calendar yet
    assert client.get("/v1/calendar", params={"date": "2027-03-10"}).json()["year"] == 2026
    later = client.get("/v1/calendar", params={"date": "2027-05-01"}).json()
    assert later["found"] is False and "schedule" in later["note"]
    assert client.get("/v1/calendar", params={"program": "大学院"}).json()["found"] is False


@needs_pdf
def test_calendar_chunks_are_the_events_and_the_admin_page_lists_them(client, published):
    hits = client.get("/v1/search", params={"q": "履修訂正期間", "source": "cal-site"}).json()["results"]
    assert any("2026-10-02〜2026-10-05 履修訂正期間" in h["text"] for h in hits)
    from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, log_in
    log_in(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    try:
        page = client.get(f"/admin/documents/{published['document_id']}/versions/{published['version_id']}")
        assert page.status_code == 200 and "学年暦（解析结果" in page.text and "履修登録期間" in page.text
    finally:
        client.cookies.clear()  # the client is shared: later tests expect no admin session


def test_page_text_is_cut_into_chunks_of_a_few_hundred_characters():
    from app.processing import _plain_text_chunks
    page = "\n".join(["見出し"] + ["本文の行です。" * 20] * 30 + ["", "あ" * 3000])
    parts = [t for _, t in _plain_text_chunks(page)]
    assert len(parts) > 5 and max(len(t) for t in parts) <= 1600
    assert "".join(t.replace("\n", "") for t in parts) == page.replace("\n", "")
