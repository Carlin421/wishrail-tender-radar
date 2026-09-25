from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

from pcc_official import PCC, Tender

TAIPEI = ZoneInfo("Asia/Taipei")


MLW_KEYWORD = "https://pcc.mlwmlw.org/api/keyword"
TITLE_STOPWORDS = [
    "年度", "資訊", "系統", "建置", "開發", "維護", "維運", "委外",
    "服務", "採購案", "採購", "計畫", "功能", "增修", "擴充", "案"
]

def normalize_title(text: str) -> str:
    s = re.sub(r"\d{2,4}年度?", "", (text or "").lower())
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "", s)
    for word in TITLE_STOPWORDS:
        s = s.replace(word, "")
    return s

def title_similarity(a: str, b: str) -> float:
    aa, bb = normalize_title(a), normalize_title(b)
    if not aa or not bb:
        return 0.0
    seq = SequenceMatcher(None, aa, bb).ratio()
    a2 = {aa[i:i+2] for i in range(max(0, len(aa)-1))}
    b2 = {bb[i:i+2] for i in range(max(0, len(bb)-1))}
    jac = len(a2 & b2) / len(a2 | b2) if a2 and b2 else 0.0
    return max(seq, jac)

def history_query(title: str) -> str:
    cleaned = normalize_title(title)
    if len(cleaned) >= 4:
        return cleaned[:18]
    return re.sub(r"\d+", "", title)[:18].strip() or title[:18]

def merchant_names(row: dict) -> list[str]:
    out: list[str] = []
    merchants = row.get("merchants") or []
    if isinstance(merchants, dict):
        merchants = [merchants]
    if isinstance(merchants, list):
        for merchant in merchants:
            if isinstance(merchant, dict):
                name = str(merchant.get("name") or merchant.get("_id") or "").strip()
            else:
                name = str(merchant).strip()
            if name:
                out.append(name)
    return list(dict.fromkeys(out))

