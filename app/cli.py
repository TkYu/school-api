"""school-api migrate | ingest-files | ingest-site | ingest-bbs | embed-remote

Accounts are made in the web UI (/admin/signup; the first confirmed one is the admin). These commands are the
admin's own tools; what they submit belongs to the admin.

ingest-files runs inside the api container and talks to the API over its own Unix socket with INGEST_TOKEN from
the environment, so the token never leaves the server:
  docker compose exec api school-api ingest-files --source shonan-it-syllabus --label 2026 --key-prefix 2026/ \
      [--kind syllabus] [--full] [--publish] [--remote-embed] FILE…

ingest-site reads a cleaned crawl database of the official site (crawlers/site: crawl.py, then dedupe.py) in the
api container: every page with its text, every PDF with its bytes, each URL's snapshots oldest first (history), the
category from the URL path, the link text as a PDF's title, merged spellings as `also_at`:
  docker compose exec api school-api ingest-site --source shonan-it-site --label 2026-11-09 [--only '%schedule%'] \
      [--full] [--publish] [--remote-embed] /data/files/imports/site.clean.sqlite

embed-remote runs on a machine with a GPU (Apple: --device mps): it takes the texts without vectors from the API,
computes them with the server's model and revision, and sends them back. The token comes from SCHOOL_API_TOKEN or
--token-file, never from the command line:
  school-api embed-remote --api https://api.example.ac.jp --token-file ~/.config/school-api/token --device mps
"""
from __future__ import annotations

import argparse
import base64
import mimetypes
import os
import sys
from pathlib import Path

from .config import conninfo, settings
from .migrate import migrate

SOCKET = "/run/api/api.sock"


def _client():
    import httpx
    token = os.environ.get("INGEST_TOKEN") or sys.exit("INGEST_TOKEN is not set")
    return httpx.Client(transport=httpx.HTTPTransport(uds=SOCKET), base_url="http://api",
                        headers={"Authorization": f"Bearer {token}"}, timeout=120)


def ingest_files(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="school-api ingest-files")
    p.add_argument("--source", required=True)
    p.add_argument("--kind", default="syllabus", choices=["syllabus", "site", "bbs", "upload"])
    p.add_argument("--label", required=True, help="the batch, e.g. 2026 or 2026後期")
    p.add_argument("--key-prefix", default="", help="prepended to the file name stem: 2026/ -> key 2026/S-172R09-01")
    p.add_argument("--url-template", default="", help="e.g. https://…/{stem}.pdf (the public URL of each file)")
    p.add_argument("--full", action="store_true", help="a complete set: what it does not contain counts as gone")
    p.add_argument("--publish", action="store_true", help="publish at once (else it waits in staging)")
    p.add_argument("--remote-embed", action="store_true", help="vectors are computed outside (embed-remote), not by the worker")
    p.add_argument("files", nargs="+", type=Path)
    a = p.parse_args(argv)
    with _client() as c:
        c.put(f"/v1/admin/sources/{a.source}", json={"name": a.source, "kind": a.kind}).raise_for_status()
        b = c.post("/v1/admin/batches", json={"source": a.source, "label": a.label, "kind": "full" if a.full else "partial",
                                              "embed": "remote" if a.remote_embed else "worker"})
        b.raise_for_status()
        bid = b.json()["id"]
        changed = 0
        for f in a.files:
            data = f.read_bytes()
            body = {"key": a.key_prefix + f.stem, "file_name": f.name, "mime": mimetypes.guess_type(f.name)[0] or "application/octet-stream",
                    "raw_base64": base64.b64encode(data).decode(), "url": a.url_template.format(stem=f.stem, name=f.name) or None}
            r = c.put(f"/v1/admin/batches/{bid}/documents", json=body)
            r.raise_for_status()
            changed += r.json()["changed"]
            print(f"{f.name}: {'new version' if r.json()['changed'] else 'unchanged'}")
        r = c.post(f"/v1/admin/batches/{bid}/finish", json={"publish": a.publish})
        r.raise_for_status()
        print(f"batch {bid}: {len(a.files)} files, {changed} new versions, {r.json()['status']}")


