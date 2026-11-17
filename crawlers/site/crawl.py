"""Polite, resumable crawler for the school's official site; everything goes into one SQLite file.

Runs until stopped (Ctrl+C, or close the window: every fetch is committed on its own, so nothing is lost and the next
start continues where it stopped). The first pass takes every URL in sitemap.xml plus every same-site link found on
the pages (documents such as PDFs included, which the sitemap does not list); afterwards it re-reads the sitemap every
few hours, fetches pages whose lastmod changed at once and revisits the rest now and then with conditional requests.

The file keeps every distinct content a URL ever had (snapshots), not only the latest, so the import into school-api
can replay the history. Copy it while the crawler runs with `--snapshot` (snapshot.cmd), which uses SQLite's backup.

    python crawl.py                    # run until stopped
    python crawl.py --status           # counts, for a look while it runs
    python crawl.py --snapshot copy.sqlite
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import logging.handlers
import re
import signal
import sqlite3
import sys
import time
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup

from rules import (DOC_EXT, DOC_TYPES, HOSTS, MAX_BYTES, PRIORITY_WORDS, SITE, SITEMAP, SKIP_EXT, SKIP_PREFIXES, UA,  # noqa: F401
                   allowed, ext_of, kind_of, normalize)

DB_SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS urls (
  url              TEXT PRIMARY KEY,
  kind             TEXT NOT NULL,          -- page | file | skip (not text: images, archives, other hosts after redirect)
  found_via        TEXT NOT NULL,          -- start | sitemap | link
  referrer         TEXT,                   -- first page that linked it
  link_text        TEXT,                   -- its anchor text there (a PDF's best title)
  sitemap_lastmod  TEXT,
  first_found_at   TEXT NOT NULL,
  last_fetch_at    TEXT,
  next_fetch_at    TEXT NOT NULL,
  status           INTEGER,                -- last HTTP status (0: network error)
  error            TEXT,
  fails            INTEGER NOT NULL DEFAULT 0,
  etag             TEXT,
  last_modified    TEXT,
  final_url        TEXT,
  content_type     TEXT,
  current_fp       TEXT,                   -- fingerprint of the content last seen
  gone_at          TEXT                    -- 404/410 since then
);
CREATE INDEX IF NOT EXISTS urls_due ON urls (next_fetch_at);
-- one row per distinct content: fp = sha256 of the main text (pages: navigation and dynamic parts ignored) or of the
-- bytes (files); raw = the response body as fetched (zlib), text = the extracted main text of a page
CREATE TABLE IF NOT EXISTS contents (
  fp    TEXT PRIMARY KEY,
  raw   BLOB NOT NULL,
  size  INTEGER NOT NULL,
  text  TEXT
);
-- every content a URL had, with when it was first and last seen
CREATE TABLE IF NOT EXISTS snapshots (
  id             INTEGER PRIMARY KEY,
  url            TEXT NOT NULL,
  fp             TEXT NOT NULL,
  title          TEXT,
  published      TEXT,                     -- a date printed at the top of the page, if any (news)
  content_type   TEXT,
  first_seen_at  TEXT NOT NULL,
  last_seen_at   TEXT NOT NULL,
  UNIQUE (url, fp)
);
-- every request, for the record
CREATE TABLE IF NOT EXISTS fetches (
  id      INTEGER PRIMARY KEY,
  url     TEXT NOT NULL,
  at      TEXT NOT NULL,
  status  INTEGER,
  fp      TEXT,
  ms      INTEGER,
  error   TEXT
);
CREATE TABLE IF NOT EXISTS links (
  src   TEXT NOT NULL,
  dst   TEXT NOT NULL,
  text  TEXT,
  PRIMARY KEY (src, dst)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

log = logging.getLogger("crawl")
_stop = False


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ extraction
DATE_RE = re.compile(r"(20\d\d)\s*[./年-]\s*(\d{1,2})\s*[./月-]\s*(\d{1,2})")


def extract(html: str, url: str) -> tuple[str, str, str | None, list[tuple[str, str]]]:
    """(title, main text, published date, links) of a page. Links come from the whole page (menus included, so
    nothing is missed); the text only from the main column, so menus and footers do not count as content."""
    soup = BeautifulSoup(html, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        u = normalize(a["href"], url)
        if u:
            links.append((u, " ".join(a.get_text(" ", strip=True).split())[:300]))
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    title = re.sub(r"\s*[|｜]\s*湘南工科大学\s*$", "", title)
    for tag in soup(["script", "style", "noscript", "template", "iframe", "svg", "form"]):
        tag.decompose()
    main = soup.select_one("article") or soup.select_one(".main-contents") or soup.select_one("main") \
        or soup.select_one("#main") or soup.body or soup
    for tag in main.select("nav, header, footer, .breadcrumb, .pankuzu, .sns, .share"):
        tag.decompose()
    lines = [" ".join(t.split()) for t in main.get_text("\n").split("\n")]
    text = "\n".join(t for t in lines if t)
    published = None
    tm = main.find("time")
    if tm and DATE_RE.search(tm.get("datetime", "") or tm.get_text()):
        m = DATE_RE.search(tm.get("datetime", "") or tm.get_text())
    else:
        m = DATE_RE.search(text[:200])
    if m:
        try:
            published = datetime(int(m[1]), int(m[2]), int(m[3])).strftime("%Y-%m-%d")
        except ValueError:
            published = None
    return title, text, published, links


# ------------------------------------------------------------------ database
class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, timeout=60, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(DB_SCHEMA)

    def tidy(self) -> None:
        """URLs queued before a rule changed: duplicates of a page under another spelling (/x, /x/index.html) and
        excluded paths are not fetched again; their snapshots stay (the import merges them by URL)."""
        rows = self.db.execute("SELECT url, found_via, referrer, link_text, sitemap_lastmod FROM urls WHERE kind <> 'skip'").fetchall()
        self.db.execute("BEGIN")
        for r in rows:
            n = normalize(r["url"])
            if n is None or kind_of(r["url"]) == "skip":
                self.db.execute("UPDATE urls SET kind = 'skip', error = 'excluded path' WHERE url = ?", (r["url"],))
            elif n != r["url"]:
                self.db.execute("UPDATE urls SET kind = 'skip', error = ? WHERE url = ?", (f"same page as {n}", r["url"]))
                self.db.execute(
                    """INSERT INTO urls (url, kind, found_via, referrer, link_text, sitemap_lastmod, first_found_at, next_fetch_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (url) DO NOTHING""",
                    (n, kind_of(n), r["found_via"], r["referrer"], r["link_text"], r["sitemap_lastmod"], iso(now()), iso(now())))
        self.db.execute("COMMIT")

    def meta(self, key: str, value: str | None = None) -> str | None:
        if value is None:
            row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None
        self.db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                        (key, value))
        return value

    def add(self, url: str, via: str, *, referrer: str | None = None, text: str | None = None,
            lastmod: str | None = None, due: datetime | None = None) -> bool:
        """Queue a URL once. A sitemap lastmod newer than the last fetch makes it due now."""
        t = iso(due or now())
        cur = self.db.execute(
            """INSERT INTO urls (url, kind, found_via, referrer, link_text, sitemap_lastmod, first_found_at, next_fetch_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (url) DO NOTHING""",
            (url, kind_of(url), via, referrer, text, lastmod, iso(now()), t))
        if cur.rowcount:
            return True
        if lastmod:
            self.db.execute(
                """UPDATE urls SET sitemap_lastmod = ?,
                     next_fetch_at = CASE WHEN last_fetch_at IS NULL OR ? > last_fetch_at THEN ? ELSE next_fetch_at END
                   WHERE url = ? AND (sitemap_lastmod IS NULL OR sitemap_lastmod <> ?)""",
                (lastmod, lastmod_utc(lastmod), iso(now()), url, lastmod))
        return False

    def due(self) -> sqlite3.Row | None:
        # never fetched before anything else; pages before files; then whatever is due first
        important = " OR ".join(f"url LIKE '%{w}%' OR coalesce(link_text, '') LIKE '%{w}%'" for w in PRIORITY_WORDS)
        return self.db.execute(
            f"""SELECT * FROM urls WHERE kind <> 'skip' AND next_fetch_at <= ?
               ORDER BY last_fetch_at IS NOT NULL, NOT ({important}), kind = 'file', next_fetch_at LIMIT 1""", (iso(now()),)).fetchone()

    def next_due_at(self) -> datetime | None:
        row = self.db.execute("SELECT min(next_fetch_at) AS t FROM urls WHERE kind <> 'skip'").fetchone()
        return datetime.strptime(row["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if row["t"] else None


def lastmod_utc(s: str) -> str:
    try:
        return iso(datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc))
    except ValueError:
        return s


# ------------------------------------------------------------------ crawler
class Crawler:
    def __init__(self, store: Store, *, delay: float, page_days: float, file_days: float, sitemap_hours: float):
        self.s = store
        self.delay, self.page_days, self.file_days, self.sitemap_hours = delay, page_days, file_days, sitemap_hours
        self.http = httpx.Client(headers={"User-Agent": UA, "Accept-Language": "ja,en;q=0.5"}, timeout=httpx.Timeout(30, connect=15),
                                 follow_redirects=True)
        self.robots: RobotFileParser | None = None
        self.count = 0
        self.last_report = time.monotonic()

    # ---- sitemap and robots
    def refresh_sitemap(self) -> None:
        self.load_robots()
        added = seen = 0
        for loc, lastmod in self.sitemap_urls(SITEMAP, depth=0):
            u = normalize(loc)
            if u and allowed(u):
                seen += 1
                added += self.s.add(u, "sitemap", lastmod=lastmod)
        self.s.add(normalize(SITE), "start")
        self.s.meta("sitemap_read_at", iso(now()))
        log.info("sitemap: %d URLs, %d new", seen, added)

    def sitemap_urls(self, url: str, depth: int):
        try:
            r = self.http.get(url)
            r.raise_for_status()
            root = ElementTree.fromstring(r.content)
        except Exception as e:  # noqa: BLE001 - a broken sitemap must not stop the crawl
            log.warning("sitemap %s: %s", url, e)
            return
        for node in root:
            tag = node.tag.rsplit("}", 1)[-1]
            loc = next((c.text.strip() for c in node if c.tag.endswith("loc") and c.text), None)
            mod = next((c.text.strip() for c in node if c.tag.endswith("lastmod") and c.text), None)
            if not loc:
                continue
            if tag == "sitemap" and depth < 3:
                yield from self.sitemap_urls(loc, depth + 1)
            elif tag == "url":
                yield loc, mod

    def load_robots(self) -> None:
        try:
            r = self.http.get(urljoin(SITE, "/robots.txt"))
            if r.status_code == 200 and "text/plain" in r.headers.get("content-type", ""):
                rp = RobotFileParser()
                rp.parse(r.text.splitlines())
                self.robots = rp
                return
        except httpx.HTTPError:
            pass
        self.robots = None

    def robots_ok(self, url: str) -> bool:
        return self.robots is None or self.robots.can_fetch(UA, url)

    # ---- one fetch
    def fetch(self, row: sqlite3.Row) -> None:
        url, t0 = row["url"], time.monotonic()
        if not self.robots_ok(url):
            self.s.db.execute("UPDATE urls SET kind = 'skip', error = 'robots.txt' WHERE url = ?", (url,))
            return
        headers = {}
        if row["current_fp"]:
            if row["etag"]:
                headers["If-None-Match"] = row["etag"]
            if row["last_modified"]:
                headers["If-Modified-Since"] = row["last_modified"]
        at = iso(now())
        try:
            with self.http.stream("GET", url, headers=headers) as r:
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                final = normalize(str(r.url)) or url
                if r.status_code == 304:
                    return self.done(row, at, 304, row["current_fp"], t0, unchanged=True)
                if r.status_code in (429, 503):
                    wait = retry_after(r.headers.get("retry-after")) or 300
                    log.warning("%s %s: the site asks to slow down, pausing %d s", r.status_code, url, wait)
                    self.s.db.execute("UPDATE urls SET next_fetch_at = ? WHERE url = ?", (iso(now() + timedelta(seconds=wait)), url))
                    sleep(wait)
                    return
                if r.status_code >= 400:
                    return self.failed(row, at, r.status_code, f"HTTP {r.status_code}", t0)
                if not allowed(final):
                    return self.skip(url, at, r.status_code, f"redirects to another site: {final}", t0)
                is_html = ctype in ("text/html", "application/xhtml+xml")
                is_doc = ctype.startswith(DOC_TYPES) or (ctype in ("application/octet-stream", "") and kind_of(url) == "file")
                if not (is_html or is_doc):
                    return self.skip(url, at, r.status_code, f"not text ({ctype or 'no type'})", t0)
                body = bytearray()
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        return self.skip(url, at, r.status_code, f"larger than {MAX_BYTES // 1048576} MB", t0)
                raw = bytes(body)
                etag, lastmod = r.headers.get("etag"), r.headers.get("last-modified")
                enc = r.encoding or "utf-8"
        except httpx.HTTPError as e:
            return self.failed(row, at, 0, f"{type(e).__name__}: {e}"[:300], t0)

        if is_html:
            html = raw.decode(enc, errors="replace")
            title, text, published, links = extract(html, final)
            fp = hashlib.sha256(("page\n" + title + "\n" + text).encode("utf-8")).hexdigest()
            for dst, ltext in links:
                if dst == url:
                    continue
                self.s.db.execute("INSERT INTO links (src, dst, text) VALUES (?, ?, ?) ON CONFLICT DO NOTHING", (url, dst, ltext))
                if allowed(dst):
                    self.s.add(dst, "link", referrer=url, text=ltext or None)
            kind = "page"
        else:
            title, text, published = (row["link_text"] or final.rsplit("/", 1)[-1]), None, None
            fp = hashlib.sha256(raw).hexdigest()
            kind = "file"
        db = self.s.db
        db.execute("BEGIN")
        db.execute("INSERT INTO contents (fp, raw, size, text) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                   (fp, zlib.compress(raw, 6), len(raw), text))
        db.execute("""INSERT INTO snapshots (url, fp, title, published, content_type, first_seen_at, last_seen_at)
                      VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (url, fp) DO UPDATE SET last_seen_at = excluded.last_seen_at""",
                   (url, fp, title, published, ctype, at, at))
        db.execute("UPDATE urls SET kind = ?, etag = ?, last_modified = ?, final_url = ?, content_type = ? WHERE url = ?",
                   (kind, etag, lastmod, final if final != url else None, ctype, url))
        db.execute("COMMIT")
        changed = fp != row["current_fp"]
        self.done(row, at, 200, fp, t0, unchanged=not changed, kind=kind, title=title)

    def revisit(self, kind: str) -> datetime:
        return now() + timedelta(days=self.file_days if kind == "file" else self.page_days)

    def done(self, row, at, status, fp, t0, *, unchanged: bool, kind: str | None = None, title: str = "") -> None:
        ms = int((time.monotonic() - t0) * 1000)
        kind = kind or row["kind"]
        db = self.s.db
        db.execute("""UPDATE urls SET last_fetch_at = ?, next_fetch_at = ?, status = ?, error = NULL, fails = 0,
                        current_fp = ?, gone_at = NULL WHERE url = ?""", (at, iso(self.revisit(kind)), status, fp, row["url"]))
        if unchanged and fp:
            db.execute("UPDATE snapshots SET last_seen_at = ? WHERE url = ? AND fp = ?", (at, row["url"], fp))
        db.execute("INSERT INTO fetches (url, at, status, fp, ms) VALUES (?, ?, ?, ?, ?)", (row["url"], at, status, fp, ms))
        mark = "=" if unchanged else ("+" if not row["current_fp"] else "~")
        log.info("%s %s %s %s", mark, kind, row["url"], f"「{title[:40]}」" if title and not unchanged else "")

    def failed(self, row, at, status, error, t0) -> None:
        ms = int((time.monotonic() - t0) * 1000)
        fails = row["fails"] + 1
        gone = status in (404, 410)
        # gone pages are checked again weekly; errors back off: 2, 4, 8 … minutes, at most a day
        wait = timedelta(days=7) if gone else timedelta(minutes=min(2 ** fails, 1440))
        self.s.db.execute(
            """UPDATE urls SET last_fetch_at = ?, next_fetch_at = ?, status = ?, error = ?, fails = ?,
                 gone_at = CASE WHEN ? THEN COALESCE(gone_at, ?) ELSE gone_at END WHERE url = ?""",
            (at, iso(now() + wait), status, error, fails, gone, at, row["url"]))
        self.s.db.execute("INSERT INTO fetches (url, at, status, ms, error) VALUES (?, ?, ?, ?, ?)", (row["url"], at, status, ms, error))
        log.warning("! %s %s", row["url"], error)

    def skip(self, url, at, status, why, t0) -> None:
        ms = int((time.monotonic() - t0) * 1000)
        self.s.db.execute("UPDATE urls SET kind = 'skip', last_fetch_at = ?, status = ?, error = ? WHERE url = ?", (at, status, why, url))
        self.s.db.execute("INSERT INTO fetches (url, at, status, ms, error) VALUES (?, ?, ?, ?, ?)", (url, at, status, ms, why))
        log.info("- skip %s (%s)", url, why)

    # ---- main loop
    def run(self, max_requests: int | None = None) -> None:
        log.info("crawler started (delay %.1f s; pages revisited every %g days, files every %g days, sitemap every %g h)",
                 self.delay, self.page_days, self.file_days, self.sitemap_hours)
        while not _stop:
            read_at = self.s.meta("sitemap_read_at")
            if not read_at or now() - datetime.strptime(read_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) \
                    > timedelta(hours=self.sitemap_hours):
                self.refresh_sitemap()
            row = self.s.due()
            if row is None:
                nxt = self.s.next_due_at()
                wait = 300 if nxt is None else max(5, min(300, (nxt - now()).total_seconds()))
                self.report(force=True, idle=wait)
                sleep(wait)
                continue
            self.fetch(row)
            self.count += 1
            self.report()
            if max_requests and self.count >= max_requests:
                break
            sleep(self.delay)
        self.report(force=True)
        log.info("stopped after %d requests in this run", self.count)

    def report(self, *, force: bool = False, idle: float | None = None) -> None:
        if not force and time.monotonic() - self.last_report < 300:
            return
        self.last_report = time.monotonic()
        log.info("status: %s%s", summary(self.s), f"; nothing due, next check in {int(idle)} s" if idle else "")


def summary(s: Store) -> str:
    r = s.db.execute(
        """SELECT count(*) AS known,
                  sum(kind = 'page' AND current_fp IS NOT NULL) AS pages,
                  sum(kind = 'file' AND current_fp IS NOT NULL) AS files,
                  sum(kind <> 'skip' AND last_fetch_at IS NULL) AS pending,
                  sum(gone_at IS NOT NULL) AS gone,
                  sum(fails > 0 AND gone_at IS NULL) AS failing,
                  sum(kind = 'skip') AS skipped FROM urls""").fetchone()
    snaps = s.db.execute("SELECT count(*) FROM snapshots").fetchone()[0]
    mb = s.db.execute("SELECT coalesce(sum(size), 0) FROM contents").fetchone()[0] / 1048576
    return (f"{r['known']} URLs known, {r['pages'] or 0} pages and {r['files'] or 0} files stored, "
            f"{r['pending'] or 0} never fetched, {r['gone'] or 0} gone, {r['failing'] or 0} failing, "
            f"{r['skipped'] or 0} skipped, {snaps} snapshots, {mb:.0f} MB content")


def retry_after(v: str | None) -> int | None:
    if not v:
        return None
    if v.strip().isdigit():
        return min(int(v), 3600)
    try:
        from email.utils import parsedate_to_datetime
        return max(0, min(3600, int((parsedate_to_datetime(v) - now()).total_seconds())))
    except (TypeError, ValueError):
        return None


def sleep(seconds: float) -> None:
    end = time.monotonic() + seconds
    while not _stop and time.monotonic() < end:
        time.sleep(min(1.0, end - time.monotonic()))


def _on_signal(*_):
    global _stop
    if _stop:
        sys.exit(1)
    _stop = True
    log.info("stopping after the current request … (press again to quit at once)")


def setup_logging(dir_: Path) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(message)s", "%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(dir_ / "crawl.log", maxBytes=5 * 1048576, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh])
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Crawl the school's official site into SQLite (runs until stopped).")
    ap.add_argument("--db", type=Path, default=here / "data" / "site.sqlite")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default 1)")
    ap.add_argument("--page-days", type=float, default=7, help="revisit a page after this many days (default 7)")
    ap.add_argument("--file-days", type=float, default=14, help="revisit a PDF or other file after this many days (default 14)")
    ap.add_argument("--sitemap-hours", type=float, default=6, help="re-read sitemap.xml this often (default 6)")
    ap.add_argument("--max-requests", type=int, help="stop after this many requests (for a trial)")
    ap.add_argument("--status", action="store_true", help="print counts and exit")
    ap.add_argument("--snapshot", type=Path, metavar="FILE", help="write a consistent copy of the database and exit")
    a = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    a.db.parent.mkdir(parents=True, exist_ok=True)
    if a.status or a.snapshot:
        store = Store(a.db)
        if a.snapshot:
            out = sqlite3.connect(a.snapshot)
            store.db.backup(out)
            out.close()
            print(f"copied to {a.snapshot}")
        print(summary(store))
        return 0

    setup_logging(here / "logs")
    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _on_signal)
    store = Store(a.db)
    store.tidy()
    Crawler(store, delay=a.delay, page_days=a.page_days, file_days=a.file_days, sitemap_hours=a.sitemap_hours).run(a.max_requests)
    return 0


if __name__ == "__main__":
    sys.exit(main())
