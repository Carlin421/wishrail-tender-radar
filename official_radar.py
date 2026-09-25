from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pcc_official import PCC, Tender

TAIPEI = ZoneInfo("Asia/Taipei")

def has(text: str, words: list[str]) -> bool:
    low = text.lower()
    return any(word.lower() in low for word in words)

def prefilter(t: Tender, cfg: dict) -> bool:
    text = f"{t.unit} {t.title} {t.summary}"
    return (
        has(t.unit, cfg["regions"])
        and has(text, cfg["include_keywords"])
        and not has(text, cfg["exclude_keywords"])
    )

def score(t: Tender, cfg: dict) -> None:
    value = 40
    reasons: list[str] = []
    risks = list(t.risks)
    text = " ".join([
        t.unit, t.title, t.category, t.summary, t.award_type,
        t.qualification, t.detail_text,
    ])

    value += 10
    reasons.append("+10 中部目標區域")

    if t.budget is None:
        value -= 4
        risks.append("-4 預算未解析")
    elif cfg["preferred_budget_min"] <= t.budget <= cfg["preferred_budget_max"]:
        value += 15
        reasons.append("+15 第一案甜蜜預算")
    elif cfg["min_budget"] <= t.budget <= cfg["max_budget"]:
        value += 8
        reasons.append("+8 預算可接受")

    if has(text, ["新建", "建置", "開發", "建立", "導入", "原型", "PoC", "改版", "重建"]):
        value += 15
        reasons.append("+15 新建/開發型")
    if has(text, ["網站", "平台", "後台", "APP", "應用程式", "資料庫"]):
        value += 8
        reasons.append("+8 Web/平台/資料庫")
    if has(text, ["AI", "人工智慧", "LLM", "RAG", "API", "介接", "串接", "智慧"]):
        value += 8
        reasons.append("+8 AI/API")

    if has(t.unit, [
        "鄉公所", "鎮公所", "市公所", "地政事務所", "戶政事務所",
        "圖書館", "國民小學", "國民中學", "高級中學", "農會",
    ]):
        value += 6
        reasons.append("+6 中小型地方機關")

    if "公開取得報價單或企劃書" in t.category:
        value += 8
        reasons.append("+8 公開取得企劃書")
    elif "限制性招標" in t.category or "最有利標" in text or "評選" in text:
        value += 5
        reasons.append("+5 評選/技術競爭")

    if "是否提供電子投標]是" in t.summary:
        value += 2
        reasons.append("+2 可電子投標")

    if has(text, ["維護", "維運", "保固維護"]):
        value -= 10
        risks.append("-10 維護/維運型")
    if has(text, ["既有系統", "原系統", "現行系統", "續約"]):
        value -= 8
        risks.append("-8 既有系統優勢")
    if has(text, ["硬體", "設備採購", "伺服器", "交換器", "RFID", "讀取器"]):
        value -= 18
        risks.append("-18 硬體成分")
    if has(text, ["駐點", "駐府", "駐場", "常駐"]):
        value -= 20
        risks.append("-20 可能需駐點")
    if has(text, ["ISO 27001", "ISO27001", "資安專業證照", "國安", "敏感性採購"]):
        value -= 12
        risks.append("-12 高資安/國安門檻")
    if has(t.qualification, ["政府機關實績", "公部門實績", "政府實績", "機關履約實績"]):
        value -= 18
        risks.append("-18 政府實績門檻")
    if has(t.bid_bond, ["是", "須"]) and not has(t.bid_bond, ["否", "免"]):
        value -= 3
        risks.append("-3 需押標金")
    if has(t.performance_bond, ["是", "須"]) and not has(t.performance_bond, ["否", "免"]):
        value -= 3
        risks.append("-3 需履約保證金")

    t.score = max(0, min(100, int(value)))
    t.reasons = reasons
    t.risks = risks

def money(n: int | None) -> str:
    return "未知" if n is None else f"NTD {n:,}"

def recommendation(value: int) -> str:
    if value >= 78:
        return "🔥 優先 BID"
    if value >= 65:
        return "🟢 建議領標研究"
    if value >= 52:
        return "🟡 觀察 / 看規格"
    return "⚪ 低優先"

