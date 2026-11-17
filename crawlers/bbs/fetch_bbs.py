"""Fetch the public topics of the community BBS (Discourse) into one JSON file, for review, translation and import into
school-api. Anonymous reads of Discourse's public JSON only (no API key): what a visitor sees, nothing from restricted
categories or private messages. Python standard library only, so it runs anywhere.

    python fetch_bbs.py                       # → data/bbs-YYYYMMDD-HHMM.json, and a list of the topics
    python fetch_bbs.py --list data/bbs-….json  # the list again from a file

The file keeps each post's Markdown (`raw`) and the author's role (staff / admin / moderator) but no user name,
display name or avatar; @mentions in the text are kept here and removed at import. Each post gets `lang`
(ja when it has kana, zh when it has CJK characters without kana, else other) to decide what needs translating.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://bbs.gakuen.ac"
UA = "TomoSchoolCrawler/0.1 (read-only school information index; +https://tomo.gakuen.ac/)"
DELAY = 0.5
KANA = re.compile(r"[぀-ヿ]")
CJK = re.compile(r"[一-鿿]")


def get(path: str) -> dict:
    time.sleep(DELAY)
    req = urllib.request.Request(BASE + path, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def lang_of(text: str) -> str:
    """ja / zh / other by the share of kana among kana and CJK characters: Japanese prose is about half kana, while
    a Chinese post with a few Japanese terms (在留カード, ログイン) has only a few percent."""
    kana, cjk = len(KANA.findall(text)), len(CJK.findall(text))
    if kana + cjk == 0:
        return "other"
    return "ja" if kana / (kana + cjk) >= 0.2 else "zh"


def topic_ids() -> list[int]:
    """Every public topic: the latest list, page by page (pinned and closed ones included)."""
    ids: list[int] = []
    path = "/latest.json?no_definitions=true"
    while path:
        page = get(path)["topic_list"]
        ids += [t["id"] for t in page["topics"] if t["id"] not in ids]
        more = page.get("more_topics_url")
        path = (more.replace("/latest?", "/latest.json?") if more else None)
    return ids


def fetch_topic(tid: int, categories: dict[int, str]) -> dict:
    t = get(f"/t/{tid}.json")
    stream = t["post_stream"]["stream"]
    have = {p["id"]: p for p in t["post_stream"]["posts"]}
    missing = [i for i in stream if i not in have]
    for i in range(0, len(missing), 20):
        q = "&".join(f"post_ids[]={pid}" for pid in missing[i:i + 20])
        for p in get(f"/t/{tid}/posts.json?{q}")["post_stream"]["posts"]:
            have[p["id"]] = p
    posts = []
    for pid in stream:
        p = have.get(pid)
        if not p or p.get("hidden") or p.get("deleted_at") or p.get("user_deleted") or p.get("post_type") != 1:
            continue  # hidden, deleted, moderator actions and "closed" notes are not content
        raw = get(f"/posts/{pid}.json").get("raw") or ""
        posts.append({"id": pid, "post_number": p["post_number"], "created_at": p["created_at"], "updated_at": p["updated_at"],
                      "reply_to": p.get("reply_to_post_number"), "staff": bool(p.get("staff")), "admin": bool(p.get("admin")),
                      "moderator": bool(p.get("moderator")), "lang": lang_of(raw), "raw": raw})
    return {"id": tid, "url": f"{BASE}/t/{t['slug']}/{tid}", "title": t["title"], "category": categories.get(t.get("category_id"), ""),
            "tags": [x if isinstance(x, str) else x.get("name") for x in (t.get("tags") or [])],
            "created_at": t["created_at"], "last_posted_at": t.get("last_posted_at"), "bumped_at": t.get("bumped_at"),
            "views": t.get("views"), "like_count": t.get("like_count"), "closed": t.get("closed"), "archived": t.get("archived"),
            "pinned": t.get("pinned") or t.get("pinned_globally"), "posts": posts}


def show(data: dict) -> None:
    topics = data["topics"]
    print(f"{len(topics)} topics, {sum(len(t['posts']) for t in topics)} posts (fetched {data['fetched_at']})")
    print(f"{'id':>4}  {'category':10}  {'posts':>5}  {'zh/ja/other':11}  {'chars':>6}  {'last post':10}  title")
    for t in sorted(topics, key=lambda t: t["id"]):
        langs = [p["lang"] for p in t["posts"]]
        mix = f"{langs.count('zh')}/{langs.count('ja')}/{langs.count('other')}"
        chars = sum(len(p["raw"]) for p in t["posts"])
        print(f"{t['id']:>4}  {t['category'][:10]:10}  {len(t['posts']):>5}  {mix:11}  {chars:>6}  {(t['last_posted_at'] or '')[:10]:10}  {t['title'][:60]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch the BBS's public topics into a JSON file")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--list", type=Path, help="only print the list of an earlier file")
    a = ap.parse_args()
    if a.list:
        data = json.loads(a.list.read_text(encoding="utf-8"))
        for t in data["topics"]:
            for p in t["posts"]:
                p["lang"] = lang_of(p["raw"])
        show(data)
        return
    cats: dict[int, str] = {}
    for c in get("/categories.json?include_subcategories=true")["category_list"]["categories"]:
        cats[c["id"]] = c["name"]
        for s in c.get("subcategory_list") or []:
            cats[s["id"]] = f"{c['name']} / {s['name']}"
    data = {"source": BASE, "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "topics": []}
    for tid in topic_ids():
        try:
            data["topics"].append(fetch_topic(tid, cats))
        except Exception as e:  # noqa: BLE001 - one broken topic must not stop the rest
            print(f"topic {tid}: {e}", file=sys.stderr)
    out = a.out or Path(__file__).parent / "data" / f"bbs-{datetime.now():%Y%m%d-%H%M}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {out}")
    show(data)


if __name__ == "__main__":
    main()
