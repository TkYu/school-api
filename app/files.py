"""Which uploaded files Tomo's search can use, and zip archives unpacked into their files.

Supported: PDF (text layer; scans without one give no text), Office Open XML (.docx .xlsx .pptx) and every text-like
format in parsers/text.py. A .zip is unpacked: each entry is handled like an uploaded file; nested archives, images,
old Office formats and anything else are skipped and reported. Limits keep a hostile archive harmless: entry count,
size per entry and in total (checked on the bytes actually read, not only on the archive's own claims), compression
ratio; entry names never reach the disk (files are stored by hash), so `../` in a name only becomes part of a key."""
from __future__ import annotations

import mimetypes
import posixpath
import zipfile
import io
from dataclasses import dataclass, field

from .parsers.text import KINDS as TEXT_KINDS, MIME_KINDS

OFFICE = {".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx"}
OFFICE_MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
OLD_OFFICE = {".doc", ".xls", ".ppt"}
ZIP_MAX_ENTRIES = 1000
ZIP_MAX_RATIO = 200          # uncompressed / compressed, for entries over 1 MB
JUNK = ("__MACOSX/", ".DS_Store", "Thumbs.db", "desktop.ini")


def ext_of(name: str) -> str:
    return posixpath.splitext((name or "").lower())[1]


def kind_of(name: str | None, mime: str | None) -> str | None:
    """'pdf', 'docx' / 'xlsx' / 'pptx', a text kind ('plain', 'csv', 'html' …), 'zip', or None (not supported)."""
    ext = ext_of(name or "")
    mime = (mime or "").split(";")[0].strip().lower()
    if ext == ".pdf" or mime == "application/pdf":
        return "pdf"
    if ext in OFFICE:
        return OFFICE[ext]
    for k, m in OFFICE_MIME.items():
        if mime == m:
            return k
    if ext == ".zip" or mime in ("application/zip", "application/x-zip-compressed"):
        return "zip"
    if ext in TEXT_KINDS:
        return TEXT_KINDS[ext]
    if mime in MIME_KINDS:
        return MIME_KINDS[mime]
    return None


def mime_of(name: str, kind: str) -> str:
    if kind in OFFICE_MIME:
        return OFFICE_MIME[kind]
    if kind == "pdf":
        return "application/pdf"
    return mimetypes.guess_type(name)[0] or "text/plain"


@dataclass
class Item:
    path: str          # the name inside the upload ("folder/file.pdf" for a zip entry)
    data: bytes
    mime: str


@dataclass
class Expanded:
    items: list[Item] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (path, reason key)
    total: int = 0


def clean_path(name: str) -> str:
    """A safe key part from a file or entry name: forward slashes, no empty / . / .. parts, no leading slash."""
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(parts)[:500] or "file"


def _entry_name(info: zipfile.ZipInfo) -> str:
    """Japanese Windows zips store names in CP932 without the UTF-8 flag; Python reads those as CP437."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("cp932")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def add(out: Expanded, name: str, data: bytes, max_file: int, max_total: int, *, in_zip: bool = False) -> None:
    path = clean_path(name)
    kind = kind_of(path, None) if in_zip else kind_of(path, mimetypes.guess_type(path)[0])
    if len(data) > max_file:
        out.skipped.append((path, "skip.too_large"))
        return
    if kind == "zip":
        if in_zip:
            out.skipped.append((path, "skip.nested_zip"))
            return
        _unzip(out, path, data, max_file, max_total)
        return
    if kind is None:
        out.skipped.append((path, "skip.old_office" if ext_of(path) in OLD_OFFICE else "skip.unsupported"))
        return
    if out.total + len(data) > max_total:
        out.skipped.append((path, "skip.total_limit"))
        return
    out.total += len(data)
    out.items.append(Item(path, data, mime_of(path, kind)))


def _unzip(out: Expanded, zip_path: str, data: bytes, max_file: int, max_total: int) -> None:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        out.skipped.append((zip_path, "skip.bad_zip"))
        return
    with z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) > ZIP_MAX_ENTRIES:
            out.skipped.append((zip_path, "skip.zip_too_many"))
            return
        stem = posixpath.splitext(zip_path)[0]
        for info in infos:
            name = _entry_name(info)
            path = f"{stem}/{clean_path(name)}"
            if any(j in name for j in JUNK):
                continue
            if info.flag_bits & 0x1:
                out.skipped.append((path, "skip.encrypted"))
                continue
            if info.file_size > max_file:
                out.skipped.append((path, "skip.too_large"))
                continue
            if info.file_size > 1024 * 1024 and info.compress_size and info.file_size / info.compress_size > ZIP_MAX_RATIO:
                out.skipped.append((path, "skip.zip_bomb"))
                continue
            if out.total + info.file_size > max_total:
                out.skipped.append((path, "skip.total_limit"))
                continue
            try:
                with z.open(info) as f:
                    entry = f.read(max_file + 1)    # never more than the limit, whatever the header claims
            except (zipfile.BadZipFile, NotImplementedError, OSError, RuntimeError):
                out.skipped.append((path, "skip.bad_zip"))
                continue
            add(out, path, entry, max_file, max_total, in_zip=True)
