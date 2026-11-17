"""Clean a crawl database into a new file (the original stays as it is), so the import sees each page once:

1. URLs under an excluded path (/digitalbook/) or of a skipped type go, with their snapshots;
2. spellings of one page (/x, /x/, /x/index.html, /a/../x/) are merged under the URL rules.normalize gives: their
   snapshots move to that URL (same content: first/last seen combined), the spelling is kept in `aliases`;
3. different URLs whose current content is identical (a teacher listed under two departments) keep one URL (the
   sitemap's, else the shortest); the others are kept in `aliases` with reason "same content".

    python dedupe.py data/site-20261109-1534.sqlite                # → data/site-20261109-1534.clean.sqlite
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from rules import kind_of, normalize


def clean(src: Path, dst: Path) -> dict[str, int]:
    if dst.exists():
        dst.unlink()
    with sqlite3.connect(src) as s:
        s.execute("VACUUM INTO ?", (str(dst),))
    db = sqlite3.connect(dst)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode = DELETE")
    db.execute("CREATE TABLE IF NOT EXISTS aliases (url TEXT PRIMARY KEY, primary_url TEXT NOT NULL, reason TEXT NOT NULL)")
    n = {"excluded": 0, "spellings": 0, "same_content": 0}

    # 1 + 2: one row per page under its normal spelling
    for r in db.execute("SELECT * FROM urls").fetchall():
        url = r["url"]
        canon = normalize(url)
        if canon is None or kind_of(canon) == "skip":
            if db.execute("SELECT 1 FROM snapshots WHERE url = ?", (url,)).fetchone() or (r["error"] or "").startswith("excluded"):
                db.execute("DELETE FROM snapshots WHERE url = ?", (url,))
                db.execute("DELETE FROM urls WHERE url = ?", (url,))
                n["excluded"] += 1
            continue
        if canon == url:
            continue
        for sn in db.execute("SELECT * FROM snapshots WHERE url = ?", (url,)).fetchall():
            db.execute(
                """INSERT INTO snapshots (url, fp, title, published, content_type, first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (url, fp) DO UPDATE SET
                     first_seen_at = min(first_seen_at, excluded.first_seen_at),
                     last_seen_at = max(last_seen_at, excluded.last_seen_at),
                     title = CASE WHEN excluded.last_seen_at > last_seen_at THEN excluded.title ELSE title END,
                     published = coalesce(published, excluded.published)""",
                (canon, sn["fp"], sn["title"], sn["published"], sn["content_type"], sn["first_seen_at"], sn["last_seen_at"]))
        db.execute("DELETE FROM snapshots WHERE url = ?", (url,))
        if not db.execute("SELECT 1 FROM urls WHERE url = ?", (canon,)).fetchone():
            cols = [k for k in r.keys() if k != "url"]
            db.execute(f"INSERT INTO urls (url, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
                       (canon, *[r[k] for k in cols]))
            db.execute("UPDATE urls SET kind = ?, error = NULL WHERE url = ?", (kind_of(canon), canon))
        else:  # keep what the spelling knew that the page's row does not
            db.execute("""UPDATE urls SET link_text = coalesce(link_text, ?), referrer = coalesce(referrer, ?),
                            sitemap_lastmod = coalesce(sitemap_lastmod, ?) WHERE url = ?""",
                       (r["link_text"], r["referrer"], r["sitemap_lastmod"], canon))
        db.execute("DELETE FROM urls WHERE url = ?", (url,))
        db.execute("INSERT OR REPLACE INTO aliases (url, primary_url, reason) VALUES (?, ?, 'spelling')", (url, canon))
        n["spellings"] += 1

    # every page's current content = its most recently seen snapshot
    db.execute("""UPDATE urls SET current_fp = (SELECT fp FROM snapshots s WHERE s.url = urls.url
                                                 ORDER BY last_seen_at DESC LIMIT 1)
                  WHERE EXISTS (SELECT 1 FROM snapshots s WHERE s.url = urls.url)""")
    db.execute("UPDATE urls SET kind = 'file' WHERE kind = 'page' AND EXISTS (SELECT 1 FROM snapshots s WHERE s.url = urls.url AND s.content_type NOT IN ('text/html', 'application/xhtml+xml'))")

    # 3: identical current content under different URLs
    for g in db.execute("""SELECT current_fp FROM urls WHERE kind <> 'skip' AND current_fp IS NOT NULL
                           GROUP BY current_fp HAVING count(*) > 1""").fetchall():
        rows = db.execute("SELECT url, found_via FROM urls WHERE current_fp = ? AND kind <> 'skip'", (g["current_fp"],)).fetchall()
        rows.sort(key=lambda x: (x["found_via"] != "sitemap", len(x["url"]), x["url"]))
        keep = rows[0]["url"]
        for x in rows[1:]:
            db.execute("DELETE FROM snapshots WHERE url = ?", (x["url"],))
            db.execute("DELETE FROM urls WHERE url = ?", (x["url"],))
            db.execute("INSERT OR REPLACE INTO aliases (url, primary_url, reason) VALUES (?, ?, 'same content')", (x["url"], keep))
            db.execute("UPDATE aliases SET primary_url = ? WHERE primary_url = ?", (keep, x["url"]))
            n["same_content"] += 1

    db.execute("DELETE FROM contents WHERE fp NOT IN (SELECT fp FROM snapshots)")
    db.commit()
    db.execute("VACUUM")
    for k, sql in (("pages", "SELECT count(*) FROM urls WHERE kind = 'page' AND current_fp IS NOT NULL"),
                   ("files", "SELECT count(*) FROM urls WHERE kind = 'file' AND current_fp IS NOT NULL"),
                   ("snapshots", "SELECT count(*) FROM snapshots"), ("contents", "SELECT count(*) FROM contents"),
                   ("aliases", "SELECT count(*) FROM aliases"),
                   ("duplicate_content_left", """SELECT count(*) FROM (SELECT current_fp FROM urls WHERE kind <> 'skip' AND current_fp IS NOT NULL
                                                  GROUP BY current_fp HAVING count(*) > 1)""")):
        n[k] = db.execute(sql).fetchone()[0]
    db.close()
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="Clean a crawl database into <name>.clean.sqlite")
    ap.add_argument("db", type=Path)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    out = a.out or a.db.with_name(a.db.stem + ".clean.sqlite")
    print(out, clean(a.db, out))


if __name__ == "__main__":
    main()
