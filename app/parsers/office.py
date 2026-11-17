"""Office Open XML (.docx, .xlsx, .pptx) to text with the standard library only (zip + XML): no LibreOffice, no
third-party parser. Old binary formats (.doc, .xls, .ppt) are not supported. Each function returns
(title or None, [(section, text), …]): docx sections follow its headings, xlsx one per sheet, pptx one per slide."""
from __future__ import annotations

import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
DC = "{http://purl.org/dc/elements/1.1/}"
MAX_XML = 64 * 1024 * 1024      # one part, uncompressed: guards against a zip bomb inside the file
MAX_TOTAL = 256 * 1024 * 1024   # all parts read from one file together
MAX_ROWS = 20000                # per sheet


class OfficeError(ValueError):
    pass


def _part(z: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_XML:
        raise OfficeError(f"{name} is too large ({info.file_size} bytes)")
    with z.open(info) as f:
        data = f.read(MAX_XML + 1)[:MAX_XML]
    z.read_total = getattr(z, "read_total", 0) + len(data)   # many parts can be a bomb as well as one big one
    if z.read_total > MAX_TOTAL:
        raise OfficeError("the file unpacks to too much data")
    return data


def _xml(z: zipfile.ZipFile, name: str) -> ET.Element | None:
    data = _part(z, name)
    if data is None:
        return None
    if b"<!DOCTYPE" in data[:4096] or b"<!ENTITY" in data[:4096]:
        raise OfficeError(f"{name}: a DTD is not allowed in Office XML")   # no entity tricks
    try:
        return ET.fromstring(data)   # stdlib ElementTree does not resolve external entities
    except ET.ParseError as e:
        raise OfficeError(f"{name}: {e}") from e


def _open(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise OfficeError("not an Office Open XML file (old .doc/.xls/.ppt are not supported)") from e


def _title(z: zipfile.ZipFile) -> str | None:
    core = _xml(z, "docProps/core.xml")
    el = core.find(f"{DC}title") if core is not None else None
    return el.text.strip() if el is not None and el.text and el.text.strip() else None


def _clean(text: str) -> str:
    return re.sub(r"[ \t　]+", " ", text).strip()


# ---------------------------------------------------------------- docx
def _para_text(p: ET.Element) -> str:
    out = []
    for el in p.iter():
        if el.tag == f"{W}t" and el.text:
            out.append(el.text)
        elif el.tag == f"{W}tab":
            out.append("\t")
        elif el.tag in (f"{W}br", f"{W}cr"):
            out.append("\n")
    return _clean("".join(out))


def _heading_level(p: ET.Element) -> int | None:
    style = p.find(f"{W}pPr/{W}pStyle")
    name = (style.get(f"{W}val") or "") if style is not None else ""
    m = re.match(r"(?i)(heading|見出し|title)\s*(\d*)", name)
    if not m:
        return None
    return int(m.group(2)) if m.group(2) else 1


def docx(data: bytes) -> tuple[str | None, list[tuple[str, str]]]:
    with _open(data) as z:
        root = _xml(z, "word/document.xml")
        if root is None:
            raise OfficeError("no word/document.xml")
        title = _title(z)
    body = root.find(f"{W}body")
    sections: list[tuple[str, str]] = []
    heading, lines = "", []

    def flush():
        if lines:
            sections.append((heading, "\n".join(lines)))
            lines.clear()

    for el in (body if body is not None else []):
        if el.tag == f"{W}p":
            text = _para_text(el)
            if not text:
                continue
            if _heading_level(el) is not None:
                flush()
                heading = text[:120]
                lines.append(text)
            else:
                lines.append(text)
        elif el.tag == f"{W}tbl":
            for tr in el.iter(f"{W}tr"):
                cells = [" ".join(_para_text(p) for p in tc.iter(f"{W}p")).strip() for tc in tr.iter(f"{W}tc")]
                if any(cells):
                    lines.append(" | ".join(cells))
    flush()
    return title, sections


# ---------------------------------------------------------------- xlsx
def _rels(z: zipfile.ZipFile, part: str) -> dict[str, str]:
    folder, name = posixpath.split(part)
    root = _xml(z, posixpath.join(folder, "_rels", name + ".rels"))
    out = {}
    for r in (root if root is not None else []):
        target = r.get("Target") or ""
        out[r.get("Id")] = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
    return out


def xlsx(data: bytes) -> tuple[str | None, list[tuple[str, str]]]:
    with _open(data) as z:
        wb = _xml(z, "xl/workbook.xml")
        if wb is None:
            raise OfficeError("no xl/workbook.xml")
        title = _title(z)
        shared: list[str] = []
        sst = _xml(z, "xl/sharedStrings.xml")
        for si in (sst if sst is not None else []):
            shared.append("".join(t.text or "" for t in si.iter(f"{S}t")))
        rels = _rels(z, "xl/workbook.xml")
        sections = []
        for sheet in wb.iter(f"{S}sheet"):
            target = rels.get(sheet.get(f"{R}id") or "")
            root = _xml(z, target) if target else None
            if root is None:
                continue
            rows = []
            for n, row in enumerate(root.iter(f"{S}row")):
                if n >= MAX_ROWS:
                    break
                cells = []
                for c in row.iter(f"{S}c"):
                    kind, v = c.get("t"), c.find(f"{S}v")
                    if kind == "s" and v is not None and v.text and v.text.isdigit() and int(v.text) < len(shared):
                        cells.append(shared[int(v.text)])
                    elif kind == "inlineStr":
                        cells.append("".join(t.text or "" for t in c.iter(f"{S}t")))
                    elif v is not None and v.text:
                        cells.append(v.text)
                cells = [_clean(x) for x in cells]
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                sections.append((sheet.get("name") or "", "\n".join(rows)))
    return title, sections


# ---------------------------------------------------------------- pptx
def pptx(data: bytes) -> tuple[str | None, list[tuple[str, str]]]:
    with _open(data) as z:
        title = _title(z)
        slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                        key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
        sections = []
        for no, name in enumerate(slides, start=1):
            root = _xml(z, name)
            paras = []
            for p in (root.iter(f"{A}p") if root is not None else []):
                text = _clean("".join(t.text or "" for t in p.iter(f"{A}t")))
                if text:
                    paras.append(text)
            notes = _xml(z, f"ppt/notesSlides/notesSlide{name.rsplit('slide', 1)[1]}")
            if notes is not None:
                note = [_clean("".join(t.text or "" for t in p.iter(f"{A}t"))) for p in notes.iter(f"{A}p")]
                note = [x for x in note if x and not x.isdigit()]   # the slide number placeholder
                if note:
                    paras.append("（ノート）" + " ".join(note))
            if paras:
                sections.append((f"slide {no}", "\n".join(paras)))
    return title, sections
