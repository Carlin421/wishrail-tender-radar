import json
from pathlib import Path
from radar import Tender, parse_money, parse_date, similarity, score

CFG = json.loads((Path(__file__).parents[1] / "config.json").read_text(encoding="utf-8"))

def test_money():
    assert parse_money("1,450,000元") == 1450000
    assert parse_money("約 68 萬元") == 680000

def test_roc_date():
    assert parse_date("115/09/30 17:00").startswith("2026-09-30 17:00")

def test_similarity():
    assert similarity("116年度街友服務資訊系統功能增修案", "115年度街友服務資訊系統維護及功能擴充") > .34

def test_good_first_case():
    t = Tender("x", "1", "智慧觀光網站暨後台管理系統建置",
               unit_name="南投縣某鄉公所", budget=680000,
               award_type="參考最有利標精神",
               detail_text="公開取得企劃書 API 串接")
    score(t, CFG)
    assert t.score >= 75

def test_incumbent_penalty():
    a = Tender("x", "1", "網站建置", unit_name="臺中市某區公所", budget=500000)
    b = Tender("x", "2", "網站建置", unit_name="臺中市某區公所", budget=500000, incumbent_risk="HIGH")
    score(a, CFG); score(b, CFG)
    assert b.score < a.score


from pcc_official import money_from_text, deadline_from_text
from official_radar import title_similarity, merchant_names

def test_official_money_fallback():
    assert money_from_text("採購資料 預算金額 新臺幣 1,450,000 元 是否公開 是") == 1450000
    assert money_from_text("預算金額：145 萬元") == 1450000

def test_official_deadline_fallback():
    value = deadline_from_text("領投開標 截止投標：115/09/30 17:00 開標時間")
    assert value.startswith("2026-09-30 17:00")

def test_history_similarity_and_merchants():
    assert title_similarity(
        "116年度街友服務資訊系統功能增修案",
        "115年度街友服務資訊系統維護及功能擴充"
    ) >= .36
    assert merchant_names({"merchants": [{"name": "甲資訊有限公司"}, {"name": "甲資訊有限公司"}]}) == ["甲資訊有限公司"]


def test_failed_history_signal_can_raise_priority():
    from pcc_official import Tender as OfficialTender
    from official_radar import score as official_score

    base = OfficialTender(
        unit="南投縣某鄉公所",
        job_number="A1",
        title="智慧觀光平台建置案",
        budget=600000,
        deadline="2026-10-10 17:00:00",
        category="公開取得報價單或企劃書公告",
    )
    boosted = OfficialTender(
        unit="南投縣某鄉公所",
        job_number="A2",
        title="智慧觀光平台建置案",
        budget=600000,
        deadline="2026-10-10 17:00:00",
        category="公開取得報價單或企劃書公告",
        failed_history_cases=2,
        failed_history_ratio=0.5,
    )
    official_score(base, CFG)
    official_score(boosted, CFG)
    assert any("流標/無得標訊號偏高" in reason for reason in boosted.reasons)
    assert boosted.score >= base.score


from line_push import parse_top_rows, format_line_message

def test_line_message_parses_top_candidate():
    markdown = """# WishRail Tender Radar — 2026-09-25

|分數|建議|標案|機關|預算|截止|Incumbent|公告類型|
|---:|---|---|---|---:|---|---|---|
|**88**|🔥 優先 BID|[智慧平台建置](https://example.com/tender)|南投縣某鄉公所|NTD 800,000|2026-10-10 17:00|LOW|公開取得報價單或企劃書公告|
"""
    rows = parse_top_rows(markdown)
    assert rows[0]["score"] == "88"
    assert rows[0]["title"] == "智慧平台建置"
    message = format_line_message(markdown)
    assert "88/100" in message
    assert "南投縣某鄉公所" in message
    assert "https://example.com/tender" in message
