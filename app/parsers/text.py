"""Text-like files to clean text: plain text and Markdown, CSV / TSV, HTML, XML, JSON, RTF, subtitles and similar.
Japanese files are often Shift_JIS (CP932) or EUC-JP rather than UTF-8: the encoding is detected, never assumed."""
from __future__ import annotations

import codecs
import csv
import html
import io
import json
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

# extension -> how the text is read
KINDS = {
    ".txt": "plain", ".text": "plain", ".md": "plain", ".markdown": "plain", ".rst": "plain", ".log": "plain",
    ".ini": "plain", ".cfg": "plain", ".conf": "plain", ".yaml": "plain", ".yml": "plain", ".toml": "plain",
    ".tex": "plain", ".srt": "plain", ".vtt": "plain", ".py": "plain", ".js": "plain", ".ts": "plain", ".java": "plain",
    ".c": "plain", ".h": "plain", ".cpp": "plain", ".sh": "plain", ".bat": "plain", ".ps1": "plain", ".sql": "plain",
    ".r": "plain", ".m": "plain", ".css": "plain",
    ".csv": "csv", ".tsv": "tsv",
    ".html": "html", ".htm": "html", ".xhtml": "html",
    ".xml": "xml", ".json": "json", ".rtf": "rtf",
}
MIME_KINDS = {"text/plain": "plain", "text/markdown": "plain", "text/csv": "csv", "text/tab-separated-values": "tsv",
              "text/html": "html", "application/xhtml+xml": "html", "application/xml": "xml", "text/xml": "xml",
              "application/json": "json", "application/rtf": "rtf", "text/rtf": "rtf"}


def decode(data: bytes) -> str:
    """UTF-8 (with or without BOM), UTF-16 with BOM, else CP932 (Shift_JIS), else EUC-JP; never fails."""
    if data.startswith(codecs.BOM_UTF8):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # CP932 and EUC-JP often both decode the same bytes; the wrong one gives half-width katakana and rare kanji
    candidates = []
    for enc in ("cp932", "euc_jp"):
        try:
            candidates.append((_japaneseness(data.decode(enc)), enc))
        except UnicodeDecodeError:
            continue
    if candidates:
        return data.decode(max(candidates)[1])
    return data.decode("utf-8", errors="replace")


_COMMON = re.compile(r"[\u3040-\u30ff\u3000-\u303f\uff01-\uff5e\u4e00-\u9fff。、]")
_HALFWIDTH = re.compile(r"[\uff61-\uff9f]")


def _japaneseness(text: str) -> int:
    """Hiragana, katakana, CJK and full-width punctuation count for; half-width katakana (rare in real text, typical of
    EUC-JP read as CP932) counts against."""
    return len(_COMMON.findall(text)) - 3 * len(_HALFWIDTH.findall(text))


def clean(text: str) -> str:
    """Line ends, control characters, trailing spaces and runs of blank lines."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class _HTML(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "ul", "ol",
             "dt", "dd", "pre", "blockquote", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.title: str | None = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title and data.strip():
            self.title = (self.title or "") + data.strip()
        if not self.skip:
            self.out.append(data)


def _html(text: str) -> tuple[str | None, str]:
    p = _HTML()
    p.feed(text)
    p.close()
    body = "\n".join(re.sub(r"[ \t　]+", " ", line).strip(" |") for line in "".join(p.out).split("\n"))
    return p.title, body


def _rtf(text: str) -> str:
    """Enough of RTF for its text: skips destinations ({\\*…}, font and colour tables), decodes \\'hh and \\uN."""
    cp = "cp932" if re.search(r"\\ansicpg932", text) else "cp1252"
    out, stack, skip, i = [], [], False, 0
    pending: list[int] = []

    def flush():
        if pending:
            out.append(bytes(pending).decode(cp, errors="replace"))
            pending.clear()

    while i < len(text):
        ch = text[i]
        if ch == "{":
            stack.append(skip)
            if text.startswith("{\\*", i) or re.match(r"\{\\(fonttbl|colortbl|stylesheet|info|pict|header|footer)\b", text[i:i + 14]):
                skip = True
            i += 1
        elif ch == "}":
            flush()
            skip = stack.pop() if stack else False
            i += 1
        elif ch == "\\":
            m = re.match(r"\\'([0-9a-fA-F]{2})", text[i:i + 4])
            if m:
                if not skip:
                    pending.append(int(m.group(1), 16))
                i += 4
                continue
            flush()
            m = re.match(r"\\([a-zA-Z]+)(-?\d+)? ?", text[i:])
            if m:
                word, arg = m.group(1), m.group(2)
                if not skip:
                    if word in ("par", "line"):
                        out.append("\n")
                    elif word == "tab":
                        out.append("\t")
                    elif word == "u" and arg:
                        out.append(chr(int(arg) % 65536))
                        # the replacement character after \uN
                        rest = text[i + m.end():]
                        if rest.startswith("\\'"):
                            i += 4
                        elif rest[:1] and rest[:1] not in "\\{}":
                            i += 1
                i += m.end()
            else:
                if not skip and i + 1 < len(text) and text[i + 1] in "\\{}":
                    out.append(text[i + 1])
                i += 2
        else:
            flush()
            if not skip and ch not in "\r\n":
                out.append(ch)
            i += 1
    flush()
    return "".join(out)


def _json(text: str) -> str:
    lines: list[str] = []

    def walk(v, path=""):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, f"{path}.{k}" if path else str(k))
        elif isinstance(v, list):
            for x in v:
                walk(x, path)
        elif v is not None and str(v).strip():
            lines.append(f"{path}: {v}" if path else str(v))
    walk(json.loads(text))
    return "\n".join(lines)


def extract(kind: str, data: bytes) -> tuple[str | None, str]:
    """(title or None, clean text)."""
    text = decode(data)
    title = None
    if kind in ("csv", "tsv"):
        rows = csv.reader(io.StringIO(text), delimiter="\t" if kind == "tsv" else ",")
        text = "\n".join(" | ".join(c.strip() for c in row) for row in rows if any(c.strip() for c in row))
    elif kind == "html":
        title, text = _html(text)
    elif kind == "xml":
        if "<!DOCTYPE" in text[:4096] or "<!ENTITY" in text[:4096]:
            text = re.sub(r"<[^>]+>", " ", text)          # no DTD processing: plain tag stripping
        else:
            try:
                text = "\n".join(x.strip() for x in ET.fromstring(text.encode("utf-8")).itertext() if x.strip())
            except ET.ParseError:
                text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
    elif kind == "json":
        try:
            text = _json(text)
        except ValueError:
            pass
    elif kind == "rtf":
        text = _rtf(text)
    return title, clean(text)