# the official site's sections by the first part of the URL path (shown with a hit; search can filter by it)
SITE_SECTIONS = {
    "campuslife": "学生生活", "for-students": "在学生の方へ", "career": "就職・キャリア", "library": "図書館", "mediaict": "メディア・ICT",
    "faculty": "学部・学科", "faculties": "学部・学科", "teachers": "教員紹介", "examination": "入試情報", "for-examinee": "受験生の方へ",
    "open-campus": "オープンキャンパス", "outline": "大学概要", "public": "情報公開", "basic-information": "情報公開",
    "topics": "ニュース", "event": "イベント", "for-guardian": "保護者の方へ", "for-general": "一般の方へ", "for-alumni": "卒業生の方へ",
    "dousoukai": "同窓会", "for-business": "企業の方へ", "soumu": "総務", "jimu": "事務", "special": "特集", "concept": "特集",
}


def _section(url: str | None) -> str | None:
    from urllib.parse import urlsplit
    first = urlsplit(url or "").path.strip("/").split("/")[0]
    return SITE_SECTIONS.get(first)


def _link_title(text: str | None) -> str | None:
    """A link text as a title: without the size note the site adds ("R8学年暦（学部） （PDF ファイル 0.11MB）")."""
    import re
    if not text:
        return None
    return re.sub(r"\s*[（(]\s*PDF\s*ファイル[^）)]*[）)]\s*$", "", text).strip() or None


def site_date(url: str, published: str | None, lastmod: str | None) -> str | None:
    """The date a hit shows (and old news ranks by): a news page's date from its URL (/topics/20211102_05/), which the
    site never changes; another page's last change from the sitemap. A date found in the page text is used only for
    news without a date in the URL: in other pages it is often a date the text talks about ("2004年4月1日に設立")."""
    import re
    from datetime import date
    if m := re.search(r"/topics/(\d{4})(\d{2})(\d{2})", url):
        try:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            return published
    return (lastmod or "")[:10] or None


def ingest_site(argv: list[str]) -> None:
    import sqlite3
    import zlib
    p = argparse.ArgumentParser(prog="school-api ingest-site")
    p.add_argument("--source", required=True)
    p.add_argument("--label", required=True, help="the batch, e.g. the crawl date")
    p.add_argument("--only", default="", help="SQL LIKE pattern on the URL, e.g. %%schedule%% (a partial batch)")
    p.add_argument("--full", action="store_true", help="the whole site: what it does not contain counts as gone")
    p.add_argument("--publish", action="store_true")
    p.add_argument("--remote-embed", action="store_true")
    p.add_argument("db", type=Path)
    a = p.parse_args(argv)
    if a.full and a.only:
        sys.exit("--full and --only exclude each other")
    db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    aliases: dict[str, list[str]] = {}
    for r in db.execute("SELECT url, primary_url FROM aliases"):
        aliases.setdefault(r["primary_url"], []).append(r["url"])
    titles = {r["dst"]: r["text"] for r in db.execute("SELECT dst, text FROM links WHERE text <> '' ORDER BY length(text) DESC")}
    urls = db.execute("""SELECT * FROM urls WHERE kind IN ('page', 'file') AND current_fp IS NOT NULL AND url LIKE ? ORDER BY url""",
                      (a.only or "%",)).fetchall()
    counts = {"documents": 0, "versions": 0, "unchanged": 0, "skipped": 0}
    with _client() as c:
        c.put(f"/v1/admin/sources/{a.source}", json={"name": a.source, "kind": "site"}).raise_for_status()
        b = c.post("/v1/admin/batches", json={"source": a.source, "label": a.label, "kind": "full" if a.full else "partial",
                                              "embed": "remote" if a.remote_embed else "worker"})
        b.raise_for_status()
        bid = b.json()["id"]
        for i, u in enumerate(urls, 1):
            snaps = db.execute("""SELECT s.*, c.raw, c.text FROM snapshots s JOIN contents c USING (fp) WHERE s.url = ?
                                  ORDER BY s.first_seen_at""", (u["url"],)).fetchall()
            is_file = u["kind"] == "file"
            if is_file and not (u["content_type"] or "").startswith("application/pdf"):
                counts["skipped"] += 1  # Word / Excel: no parser yet
                continue
            counts["documents"] += 1
            for sn in snaps:
                body: dict = {"key": u["url"], "url": u["url"], "lang": "ja", "mime": sn["content_type"] or u["content_type"],
                              "fetched_at": sn["first_seen_at"],
                              "meta": {k: v for k, v in {
                                  "category": _section(u["referrer"] if is_file else u["url"]),
                                  "linked_from": u["referrer"] if is_file else None, "link_text": _link_title(u["link_text"] or titles.get(u["url"])),
                                  "also_at": aliases.get(u["url"]), "sitemap_lastmod": u["sitemap_lastmod"],
                                  "last_seen": sn["last_seen_at"]}.items() if v}}
                if is_file:
                    name = u["url"].rsplit("/", 1)[-1]
                    body.update(title=(_link_title(u["link_text"] or titles.get(u["url"])) or name), file_name=name,
                                raw_base64=base64.b64encode(zlib.decompress(sn["raw"])).decode())
                else:
                    body.update(title=sn["title"], text=sn["text"] or "",
                                info_date=site_date(u["url"], sn["published"], u["sitemap_lastmod"]))
                r = c.put(f"/v1/admin/batches/{bid}/documents", json=body)
                r.raise_for_status()
                counts["versions" if r.json()["changed"] else "unchanged"] += 1
            if i % 200 == 0:
                print(f"{i}/{len(urls)} URLs …", flush=True)
        r = c.post(f"/v1/admin/batches/{bid}/finish", json={"publish": a.publish})
        r.raise_for_status()
        print(f"batch {bid}: {counts}, {r.json()['status']}")


