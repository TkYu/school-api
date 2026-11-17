"""A topic of the community BBS (Discourse), as `ingest-bbs` sends it: JSON with the topic's title (and its Japanese
translation), category and posts in order, each with its Markdown and, for posts not written in Japanese, a Japanese
translation. One chunk per post: the agent reads the original (the student's own words, cited as such); the search
index gets the Japanese (the search model and the word index are Japanese, and simplified Chinese 奖学金 never
matches 奨学金). User names are never stored: @mentions and quote attributions are removed here.
"""
from __future__ import annotations

import json
import re

MENTION = re.compile(r"(?<![\w.])@[A-Za-z0-9_.-]{2,}")
QUOTE_HEAD = re.compile(r'\[quote="[^"]*"\]')
IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
UPLOAD = re.compile(r"\(upload://[^)]*\)")


def clean(raw: str) -> str:
    """Post Markdown without user names or image links (the words stay as written)."""
    text = QUOTE_HEAD.sub("[quote]", raw)
    text = MENTION.sub("@…", text)
    text = IMAGE.sub("[画像]", text)
    text = UPLOAD.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse(body: str) -> dict:
    data = json.loads(body)
    if not isinstance(data, dict) or not isinstance(data.get("posts"), list):
        raise ValueError("a BBS topic is JSON with a posts list")
    return data


def readable(topic: dict) -> str:
    """The version's body: the topic as a person reads it (for the admin page and its diff)."""
    lines = [topic.get("title") or ""]
    for p in topic["posts"]:
        role = " · staff" if p.get("staff") else ""
        lines.append(f"\n#{p.get('post_number')} ({(p.get('created_at') or '')[:10]}{role})\n{clean(p.get('raw') or '')}")
    return "\n".join(lines).strip()


def chunks(topic: dict, size: int = 1600) -> list[tuple[str, str, str]]:
    """(section, text the agent reads, text the index sees) per post; a long post is cut, its translation alongside."""
    out = []
    for p in topic["posts"]:
        text = clean(p.get("raw") or "")
        if not text:
            continue
        index = clean(p.get("ja") or "") or text
        section = f"#{p.get('post_number')}" + (" · 運営" if p.get("staff") else "")
        if len(text) <= size:
            out.append((section, text, index))
            continue
        parts = [text[i:i + size] for i in range(0, len(text), size)]
        jparts = [index[i:i + size] for i in range(0, len(index), max(1, len(index) // len(parts) + 1))] if index != text else parts
        for k, part in enumerate(parts):
            out.append((f"{section} ({k + 1}/{len(parts)})", part, jparts[min(k, len(jparts) - 1)]))
    return out
