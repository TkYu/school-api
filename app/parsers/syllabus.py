"""Syllabus PDFs (湘南工科大学 format, 令和8年度): header fields, sections, and the three tables (到達目標,
指標と評価割合, 授業計画). Header fields come from the text lines (label line, then its value: clean values);
tables come from cell positions (PyMuPDF find_tables), because their text order loses which number belongs to which
column. Values are kept as printed; inconsistencies are noted, never corrected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

ZEN = str.maketrans("０１２３４５６７８９", "0123456789")
HEADER_LABELS = ["シラバスコード", "ナンバリング", "科目名", "科目名英文", "学部", "授業形態", "学科・科目区分", "教職科目対応",
                 "科目分野", "実践的教育対応", "配当年次", "学期", "必選区分", "単位数", "担当教員"]
# sections whose lines are separate items (names, rooms, mail, book titles): joined with a space, not run together
LIST_SECTIONS = {"教科書", "参考書", "主担当教員のオフィスアワー"}
SECTION_HEADINGS = ["科目の位置づけと目的", "授業の進め方", "課題（試験やレポート等）に対するフィードバックの方法", "履修の条件",
                    "教科書", "参考書", "到達目標", "指標と評価割合", "授業計画", "主担当教員のオフィスアワー", "（備考）", "備考"]
EVALUATION_ITEMS = ["受講態度", "授業中の活動", "予習・復習", "成果物・発表", "試験", "学修の振り返り"]


@dataclass
class Syllabus:
    year: int | None
    fields: dict[str, str]
    sections: list[tuple[str, str]]                     # (heading, text) in page order
    goals: list[dict[str, str]] = field(default_factory=list)
    evaluation: dict[str, int] | None = None            # the 全体の評価割合 row
    evaluation_rows: dict[str, dict[str, int]] = field(default_factory=dict)  # indicator rows (a：受け取る力 …)
    sessions: list[dict[str, Any]] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def eval_sum(self) -> int | None:
        return sum(self.evaluation.values()) if self.evaluation else None


def _clean(cell: str | None) -> str:
    return re.sub(r"\s+", "", (cell or "")).strip()


def join_lines(lines: list[str]) -> str:
    """PDF lines back into running text: Japanese joins without a space, two Latin words keep one."""
    out = ""
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        if out and re.search(r"[A-Za-z0-9]$", out) and re.match(r"[A-Za-z0-9]", s):
            out += " "
        out += s
    return out


def _year(lines: list[str]) -> int | None:
    for s in lines[:5]:
        m = re.search(r"令和\s*([0-9０-９]+)\s*年度", s)
        if m:
            return 2018 + int(m.group(1).translate(ZEN))
    return None


def parse_header(lines: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    stripped = [s.strip() for s in lines]
    stop = len(stripped)
    for i, s in enumerate(stripped):
        if s.startswith("アクティブラーニング"):
            fields["アクティブラーニング"] = s[len("アクティブラーニング"):].strip()
            stop = i
            break
    for i, s in enumerate(stripped[:stop]):
        if s in HEADER_LABELS and s not in fields:
            values = []
            for nxt in stripped[i + 1:stop]:
                if nxt in HEADER_LABELS:
                    break
                values.append(nxt)
                if s != "担当教員":   # only the instructors may run over several lines
                    break
            fields[s] = " ".join(v for v in values if v)
    return fields


def parse_sections(lines: list[str]) -> list[tuple[str, str]]:
    sections: list[tuple[str, list[str]]] = []
    started = False
    for raw in lines:
        s = raw.strip()
        head = next((h for h in SECTION_HEADINGS if s == h or (h in ("授業計画", "主担当教員のオフィスアワー") and s.startswith(h))), None)
        if head:
            started = True
            sections.append((head.strip("（）"), []))
            continue
        if started and sections:
            sections[-1][1].append(raw)
    return [(h, " ".join(x.strip() for x in body if x.strip()) if h in LIST_SECTIONS else join_lines(body)) for h, body in sections]


def parse_tables(tables: list[list[list[str | None]]], syl: Syllabus) -> None:
    for rows in tables:
        if not rows:
            continue
        first = _clean(rows[0][0] if rows[0] else "")
        flat = [_clean(c) for r in rows for c in r]
        if first.startswith("身に付ける力"):
            for r in rows[1:]:
                cells = [_clean(c) for c in r] + ["", "", ""]
                if cells[0]:
                    syl.goals.append({"goal": cells[0], "minimum": cells[1], "target": cells[2]})
        elif "全体の評価割合" in flat:
            header = next((r for r in rows if any(_clean(c) in EVALUATION_ITEMS for c in r)), None)
            if header is None:
                continue
            cols = {i: _clean(c) for i, c in enumerate(header) if _clean(c) in EVALUATION_ITEMS}
            for r in rows:
                label = _clean(r[1] if len(r) > 1 else "") or _clean(r[0])   # the indicator (a：受け取る力), not its group
                values = {item: int(_clean(r[i])) for i, item in cols.items() if i < len(r) and re.fullmatch(r"\d{1,3}", _clean(r[i]))}
                if _clean(r[0]) == "全体の評価割合":
                    syl.evaluation = {item: values.get(item, 0) for item in cols.values()}
                elif values and label:
                    syl.evaluation_rows[label] = values
        elif first == "回数":
            current: dict[str, Any] | None = None
            for r in rows[1:]:
                cells = [(c or "").strip() for c in r] + ["", "", "", "", ""]
                no = _clean(cells[0]).translate(ZEN)
                if no.isdigit():
                    current = {"no": int(no), "topic": join_lines(cells[1].splitlines()), "prep": "", "review": "", "hours": 0.0}
                    syl.sessions.append(current)
                if current is None:
                    continue
                kind, text, hours = _clean(cells[2]), join_lines(cells[3].splitlines()), _clean(cells[4])
                if kind.startswith("予習"):
                    current["prep"] = text
                elif kind.startswith("復習"):
                    current["review"] = text
                if re.fullmatch(r"\d+(\.\d+)?", hours):
                    current["hours"] += float(hours)


# first word of each evaluation column header as printed (headers wrap: 授業中の / 活動)
_HEADER_WORDS = {"受講態度": "受講態度", "授業中の": "授業中の活動", "授業中の活動": "授業中の活動", "予習・復習": "予習・復習",
                 "成果物・": "成果物・発表", "成果物・発表": "成果物・発表", "試験": "試験", "学修の": "学修の振り返り", "学修の振り返り": "学修の振り返り"}
Word = tuple[float, float, float, float, str]


def evaluation_from_words(pages: list[list[Word]]) -> dict[str, int] | None:
    """When the table has no ruling lines (find_tables misses it): every number on the 全体の評価割合 line goes to the
    column header nearest to it horizontally. Positions, not reading order: empty cells would shift the order."""
    for words in pages:
        total = next((w for w in words if w[4] == "全体の評価割合"), None)
        if not total:
            continue
        heads = [(w, _HEADER_WORDS[w[4]]) for w in words if w[4] in _HEADER_WORDS and w[1] < total[1]]
        # keep the header row closest above the total row (the headers of the evaluation table)
        if not heads:
            continue
        top = max(w[1] for w, _ in heads)
        cols = [((w[0] + w[2]) / 2, item) for w, item in heads if top - w[1] < 30]
        yc = (total[1] + total[3]) / 2
        numbers = [w for w in words if re.fullmatch(r"\d{1,3}", w[4]) and abs((w[1] + w[3]) / 2 - yc) < 3 and w[0] > total[2]]
        if not cols or not numbers:
            continue
        result: dict[str, int] = {item: 0 for _, item in sorted(cols)}
        for n in numbers:
            xc = (n[0] + n[2]) / 2
            result[min(cols, key=lambda c: abs(c[0] - xc))[1]] = int(n[4])
        return result
    return None


def sessions_from_lines(lines: list[str]) -> list[dict[str, Any]]:
    """When the plan table has no ruling lines: after 授業計画 / 回数 … 時間, a session starts with its number (the
    next one in order), then its topic up to 予習 / 復習; a number line after those is its hours."""
    try:
        start = next(i for i, s in enumerate(lines) if s.strip().startswith("授業計画"))
    except StopIteration:
        return []
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    topic: list[str] = []
    last = ""
    for raw in lines[start + 1:]:
        s = raw.strip()
        if s.startswith("主担当教員") or s in ("（備考）", "備考"):
            break
        m = re.match(r"^([0-9０-９]{1,2})(.*)$", s)
        if m and int(m.group(1).translate(ZEN)) == (cur["no"] if cur else 0) + 1 and not re.fullmatch(r"[0-9.]+", s):
            if cur is not None and topic and not cur["topic"]:
                cur["topic"] = join_lines(topic)
            cur, topic, last = {"no": int(m.group(1).translate(ZEN)), "topic": "", "prep": "", "review": "", "hours": 0.0}, ([m.group(2)] if m.group(2).strip() else []), "topic"
            out.append(cur)
            continue
        if cur is None:
            continue
        if s.startswith(("予習", "復習")):
            if topic and not cur["topic"]:
                cur["topic"] = join_lines(topic)
            kind = "prep" if s.startswith("予習") else "review"
            cur[kind] = re.sub(r"^(予習|復習)[0-9０-９]*\s*", "", s)
            last = kind
        elif re.fullmatch(r"\d+(\.\d+)?", s):
            cur["hours"] += float(s)
        elif s and last == "topic":
            topic.append(s)
    if cur is not None and topic and not cur["topic"]:
        cur["topic"] = join_lines(topic)
    return out


def parse(lines: list[str], tables: list[list[list[str | None]]], words: list[list[Word]] | None = None) -> Syllabus:
    syl = Syllabus(year=_year(lines), fields=parse_header(lines), sections=parse_sections(lines))
    parse_tables(tables, syl)
    if not syl.sessions:
        syl.sessions = sessions_from_lines(lines)
        if syl.sessions:
            syl.notes["sessions_from_text"] = True
    if syl.evaluation is None and words:
        syl.evaluation = evaluation_from_words(words)
        if syl.evaluation is not None:
            syl.notes["evaluation_from_positions"] = True
    if syl.evaluation is not None and syl.eval_sum != 100:
        syl.notes["eval_sum_not_100"] = syl.eval_sum
    if syl.evaluation is None:
        syl.notes["no_evaluation_table"] = True
    return syl


def read_pdf(data: bytes) -> tuple[list[str], list[list[list[str | None]]], list[list[Word]]]:
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        lines = [line for page in doc for line in page.get_text().splitlines()]
        tables = [t.extract() for page in doc for t in page.find_tables().tables]
        words = [[(w[0], w[1], w[2], w[3], w[4]) for w in page.get_text("words")] for page in doc]
    return lines, tables, words


# ------------------------------------------------------------------ chunks for search
def credits(fields: dict[str, str]) -> int | None:
    m = re.search(r"\d+", (fields.get("単位数") or "").translate(ZEN))
    return int(m.group()) if m else None


def context_line(syl: Syllabus) -> str:
    f = syl.fields
    parts = [f.get("科目名", ""), f"（{f.get('シラバスコード', '')}）", f.get("学部", ""), f.get("学科・科目区分", ""),
             f.get("配当年次", ""), f.get("学期", ""), f.get("授業形態", ""), f.get("必選区分", "")]
    c = credits(f)
    if c:
        parts.append(f"{c}単位")
    return " ".join(p for p in parts if p).replace(" （", "（")


def chunks(syl: Syllabus) -> list[tuple[str, str]]:
    """(section, text) for search: the basic info, every section, the goals, the evaluation and a compressed plan."""
    f = syl.fields
    out: list[tuple[str, str]] = []
    order = HEADER_LABELS + ["アクティブラーニング"]
    basic = "、".join(f"{k}：{f[k]}" for k in order if f.get(k))
    out.append(("科目基本情報", basic))
    from_tables = {"到達目標": bool(syl.goals), "指標と評価割合": bool(syl.evaluation), "授業計画": bool(syl.sessions)}
    for head, text in syl.sections:
        if from_tables.get(head) or not text:
            continue  # written from the tables below; a plan in prose (no table) is kept as it is
        out.append((head, text))
    if syl.goals:
        out.append(("到達目標", "\n".join(f"・{g['goal']}（単位修得に必要なレベル：{g['minimum']}／目指すべきレベル：{g['target']}）" for g in syl.goals)))
    if syl.evaluation:
        s = "全体の評価割合：" + "、".join(f"{k}{v}%" for k, v in syl.evaluation.items())
        if syl.eval_sum != 100:
            s += f"（合計{syl.eval_sum}%：シラバスの記載のまま）"
        out.append(("指標と評価割合", s))
    if syl.sessions:
        plan = "、".join(f"第{x['no']}回 {x['topic']}" for x in syl.sessions if x["topic"])
        hours = sum(x["hours"] for x in syl.sessions)
        out.append(("授業計画", plan + (f"（予習・復習 合計{hours:g}時間）" if hours else "")))
    return out
