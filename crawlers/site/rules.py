"""URL rules of the school site crawl, shared by the crawler (crawl.py) and the clean-up of a crawl database
(dedupe.py): which hosts, which paths are skipped, and the one spelling every page URL is stored under."""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

SITE = "https://www.shonan-it.ac.jp/"
SITEMAP = "https://www.shonan-it.ac.jp/sitemap.xml"
HOSTS = {"www.shonan-it.ac.jp", "shonan-it.ac.jp"}
UA = "TomoSchoolCrawler/0.1 (read-only school information index; +https://tomo.gakuen.ac/)"

DOC_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".csv"}
SKIP_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tif", ".tiff", ".mp4", ".mov", ".avi",
            ".wmv", ".mp3", ".wav", ".m4a", ".zip", ".lzh", ".rar", ".7z", ".gz", ".exe", ".dmg", ".css", ".js", ".json",
            ".xml", ".woff", ".woff2", ".ttf", ".eot"}
# not worth a crawl: page-flip copies of printed brochures (one page per sheet; the brochures are elsewhere as text)
SKIP_PREFIXES = ("/digitalbook/",)
# fetched before everything else that is due: what students' questions depend on (the term calendar first)
PRIORITY_WORDS = ("schedule", "学年暦", "学事日程", "calendar", "時間割", "履修", "便覧", "要項", "ガイダンス", "syllabus", "シラバス")
DOC_TYPES = ("application/pdf", "application/msword", "application/vnd.", "text/plain", "text/csv")
MAX_BYTES = 80 * 1024 * 1024


def ext_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    return name[name.rfind("."):] if "." in name else ""


def normalize(url: str, base: str | None = None) -> str | None:
    """Absolute https URL without fragment and tracking parameters, or None if it is not a crawlable web link."""
    url = url.strip()
    if not url or url.startswith(("mailto:", "tel:", "javascript:", "data:", "#")):
        return None
    if base:
        url = urljoin(base, url)
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    host = p.hostname or ""
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                       if not k.lower().startswith(("utm_", "fbclid", "gclid"))])
    path = _dot_free(p.path or "/")
    if host in HOSTS:
        # one URL per page: /x, /x/ and /x/index.html are the same page on this site
        if path.endswith(("/index.html", "/index.htm")):
            path = path.rsplit("/", 1)[0] + "/"
        elif not path.endswith("/") and "." not in path.rsplit("/", 1)[-1]:
            path += "/"
    out = urlunsplit(("https" if host in HOSTS else p.scheme, p.netloc.lower(), path, query, ""))
    return out if len(out) <= 1000 else None


def _dot_free(path: str) -> str:
    """/faculties/../public/ → /public/ (RFC 3986 remove_dot_segments; urljoin keeps them in an absolute path)."""
    out: list[str] = []
    for seg in path.split("/"):
        if seg == "..":
            if len(out) > 1:
                out.pop()
        elif seg != ".":
            out.append(seg)
    if path.endswith(("/..", "/.")):
        out.append("")
    return "/".join(out) or "/"


def allowed(url: str) -> bool:
    return (urlsplit(url).hostname or "") in HOSTS


def kind_of(url: str) -> str:
    if urlsplit(url).path.startswith(SKIP_PREFIXES):
        return "skip"
    e = ext_of(urlsplit(url).path)
    if e in DOC_EXT:
        return "file"
    if e in SKIP_EXT:
        return "skip"
    return "page"
