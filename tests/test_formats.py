"""Upload formats: Office Open XML, text-like files with their encodings, and zip archives with their limits."""
from __future__ import annotations

import io
import zipfile

from app import files
from app.parsers import office, text

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def ooxml(parts: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, xml in parts.items():
            z.writestr(name, xml)
    return buf.getvalue()


def make_docx() -> bytes:
    return ooxml({
        "word/document.xml": f"""<w:document {W}><w:body>
          <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>開発環境の準備</w:t></w:r></w:p>
          <w:p><w:r><w:t>Python 3.14 を</w:t></w:r><w:r><w:t>インストールします。</w:t></w:r></w:p>
          <w:tbl><w:tr><w:tc><w:p><w:r><w:t>OS</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Windows 11</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
          <w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>エディタ</w:t></w:r></w:p>
          <w:p><w:r><w:t>VS Code を使います。</w:t></w:r></w:p>
        </w:body></w:document>""",
        "docProps/core.xml": '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                             'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>初期設定ガイド</dc:title></cp:coreProperties>',
    })


def make_xlsx() -> bytes:
    S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    return ooxml({
        "xl/workbook.xml": f'<workbook {S} {R}><sheets><sheet name="時間割" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                      '<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="x"/></Relationships>',
        "xl/sharedStrings.xml": f"<sst {S}><si><t>曜日</t></si><si><t>科目</t></si><si><t>月</t></si><si><t>情報リテラシー</t></si></sst>",
        "xl/worksheets/sheet1.xml": f'<worksheet {S}><sheetData><row><c t="s"><v>0</v></c><c t="s"><v>1</v></c></row>'
                                    f'<row><c t="s"><v>2</v></c><c t="s"><v>3</v></c><c><v>2</v></c></row></sheetData></worksheet>',
    })


def make_pptx() -> bytes:
    A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    return ooxml({
        "ppt/slides/slide1.xml": f"<p:sld {A} xmlns:p='p'><a:p><a:r><a:t>第1回 ガイダンス</a:t></a:r></a:p></p:sld>",
        "ppt/slides/slide2.xml": f"<p:sld {A} xmlns:p='p'><a:p><a:r><a:t>課題は Moodle で提出</a:t></a:r></a:p></p:sld>",
        "ppt/notesSlides/notesSlide2.xml": f"<p:notes {A} xmlns:p='p'><a:p><a:r><a:t>締切は金曜日</a:t></a:r></a:p></p:notes>",
    })


def test_docx_headings_paragraphs_tables_title():
    title, sections = office.docx(make_docx())
    assert title == "初期設定ガイド"
    assert sections[0] == ("開発環境の準備", "開発環境の準備\nPython 3.14 をインストールします。\nOS | Windows 11")
    assert sections[1][0] == "エディタ" and "VS Code を使います。" in sections[1][1]


def test_xlsx_sheets_shared_strings_and_numbers():
    _, sections = office.xlsx(make_xlsx())
    assert sections == [("時間割", "曜日 | 科目\n月 | 情報リテラシー | 2")]


def test_pptx_slides_in_order_with_notes():
    _, sections = office.pptx(make_pptx())
    assert sections[0] == ("slide 1", "第1回 ガイダンス")
    assert sections[1][0] == "slide 2" and "課題は Moodle で提出" in sections[1][1] and "締切は金曜日" in sections[1][1]


def test_office_refuses_old_formats_and_dtds():
    import pytest
    with pytest.raises(office.OfficeError):
        office.docx(b"\xd0\xcf\x11\xe0 old binary .doc")
    evil = ooxml({"word/document.xml": f'<!DOCTYPE x [<!ENTITY a "aaaa">]><w:document {W}><w:body/></w:document>'})
    with pytest.raises(office.OfficeError):
        office.docx(evil)


def test_text_encodings_are_detected():
    assert text.decode("履修登録".encode("cp932")) == "履修登録"
    assert text.decode("履修登録".encode("euc_jp")) == "履修登録"
    assert text.decode("﻿履修".encode("utf-8")) == "履修"
    assert text.decode("履修".encode("utf-16")) == "履修"


def test_text_kinds_are_cleaned():
    title, body = text.extract("html", "<html><head><title>お知らせ</title><style>p{}</style></head><body><p>休講です</p>"
                                       "<script>x()</script><table><tr><td>月</td><td>1限</td></tr></table></body></html>".encode())
    assert title == "お知らせ" and "休講です" in body and "x()" not in body and "p{}" not in body and "月 | 1限" in body
    assert text.extract("csv", "曜日,科目\n月,数学\n".encode("cp932"))[1] == "曜日 | 科目\n月 | 数学"
    assert text.extract("tsv", b"a\tb\n")[1] == "a | b"
    assert text.extract("json", '{"科目": "数学", "時限": [1, 2]}'.encode())[1] == "科目: 数学\n時限: 1\n時限: 2"
    assert "締切" in text.extract("xml", "<r><item>締切</item></r>".encode())[1]
    rtf = r"{\rtf1\ansi\ansicpg932{\fonttbl{\f0 MS Gothic;}}\f0 \'97\'9a\'8f\'43\par next}"
    assert text.extract("rtf", rtf.encode())[1] == "履修\nnext"
    assert text.clean("a\r\n\r\n\r\n\r\nb\x00  ") == "a\n\nb"


def test_kind_of_names_and_mimes():
    assert files.kind_of("a.PDF", None) == "pdf"
    assert files.kind_of("x.docx", None) == "docx" and files.kind_of("x.doc", None) is None
    assert files.kind_of("notes.md", None) == "plain" and files.kind_of("t.csv", None) == "csv"
    assert files.kind_of("archive.zip", None) == "zip" and files.kind_of("photo.jpg", "image/jpeg") is None
    assert files.kind_of("noext", "text/html") == "html"


class _Cp932Info(zipfile.ZipInfo):
    """What Windows' Explorer writes: the name in CP932 bytes, no UTF-8 flag."""

    def _encodeFilenameFlags(self):
        return self.filename.encode("cp932"), self.flag_bits & ~0x800


def zip_of(entries: dict[str, bytes], *, cp932_names: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            info = (_Cp932Info if cp932_names else zipfile.ZipInfo)(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    return buf.getvalue()


def test_zip_expands_supported_files_and_reports_the_rest():
    inner = zip_of({"x.txt": b"x"})
    archive = zip_of({"資料/手順.txt": "手順です".encode("cp932"), "資料/guide.docx": make_docx(), "../../etc/passwd.txt": b"root",
                      "photo.jpg": b"\xff\xd8", "old.xls": b"\xd0\xcf", "inner.zip": inner, "__MACOSX/._x": b"", "dir/": b""},
                     cp932_names=True)
    out = files.Expanded()
    files.add(out, "pack.zip", archive, 10_000_000, 50_000_000)
    paths = {i.path for i in out.items}
    assert paths == {"pack/資料/手順.txt", "pack/資料/guide.docx", "pack/etc/passwd.txt"}      # .. dropped, CP932 names read
    reasons = dict(out.skipped)
    assert reasons == {"pack/photo.jpg": "skip.unsupported", "pack/old.xls": "skip.old_office", "pack/inner.zip": "skip.nested_zip"}
    docx = next(i for i in out.items if i.path.endswith(".docx"))
    assert docx.mime.endswith("wordprocessingml.document")


def test_zip_limits():
    bomb = zip_of({"zeros.txt": b"0" * 3_000_000})          # compresses about 1000:1
    out = files.Expanded()
    files.add(out, "b.zip", bomb, 10_000_000, 50_000_000)
    assert out.items == [] and out.skipped == [("b/zeros.txt", "skip.zip_bomb")]
    out = files.Expanded()
    files.add(out, "big.zip", zip_of({"a.txt": b"a" * 2000, "b.txt": b"b" * 2000}), 1500, 50_000)
    assert [r for _, r in out.skipped] == ["skip.too_large", "skip.too_large"]
    out = files.Expanded()
    files.add(out, "total.zip", zip_of({"a.txt": b"a" * 900, "b.txt": b"b" * 900}), 1000, 1500)
    assert [i.path for i in out.items] == ["total/a.txt"] and out.skipped == [("total/b.txt", "skip.total_limit")]
    out = files.Expanded()
    files.add(out, "broken.zip", b"PK\x03\x04 not really", 1000, 1000)
    assert out.skipped == [("broken.zip", "skip.bad_zip")]


def test_office_files_that_unpack_to_too_much_are_refused(monkeypatch):
    import pytest
    monkeypatch.setattr(office, "MAX_TOTAL", 1000)
    big = make_pptx()   # three small parts are fine …
    office.pptx(big)
    monkeypatch.setattr(office, "MAX_TOTAL", 100)
    with pytest.raises(office.OfficeError):
        office.pptx(big)  # … but not over the budget