def enrich_history(t: Tender, timeout: int = 7) -> None:
    """Enrich with public near-term history. It is advisory and never blocks the scan."""
    query = history_query(t.title)
    if not query:
        return
    try:
        url = f"{MLW_KEYWORD}/{quote(query, safe='')}"
        response = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "WishRail-Tender-Radar/0.2", "Accept": "application/json"},
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            return
    except Exception as exc:
        t.historical_note = f"歷史 enrichment 暫時無法取得：{type(exc).__name__}"
        return

    def norm_unit(value: str) -> str:
        return re.sub(r"[\s　]+", "", str(value or "")).replace("臺", "台")

    matches: list[dict] = []
    target_unit = norm_unit(t.unit)
    for row in rows:
        row_unit = str(row.get("unit") or "").strip()
        row_title = str(row.get("name") or "").strip()
        if not row_title or row_title == t.title:
            continue
        # Same procuring unit is the strongest incumbent signal, but normalize
        # whitespace and 台/臺 differences before comparing.
        if target_unit and row_unit and target_unit != norm_unit(row_unit):
            continue
        sim = title_similarity(t.title, row_title)
        if sim >= 0.36:
            item = dict(row)
            item["_sim"] = sim
            matches.append(item)

    matches.sort(key=lambda x: float(x.get("_sim", 0)), reverse=True)
    matches = matches[:12]
    t.similar_awards = len(matches)
    winners: list[str] = []
    awarded_cases = 0
    bidder_counts: list[int] = []

    # The keyword endpoint already contains merged award winners for many rows.
    for row in matches:
        names = merchant_names(row)
        if names:
            awarded_cases += 1
            winners.extend(set(names))

    # Pull a bounded detail sample to estimate competition intensity.
    # Historical data is advisory, so never let a slow mirror block the daily report.
    def fetch_history_detail(row: dict) -> tuple[int | None, list[str], bool]:
        job = str(row.get("job_number") or row.get("id") or "").strip()
        unit_id = str(row.get("unit_id") or "").strip()
        unit_name = str(row.get("unit") or "").strip()
        if not job:
            return None, [], False

        base = f"https://pcc.mlwmlw.org/api/tender/{quote(job, safe='')}"
        urls: list[str] = []
        for key in (unit_id, unit_name):
            if key:
                urls.append(base + f"/{quote(key, safe='')}")
        urls.append(base)

        docs: list[dict] = []
        for detail_url in dict.fromkeys(urls):
            try:
                detail_response = requests.get(
                    detail_url,
                    timeout=6,
                    headers={"User-Agent": "WishRail-Tender-Radar/0.4", "Accept": "application/json"},
                )
                detail_response.raise_for_status()
                payload = detail_response.json()
                if isinstance(payload, list) and payload:
                    docs = [d for d in payload if isinstance(d, dict)]
                    if docs:
                        break
            except Exception:
                continue

        if not docs:
            return None, [], False

        # Prefer the record carrying award/candidate data, then the most recent row.
        doc = next((d for d in docs if d.get("candidates") or d.get("award") or d.get("merchants")), docs[0])
        candidates = doc.get("candidates") or []
        count = len(candidates) if isinstance(candidates, list) and candidates else None
        names = merchant_names(doc)
        if not names and isinstance(doc.get("award"), dict):
            names = merchant_names(doc["award"])

        # Treat an explicit award record with no winner as a failed/no-award signal.
        has_award_record = bool(doc.get("award")) or "決標" in str(doc.get("type") or "")
        failed = bool(has_award_record and not names)
        return count, names, failed

    sample_rows = matches[:8]
    failed_cases = 0
    detailed_cases = 0
    if sample_rows:
        with ThreadPoolExecutor(max_workers=min(4, len(sample_rows))) as pool:
            future_rows = {pool.submit(fetch_history_detail, row): row for row in sample_rows}
            for future in as_completed(future_rows):
                row = future_rows[future]
                count, detail_merchants, failed = future.result()
                if count is not None:
                    bidder_counts.append(count)
                if count is not None or detail_merchants or failed:
                    detailed_cases += 1
                if failed:
                    failed_cases += 1
                if detail_merchants and not merchant_names(row):
                    awarded_cases += 1
                    winners.extend(set(detail_merchants))

    if bidder_counts:
        t.history_samples = len(bidder_counts)
        t.avg_bidders = sum(bidder_counts) / len(bidder_counts)
        t.single_bid_ratio = sum(1 for n in bidder_counts if n == 1) / len(bidder_counts)
    t.failed_history_cases = failed_cases
    if detailed_cases:
        t.failed_history_ratio = failed_cases / detailed_cases

    note_parts = [f"近18個月同機關相似案 {len(matches)} 件"]
    if bidder_counts:
        note_parts.append(
            f"競爭樣本 {len(bidder_counts)} 件，平均 {t.avg_bidders:.1f} 家投標，"
            f"單一投標約 {t.single_bid_ratio:.0%}"
        )
    if t.failed_history_ratio is not None:
        note_parts.append(f"無得標/流標訊號約 {t.failed_history_ratio:.0%}（{failed_cases}/{detailed_cases}）")

    if not winners:
        note_parts.append("未取得足夠得標廠商資料")
        t.historical_note = "；".join(note_parts)
        return

    vendor, wins = Counter(winners).most_common(1)[0]
    ratio = wins / max(awarded_cases, 1)
    t.incumbent_vendor = vendor
    t.incumbent_ratio = ratio
    if awarded_cases < 3:
        t.incumbent_risk = "LOW_CONFIDENCE"
    elif ratio >= 0.65:
        t.incumbent_risk = "HIGH"
    elif ratio >= 0.45:
        t.incumbent_risk = "MEDIUM"
    else:
        t.incumbent_risk = "LOW"
    note_parts.append(f"最高集中廠商 {vendor} 約 {ratio:.0%}")
    t.historical_note = "；".join(note_parts)

def has(text: str, words: list[str]) -> bool:
    low = text.lower()
    return any(word.lower() in low for word in words)

def prefilter(t: Tender, cfg: dict) -> bool:
    strong_software = [
        "資訊", "網站", "平台", "資料庫", "軟體", "API", "人工智慧", "AI",
        "數位", "APP", "程式", "網路", "管理系統", "服務系統",
        "查詢系統", "報名系統", "申請系統", "預約系統", "智慧系統"
    ]
    return (
        has(t.unit, cfg["regions"])
        and has(t.title, strong_software)
        and not has(t.title, cfg["exclude_keywords"])
    )