def save_db(rows: list[Tender]) -> None:
    con = sqlite3.connect("tender_radar.db")
    con.execute(
        "CREATE TABLE IF NOT EXISTS tenders "
        "(key TEXT PRIMARY KEY, payload TEXT NOT NULL, score INTEGER, updated_at TEXT)"
    )
    for t in rows:
        con.execute(
            "INSERT INTO tenders(key,payload,score,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, "
            "score=excluded.score, updated_at=CURRENT_TIMESTAMP",
            (t.key, json.dumps(t.to_dict(), ensure_ascii=False), t.score),
        )
    con.commit()
    con.close()

def report(rows: list[Tender], top: int) -> Path:
    out = Path("reports")
    out.mkdir(exist_ok=True)
    path = out / f"{datetime.now(TAIPEI).strftime('%Y-%m-%d')}.md"
    ranked = sorted(rows, key=lambda x: x.score, reverse=True)[:top]

    lines = [
        f"# WishRail Tender Radar — {datetime.now(TAIPEI).strftime('%Y-%m-%d')}",
        "",
        f"官方 PCC 掃描後候選：**{len(rows)}** 筆。",
        "",
        "|分數|建議|標案|機關|預算|截止|公告類型|",
        "|---:|---|---|---|---:|---|---|",
    ]
    for t in ranked:
        title = f"[{t.title}]({t.url})" if t.url else t.title
        lines.append(
            f"|**{t.score}**|{recommendation(t.score)}|{title}|{t.unit}|"
            f"{money(t.budget)}|{t.deadline[:16] or '未知'}|{t.category}|"
        )

    for i, t in enumerate(ranked, 1):
        lines += [
            "",
            f"## {i}. {t.title} — {t.score}/100",
            "",
            f"- 建議：**{recommendation(t.score)}**",
            f"- 機關：{t.unit}",
            f"- 案號：{t.job_number}",
            f"- 預算：{money(t.budget)}",
            f"- 截止：{t.deadline or '未知'}",
            f"- 公告類型：{t.category}",
            f"- 決標方式：{t.award_type or '未知'}",
            f"- 官方公告：{t.url or 'N/A'}",
            "",
            "**加分：** " + ("；".join(t.reasons) if t.reasons else "無"),
            "",
            "**風險：** " + ("；".join(t.risks) if t.risks else "目前未偵測到明顯風險"),
        ]

    lines += [
        "",
        "---",
        "",
        "> 核心掃描直接使用政府電子採購網官方公告；目前 incumbent 歷史得標分析尚未併入正式掃描。",
        "",
        "> 分數是 Bid/No-Bid 優先級，不是得標機率。投標前仍須閱讀完整招標文件與契約。",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path

def main(days: int, top: int) -> int:
    cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
    pcc = PCC()
    now = datetime.now(TAIPEI)
    hits: list[Tender] = []
    seen: set[str] = set()

    print(f"Official PCC scan: last {days} calendar days")
    for offset in range(days):
        date = (now - timedelta(days=offset)).strftime("%Y%m%d")
        try:
            daily = pcc.daily(date)
        except Exception as exc:
            print(f"{date}: daily list failed: {exc}")
            continue
        print(f"{date}: {len(daily)} active tender notices")
        for tender in daily:
            if tender.key in seen:
                continue
            seen.add(tender.key)
            if prefilter(tender, cfg):
                hits.append(tender)

    print(f"Central-Taiwan software keyword hits: {len(hits)}")
    candidates: list[Tender] = []

    for idx, tender in enumerate(hits, 1):
        pcc.hydrate(tender)
        if tender.budget is not None and not (
            cfg["min_budget"] <= tender.budget <= cfg["max_budget"]
        ):
            print(f"[{idx}/{len(hits)}] skip {money(tender.budget)} | {tender.title}")
            continue
        score(tender, cfg)
        candidates.append(tender)
        print(
            f"[{idx}/{len(hits)}] {tender.score:3d} | {money(tender.budget):>12} | "
            f"{tender.unit} | {tender.title}"
        )

    save_db(candidates)
    output = report(candidates, top)
    print(f"report: {output}")
    print("Top candidates:")
    for tender in sorted(candidates, key=lambda x: x.score, reverse=True)[:5]:
        print(f"  {tender.score:3d} | {money(tender.budget):>12} | {tender.title} | {tender.unit}")
    return 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WishRail / 贊尼工作室政府標案雷達")
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args()
    raise SystemExit(main(args.days, args.top))