def _encode(model, texts: list[str], device: str, batch_size: int = 16) -> list[list[float]]:
    """Vectors for texts; on a GPU out of memory (an 8 GB Mac shares it with everything else) the cache is freed and
    the batch halved, down to one text at a time, instead of ending the run."""
    import torch
    while True:
        try:
            out = model.model.encode(texts, batch_size=batch_size, normalize_embeddings=True).tolist()
            if device == "mps":
                torch.mps.empty_cache()  # the allocator keeps freed blocks; over thousands of batches that ran out
            return out
        except torch.OutOfMemoryError:
            if device == "mps":
                torch.mps.empty_cache()
            if batch_size == 1:
                raise
            batch_size //= 2
            print(f"out of GPU memory: batch size {batch_size}", flush=True)


def ingest_bbs(argv: list[str]) -> None:
    """A file from crawlers/bbs/fetch_bbs.py plus its translations: one document per topic (key = topic URL), the
    topic as a JSON file with each post's Japanese translation, the readable text as body. No user names."""
    import hashlib
    import json

    from .parsers import bbs
    p = argparse.ArgumentParser(prog="school-api ingest-bbs")
    p.add_argument("--source", default="sitoss-bbs")
    p.add_argument("--label", required=True)
    p.add_argument("--topics", default="", help="only these topic ids, comma-separated")
    p.add_argument("--translations", type=Path, action="append", default=[], help="JSON with posts.<id>.ja (+ raw_sha256)")
    p.add_argument("--full", action="store_true")
    p.add_argument("--publish", action="store_true")
    p.add_argument("--remote-embed", action="store_true")
    p.add_argument("file", type=Path)
    a = p.parse_args(argv)
    data = json.loads(a.file.read_text(encoding="utf-8"))
    want = {int(x) for x in a.topics.split(",") if x.strip()}
    titles: dict[str, str] = {}
    trans: dict[str, dict] = {}
    for f in a.translations:
        t = json.loads(f.read_text(encoding="utf-8"))
        titles.update(t.get("topics") or {})
        trans.update(t.get("posts") or {})
    counts = {"topics": 0, "versions": 0, "unchanged": 0, "translated_posts": 0, "untranslated_zh": 0}
    with _client() as c:
        c.put(f"/v1/admin/sources/{a.source}", json={"name": a.source, "kind": "bbs"}).raise_for_status()
        b = c.post("/v1/admin/batches", json={"source": a.source, "label": a.label, "kind": "full" if a.full else "partial",
                                              "embed": "remote" if a.remote_embed else "worker"})
        b.raise_for_status()
        bid = b.json()["id"]
        for t in data["topics"]:
            if want and t["id"] not in want:
                continue
            posts = []
            for post in t["posts"]:
                tr = trans.get(str(post["id"]))
                ja = None
                if tr and tr.get("raw_sha256") in (None, hashlib.sha256(post["raw"].encode()).hexdigest()):
                    ja = tr["ja"]
                    counts["translated_posts"] += 1
                elif post.get("lang") == "zh":
                    counts["untranslated_zh"] += 1  # edited since it was translated, or never translated
                posts.append({"post_number": post["post_number"], "created_at": post["created_at"], "updated_at": post["updated_at"],
                              "reply_to": post.get("reply_to"), "staff": post.get("staff"), "lang": post.get("lang"), "raw": post["raw"],
                              **({"ja": ja} if ja else {})})
            topic = {"title": t["title"], "title_ja": titles.get(str(t["id"])), "category": t["category"], "posts": posts}
            raw = json.dumps(topic, ensure_ascii=False, indent=1).encode("utf-8")
            body = {"key": t["url"].rsplit("/", 2)[0] + f"/{t['id']}", "url": t["url"], "title": t["title"], "lang": "zh" if any(x["lang"] == "zh" for x in posts) else "ja",
                    "info_date": (t.get("last_posted_at") or t["created_at"])[:10], "mime": "application/json", "file_name": f"topic-{t['id']}.json",
                    "raw_base64": base64.b64encode(raw).decode(), "text": bbs.readable(topic), "fetched_at": data["fetched_at"],
                    "meta": {"category": "コミュニティ（BBS）", "bbs_category": t["category"], "title_ja": titles.get(str(t["id"])),
                             "community": True, "views": t.get("views"), "likes": t.get("like_count"), "posts": len(posts)}}
            r = c.put(f"/v1/admin/batches/{bid}/documents", json=body)
            r.raise_for_status()
            counts["topics"] += 1
            counts["versions" if r.json()["changed"] else "unchanged"] += 1
        r = c.post(f"/v1/admin/batches/{bid}/finish", json={"publish": a.publish})
        r.raise_for_status()
        print(f"batch {bid}: {counts}, {r.json()['status']}")


