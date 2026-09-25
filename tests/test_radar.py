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