def score(t: Tender, cfg: dict) -> None:
    value = 40
    reasons: list[str] = []
    risks = list(t.risks)
    title_text = t.title
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

    if has(title_text, ["新建", "建置", "開發", "建立", "導入", "原型", "PoC", "改版", "重建"]):
        value += 15
        reasons.append("+15 新建/開發型")
    if has(title_text, ["網站", "平台", "後台", "APP", "應用程式", "資料庫"]):
        value += 8
        reasons.append("+8 Web/平台/資料庫")
    if has(title_text, ["AI", "人工智慧", "LLM", "RAG", "API", "介接", "串接", "智慧"]):
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

    if t.incumbent_risk == "HIGH":
        value -= 22
        risks.append(f"-22 Incumbent HIGH：{t.incumbent_vendor}")
    elif t.incumbent_risk == "MEDIUM":
        value -= 10
        risks.append(f"-10 Incumbent MEDIUM：{t.incumbent_vendor}")
    elif t.incumbent_risk == "LOW":
        reasons.append("+0 Incumbent LOW")
    elif t.incumbent_risk == "LOW_CONFIDENCE":
        risks.append("歷史樣本不足，incumbent 判定低信心")

    if t.avg_bidders is not None and t.history_samples >= 2:
        if t.avg_bidders <= 1.5:
            value += 6
            reasons.append(f"+6 歷史競爭低：平均 {t.avg_bidders:.1f} 家投標")
        elif t.avg_bidders <= 2.5:
            value += 3
            reasons.append(f"+3 歷史競爭偏低：平均 {t.avg_bidders:.1f} 家投標")
        elif t.avg_bidders >= 5:
            value -= 5
            risks.append(f"-5 歷史競爭較高：平均 {t.avg_bidders:.1f} 家投標")
    if t.single_bid_ratio is not None and t.history_samples >= 3 and t.single_bid_ratio >= 0.5:
        value += 3
        reasons.append(f"+3 同類案單一投標比例 {t.single_bid_ratio:.0%}")
    if t.failed_history_ratio is not None and t.failed_history_cases >= 1:
        if t.failed_history_ratio >= 0.4:
            value += 4
            reasons.append(f"+4 歷史流標/無得標訊號偏高 {t.failed_history_ratio:.0%}")
        elif t.failed_history_ratio >= 0.2:
            value += 2
            reasons.append(f"+2 有歷史流標/無得標訊號 {t.failed_history_ratio:.0%}")

    # Safety gate for incomplete notices: never elevate an unknown-budget lead to green/BID.
    if t.budget is None:
        value = min(value, 62)
        risks.append("資料完整度不足：預算未知，最高僅列觀察")
    if not t.deadline:
        value = min(value, 64)
        risks.append("資料完整度不足：截止時間未知")

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
        "|分數|建議|標案|機關|預算|截止|Incumbent|公告類型|",
        "|---:|---|---|---|---:|---|---|---|",
    ]
    for t in ranked:
        title = f"[{t.title}]({t.url})" if t.url else t.title
        lines.append(
            f"|**{t.score}**|{recommendation(t.score)}|{title}|{t.unit}|"
            f"{money(t.budget)}|{t.deadline[:16] or '未知'}|"
            f"{t.incumbent_risk}{(' / ' + t.incumbent_vendor) if t.incumbent_vendor else ''}|{t.category}|"
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
            f"- Incumbent：{t.incumbent_risk}"
            + (f"；{t.incumbent_vendor} 約 {t.incumbent_ratio:.0%}" if t.incumbent_vendor and t.incumbent_ratio is not None else ""),
            f"- 歷史訊號：{t.historical_note or '未取得足夠歷史資料'}",
            f"- 歷史競爭："
            + (f"平均 {t.avg_bidders:.1f} 家投標；單一投標 {t.single_bid_ratio:.0%}（樣本 {t.history_samples}）"
               if t.avg_bidders is not None and t.single_bid_ratio is not None else "樣本不足"),
            f"- 流標/無得標訊號："
            + (f"{t.failed_history_ratio:.0%}（{t.failed_history_cases} 件）"
               if t.failed_history_ratio is not None else "樣本不足"),
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
        "> 核心掃描直接使用政府電子採購網官方公告；incumbent 是近18個月公開歷史資料的輔助訊號，資料不足時會標示 UNKNOWN / LOW_CONFIDENCE。",
        "",
        "> 分數是 Bid/No-Bid 優先級，不是得標機率。投標前仍須閱讀完整招標文件與契約。",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path

def main(days: int, top: int, skip_history: bool = False) -> int:
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
        # Daily listings already expose budget for many notices. Reject known out-of-range
        # cases before opening detail pages, which greatly reduces PCC WAF pressure.
        if tender.budget is not None and not (
            cfg["min_budget"] <= tender.budget <= cfg["max_budget"]
        ):
            print(f"[{idx}/{len(hits)}] skip-listing {money(tender.budget)} | {tender.title}")
            continue

        pcc.hydrate(tender)
        if tender.budget is not None and not (
            cfg["min_budget"] <= tender.budget <= cfg["max_budget"]
        ):
            print(f"[{idx}/{len(hits)}] skip-detail {money(tender.budget)} | {tender.title}")
            continue
        # Historical requests are relatively expensive; only enrich viable budget candidates.
        if not skip_history:
            enrich_history(tender)
        else:
            tender.historical_note = "CI smoke test：略過歷史 enrichment"
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
    parser.add_argument("--skip-history", action="store_true", help="skip incumbent/competition enrichment")
    args = parser.parse_args()
    raise SystemExit(main(args.days, args.top, args.skip_history))
