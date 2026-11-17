"""The syllabus parser on a fictional syllabus (same layout as the school's PDFs, invented content)."""
from app.parsers import syllabus

LINES = """令和8年度シラバス
湘南工科大学
科目基本情報
シラバスコード
S-999X01-01
ナンバリング
Ex000Abc
科目名
テスト工学概論
科目名英文
Introduction to Test Engineering
学部
情報学部
授業形態
講義
学科・科目区分
情報学科
配当年次
2年次
学期
後学期
必選区分
選択
単位数
2 単位
担当教員
架空太郎
アクティブラーニンググループワーク
科目の位置づけと目的
ソフトウェアの品質を確かめる方法を学ぶ。Adobe
Creative Cloudを使う回がある。
授業の進め方
講義と演習を組み合わせる。
到達目標
指標と評価割合
 授業計画
主担当教員のオフィスアワー
架空太郎
金曜日 3コマ
（備考）
レポートは期日までに提出すること。""".splitlines()

TABLES = [
    [["身に付ける力と対応する指標", "単位修得に必要なレベル", "目指すべきレベル"],
     ["テストを設計できる。", "基本的なテストを書ける。", "自動テストを設計できる。"]],
    [["", "", "評価方法", "", "", "", "", ""],
     ["", "", "受講態度", "授業中の\n活動", "予習・復習", "成果物・\n発表", "試験", "学修の\n振り返り"],
     ["共通指標", "a：受け取る力", "10", "10", "", "20", "", ""],
     ["固有指標", "g：知識・理解", "", "", "10", "", "50", ""],
     ["全体の評価割合", "", "10", "10", "10", "20", "50", "0"]],
    [["回数", "学修内容", "予習・復習内容", "", "時間"],
     ["１", "ガイダンス", "予習１", "シラバスを読む", "1"],
     ["", "", "復習１", "ノートを整理する", "1"],
     ["２", "単体テスト", "予習２", "教科書第1章", "2"],
     ["", "", "復習２", "演習問題", "2"]],
]


def test_header_sections_and_tables():
    s = syllabus.parse(LINES, TABLES)
    assert s.year == 2026
    assert s.fields["シラバスコード"] == "S-999X01-01" and s.fields["科目名"] == "テスト工学概論"
    assert s.fields["学部"] == "情報学部" and s.fields["学期"] == "後学期" and syllabus.credits(s.fields) == 2
    assert s.fields["アクティブラーニング"] == "グループワーク"
    heads = [h for h, _ in s.sections]
    assert heads[:2] == ["科目の位置づけと目的", "授業の進め方"] and "備考" in heads
    purpose = dict(s.sections)["科目の位置づけと目的"]
    assert "Adobe Creative Cloud" in purpose  # Latin words keep their space across a line break
    assert s.evaluation == {"受講態度": 10, "授業中の活動": 10, "予習・復習": 10, "成果物・発表": 20, "試験": 50, "学修の振り返り": 0}
    assert s.eval_sum == 100 and not s.notes
    assert s.goals[0]["goal"] == "テストを設計できる。"
    assert [x["no"] for x in s.sessions] == [1, 2] and s.sessions[1]["topic"] == "単体テスト" and s.sessions[1]["hours"] == 4


def test_inconsistent_totals_are_kept_and_noted():
    tables = [t if t[0][0] != "" else t[:-1] + [["全体の評価割合", "", "15", "20", "15", "35", "65", "0"]] for t in TABLES]
    s = syllabus.parse(LINES, tables)
    assert s.eval_sum == 150 and s.notes == {"eval_sum_not_100": 150}
    evaluation_chunk = dict(syllabus.chunks(s))["指標と評価割合"]
    assert "合計150%" in evaluation_chunk  # said as printed, not fixed


def test_every_chunk_carries_the_course():
    s = syllabus.parse(LINES, TABLES)
    ctx = syllabus.context_line(s)
    assert "テスト工学概論" in ctx and "S-999X01-01" in ctx and "情報学部" in ctx and "後学期" in ctx
    sections = [sec for sec, _ in syllabus.chunks(s)]
    assert sections[0] == "科目基本情報" and "指標と評価割合" in sections and "授業計画" in sections
    assert dict(syllabus.chunks(s))["科目基本情報"].startswith("シラバスコード：S-999X01-01、")
    assert dict(s.sections)["主担当教員のオフィスアワー"] == "架空太郎 金曜日 3コマ"  # list lines keep a space
    assert set(s.evaluation_rows) == {"a：受け取る力", "g：知識・理解"}


def test_evaluation_from_positions_when_the_table_has_no_lines():
    # header words and the total row as positioned on the page; 授業中の活動 and 試験 are empty in the indicator
    # rows, the total row prints all six columns; positions decide, not the order
    words = [[
        (100, 500, 140, 510, "受講態度"), (150, 500, 190, 510, "授業中の"), (200, 500, 250, 510, "予習・復習"),
        (260, 500, 300, 510, "成果物・"), (310, 500, 330, 510, "試験"), (340, 500, 370, 510, "学修の"),
        (20, 600, 90, 610, "全体の評価割合"),
        (115, 600, 125, 610, "20"), (165, 600, 175, 610, "20"), (220, 600, 230, 610, "20"),
        (275, 600, 285, 610, "40"), (318, 600, 322, 610, "0"), (352, 600, 356, 610, "0"),
    ]]
    s = syllabus.parse(LINES, [t for t in TABLES if t[0][0] != ""], words)
    assert s.evaluation == {"受講態度": 20, "授業中の活動": 20, "予習・復習": 20, "成果物・発表": 40, "試験": 0, "学修の振り返り": 0}
    assert s.notes == {"evaluation_from_positions": True}


def test_plan_without_a_table_from_text_and_plan_in_prose():
    plan = """ 授業計画
回数
学修内容
予習・復習内容
時間
１
全体ガイダンス
復習１　テーマ内容の確認
1
２
数学授業の問題点についての議論(1)：苦手意識の元を探そ
う
予習２　個別調査
2
主担当教員のオフィスアワー""".splitlines()
    lines = LINES[:LINES.index(" 授業計画")] + plan
    s = syllabus.parse(lines, [t for t in TABLES if t[0][0] != "回数"])
    assert [(x["no"], x["topic"]) for x in s.sessions] == [(1, "全体ガイダンス"), (2, "数学授業の問題点についての議論(1)：苦手意識の元を探そう")]
    assert s.sessions[0]["review"] == "テーマ内容の確認" and s.sessions[1]["prep"] == "個別調査" and s.sessions[1]["hours"] == 2
    assert s.notes.get("sessions_from_text") is True
    prose = LINES[:LINES.index(" 授業計画")] + [" 授業計画", "指導教員と一緒に研究生活を共にし、計画的に研究を進める。", "主担当教員のオフィスアワー", "架空太郎"]
    s2 = syllabus.parse(prose, [t for t in TABLES if t[0][0] != "回数"])
    assert not s2.sessions and dict(syllabus.chunks(s2))["授業計画"].startswith("指導教員と一緒に研究生活")
