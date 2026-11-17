# School site crawler

Crawls www.shonan-it.ac.jp into one SQLite file (`data/site.sqlite`); runs until stopped and continues where it
stopped. The file is copied back later and imported into school-api (importer: to do).

- `setup.cmd` — create `.venv` with uv (once, and after `uv.lock` changes)
- `start.cmd` — run until the window is closed or Ctrl+C (log in `logs/crawl.log`)
- `status.cmd` — counts so far
- `snapshot.cmd` — consistent copy `data/site-YYYYMMDD-HHMM.sqlite` while it runs (copying `site.sqlite` itself
  while the crawler runs can give a broken file: it uses WAL)

What it does: every URL in `sitemap.xml` (about 2,000, with lastmod) plus every same-site link found on the pages,
documents (PDF, Word, Excel, PowerPoint) included; images, media and archives are skipped. One request per second.
After the first pass: sitemap again every 6 h (changed lastmod → fetched at once), pages revisited every 7 days,
files every 14 days, with conditional requests (ETag / Last-Modified). 429/503 pause as long as the site asks.

- `dedupe.py` — clean copy of a crawl database for the import: spellings of one page merged (`/x`, `/x/`, `index.html`, `..`), excluded paths dropped, identical content under several URLs kept once (`aliases` table)

Tables: `urls` (state per URL), `contents` (each distinct body once, zlib; `text` = main column of a page),
`snapshots` (every content a URL had, first and last seen), `fetches` (every request), `links` (src → dst with anchor
text; a PDF's best title). Page fingerprints use the main column only, so a changed menu or footer is not a new
version.

Run it from a copy of this folder on any machine with Python (Windows: `setup.cmd`, then `start.cmd`).