def embed_remote(argv: list[str]) -> None:
    import time

    import httpx

    from .embed import RURI, RURI_REVISION, Embedder
    p = argparse.ArgumentParser(prog="school-api embed-remote")
    p.add_argument("--api", required=True)
    p.add_argument("--token-file", type=Path)
    p.add_argument("--device", default="cpu", help="cpu, mps (Apple GPU) or cuda")
    p.add_argument("--batch", type=int, default=64, help="vectors per upload")
    a = p.parse_args(argv)
    token = (a.token_file.expanduser().read_text().strip() if a.token_file else os.environ.get("SCHOOL_API_TOKEN", ""))
    if not token:
        sys.exit("no token: --token-file or SCHOOL_API_TOKEN")
    client = httpx.Client(base_url=a.api.rstrip("/"), headers={"Authorization": f"Bearer {token}", "User-Agent": "school-api embed-remote"}, timeout=120)
    model = Embedder(device=a.device)
    done, started, after = 0, time.time(), 0
    while True:
        page = client.get("/v1/admin/embeddings/pending", params={"after_id": after, "limit": a.batch})
        page.raise_for_status()
        data = page.json()
        if data["model"] != RURI or data["revision"] != RURI_REVISION:
            sys.exit(f"the server uses {data['model']}@{data['revision']}, this client {RURI}@{RURI_REVISION}: update one of them")
        texts = data["texts"]
        if not texts:
            if after == 0 or data["pending"] == 0:
                break
            after = 0  # texts added meanwhile: one more pass from the start
            continue
        vectors = _encode(model, [data["prefix"] + t["search_text"] for t in texts], a.device)
        r = client.put("/v1/admin/embeddings", json={"model": RURI, "revision": RURI_REVISION,
                                                     "vectors": [{"text_id": t["text_id"], "embedding": v} for t, v in zip(texts, vectors)]})
        r.raise_for_status()
        done += r.json()["stored"]
        after = texts[-1]["text_id"]
        rate = done / max(time.time() - started, 1e-6)
        print(f"{done} vectors · {rate:.1f}/s · about {max(data['pending'] - len(texts), 0)} left", flush=True)
    print(f"done: {done} vectors in {time.time() - started:.0f}s")


def main() -> None:
    cmd, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    if cmd == "migrate":
        done = migrate(conninfo(settings()))
        print("applied: " + (", ".join(done) if done else "nothing (up to date)"))
    elif cmd == "ingest-files":
        ingest_files(rest)
    elif cmd == "ingest-site":
        ingest_site(rest)
    elif cmd == "ingest-bbs":
        ingest_bbs(rest)
    elif cmd == "embed-remote":
        embed_remote(rest)
    else:
        sys.exit("usage: school-api migrate | ingest-files | ingest-site | ingest-bbs | embed-remote …")
