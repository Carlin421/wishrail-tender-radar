from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

BASE_URL = os.getenv("PCC_API_BASE_URL", "https://pcc-api.openfun.app/api").rstrip("/")
TAIPEI = ZoneInfo("Asia/Taipei")

@dataclass
class Tender:
    unit_id: str
    job_number: str
    title: str
    unit_name: str = ""
    budget: int | None = None
    deadline: str = ""
    url: str = ""
    award_type: str = ""
    qualification: str = ""
    detail_text: str = ""
    incumbent_risk: str = "UNKNOWN"
    incumbent_vendor: str = ""
    incumbent_ratio: float | None = None
    similar_awards: int = 0
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.unit_id}::{self.job_number}"

class PCC:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "WishRail-Tender-Radar/0.1", "Accept": "application/json"})
        token = os.getenv("OPENFUN_TOKEN", "").strip()
        if token:
            self.s.headers["Authorization"] = f"Bearer {token}"

    def get(self, path: str, **params: Any) -> dict:
        url = f"{BASE_URL}/{path}"
        last = None
        for attempt in range(4):
            try:
                time.sleep(0.25)
                r = self.s.get(url, params=params, timeout=25)
                if r.status_code == 429:
                    time.sleep(2 ** (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last = e
                time.sleep(attempt + 1)
        raise RuntimeError(f"GET {url} failed: {last}")

    def list_date(self, date: str) -> list[dict]:
        return self.get("listbydate", date=date).get("records", []) or []

    def tender(self, unit_id: str, job_number: str) -> list[dict]:
        return self.get("tender", unit_id=unit_id, job_number=job_number).get("records", []) or []

    def list_unit(self, unit_id: str) -> list[dict]:
        return self.get("listbyunit", unit_id=unit_id, page=1).get("records", []) or []

def brief_title(record: dict) -> str:
    b = record.get("brief") or {}
    return str(b.get("title", "")) if isinstance(b, dict) else str(b)

def detail_get(detail: dict, *needles: str) -> str:
    for n in needles:
        if n in detail and detail[n] not in (None, ""):
            return str(detail[n])
    for k, v in detail.items():
        if v in (None, ""):
            continue
        ks = str(k)
        for n in needles:
            if ks.endswith(n.split(":")[-1]):
                return str(v)
    return ""

def parse_money(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value)
    m = re.search(r"([\d,.]+)\s*萬", s)
    if m:
        return int(float(m.group(1).replace(",", "")) * 10000)
    m = re.search(r"\d[\d,]*", s)
    return int(m.group(0).replace(",", "")) if m else None

def parse_date(value: Any) -> str:
    s = str(value or "").strip()
    for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    m = re.match(r"^(\d{2,3})[/.-](\d{1,2})[/.-](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", s)
    if m:
        y, mo, d, hh, mm = m.groups()
        y = int(y) + 1911
        return datetime(y, int(mo), int(d), int(hh or 0), int(mm or 0)).strftime("%Y-%m-%d %H:%M:%S")
    return s

def choose_notice(records: list[dict]) -> dict | None:
    if not records:
        return None
    preferred = []
    for r in records:
        b = r.get("brief") or {}
        typ = b.get("type", "") if isinstance(b, dict) else ""
        if any(x in typ for x in ["招標公告", "公開取得", "更正公告"]):
            preferred.append(r)
    return max(preferred or records, key=lambda x: str(x.get("date", "")))

def to_tender(summary: dict, records: list[dict]) -> Tender | None:
    r = choose_notice(records)
    if not r:
        return None
    d = r.get("detail") or {}
    title = brief_title(summary) or brief_title(r)
    url = str(r.get("url") or summary.get("url") or "")
    if url.startswith("/"):
        url = "https://pcc-api.openfun.app" + url
    full = "\n".join(f"{k}: {v}" for k, v in d.items() if v not in (None, ""))
    return Tender(
        unit_id=str(summary.get("unit_id") or r.get("unit_id") or ""),
        job_number=str(summary.get("job_number") or r.get("job_number") or ""),
        title=title,
        unit_name=str(r.get("unit_name") or summary.get("unit_name") or ""),
        budget=parse_money(detail_get(d, "採購資料:預算金額", "預算金額")),
        deadline=parse_date(detail_get(d, "領投開標:截止投標", "截止投標")),
        url=url,
        award_type=detail_get(d, "領投開標:決標方式", "決標方式"),
        qualification=detail_get(d, "投標廠商:廠商資格摘要", "廠商資格摘要", "投標廠商資格及資格文件之附加說明"),
        detail_text=full,
    )

STOP = ["年度", "系統", "資訊", "建置", "開發", "維護", "維運", "委外", "服務", "採購案", "採購", "計畫", "功能", "增修", "擴充", "案"]

def norm_title(s: str) -> str:
    s = re.sub(r"\d{2,4}年度?", "", s.lower())
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "", s)
    for w in STOP:
        s = s.replace(w, "")
    return s

def similarity(a: str, b: str) -> float:
    a, b = norm_title(a), norm_title(b)
    if not a or not b:
        return 0.0
    seq = SequenceMatcher(None, a, b).ratio()
    aa = {a[i:i+2] for i in range(max(0, len(a)-1))}
    bb = {b[i:i+2] for i in range(max(0, len(b)-1))}
    jac = len(aa & bb) / len(aa | bb) if aa and bb else 0
    return max(seq, jac)

def awarded_vendors(records: list[dict]) -> list[str]:
    out = []
    for r in records:
        d = r.get("detail") or {}
        for k, v in d.items():
            ks, vs = str(k), str(v or "").strip()
            if re.search(r"得標廠商\d*:得標廠商$", ks) and vs:
                out.append(vs)
    return list(dict.fromkeys(out))

def incumbent(t: Tender, api: PCC, cfg: dict) -> None:
    try:
        hist = api.list_unit(t.unit_id)
    except Exception as e:
        t.risks.append(f"歷史查詢失敗: {e}")
        return
    matches = []
    seen = set()
    for r in hist[:150]:
        job = str(r.get("job_number", ""))
        if not job or job == t.job_number or job in seen:
            continue
        sim = similarity(t.title, brief_title(r))
        if sim >= cfg["similarity_threshold"]:
            matches.append((sim, r))
            seen.add(job)
    matches.sort(key=lambda x: x[0], reverse=True)
    wins, cases = [], 0
    for _, r in matches[: cfg["max_history_details"]]:
        try:
            vendors = awarded_vendors(api.tender(str(r.get("unit_id", t.unit_id)), str(r.get("job_number", ""))))
        except Exception:
            continue
        if vendors:
            cases += 1
            wins.extend(set(vendors))
    t.similar_awards = cases
    if not wins:
        return
    vendor, n = Counter(wins).most_common(1)[0]
    ratio = n / cases
    t.incumbent_vendor = vendor
    t.incumbent_ratio = ratio
    if cases < 3:
        t.incumbent_risk = "LOW_CONFIDENCE"
    elif ratio >= .65:
        t.incumbent_risk = "HIGH"
    elif ratio >= .45:
        t.incumbent_risk = "MEDIUM"
    else:
        t.incumbent_risk = "LOW"

def has(text: str, words: list[str]) -> bool:
    low = text.lower()
    return any(w.lower() in low for w in words)

def score(t: Tender, cfg: dict) -> None:
    s = 40
    text = " ".join([t.title, t.unit_name, t.award_type, t.qualification, t.detail_text])
    reasons, risks = [], []
    if has(text, cfg["regions"]):
        s += 10; reasons.append("+10 中部")
    if t.budget is not None:
        if cfg["preferred_budget_min"] <= t.budget <= cfg["preferred_budget_max"]:
            s += 15; reasons.append("+15 第一案甜蜜預算")
        elif cfg["min_budget"] <= t.budget <= cfg["max_budget"]:
            s += 8; reasons.append("+8 預算可接受")
    if has(text, ["新建", "建置", "開發", "建立", "導入", "原型", "PoC", "改版"]):
        s += 15; reasons.append("+15 新建/開發")
    if has(text, ["網站", "平台", "後台", "APP", "應用程式"]):
        s += 8; reasons.append("+8 Web/平台")
    if has(text, ["AI", "人工智慧", "RAG", "API", "介接", "串接", "智慧"]):
        s += 8; reasons.append("+8 AI/API")
    if has(t.unit_name, ["鄉公所", "鎮公所", "市公所", "地政事務所", "戶政事務所", "圖書館", "國民小學", "國民中學"]):
        s += 6; reasons.append("+6 小型機關")
    if "最有利標" in text or "企劃書" in text or "評選" in text:
        s += 6; reasons.append("+6 可用企劃/技術競爭")
    if has(text, ["維護", "維運"]):
        s -= 10; risks.append("-10 維護/維運")
    if has(text, ["既有", "原系統", "現行系統"]):
        s -= 8; risks.append("-8 既有系統")
    if has(text, ["硬體", "伺服器", "交換器", "RFID", "讀取器"]):
        s -= 18; risks.append("-18 硬體")
    if has(text, ["駐點", "駐府", "駐場", "常駐"]):
        s -= 20; risks.append("-20 駐點")
    if has(text, ["ISO 27001", "ISO27001", "國安", "敏感性採購"]):
        s -= 12; risks.append("-12 高資安門檻")
    if has(t.qualification, ["政府機關實績", "公部門實績", "政府實績", "機關履約實績"]):
        s -= 18; risks.append("-18 政府實績門檻")
    if t.incumbent_risk == "HIGH":
        s -= 22; risks.append("-22 incumbent HIGH")
    elif t.incumbent_risk == "MEDIUM":
        s -= 10; risks.append("-10 incumbent MEDIUM")
    t.score = max(0, min(100, s))
    t.reasons, t.risks = reasons, risks

def prefilter(r: dict, cfg: dict) -> bool:
    text = f"{brief_title(r)} {r.get('unit_name', '')}"
    return has(text, cfg["include_keywords"]) and not has(text, cfg["exclude_keywords"])

def save_db(tenders: list[Tender]) -> None:
    con = sqlite3.connect("tender_radar.db")
    con.execute("CREATE TABLE IF NOT EXISTS tenders (key TEXT PRIMARY KEY, payload TEXT NOT NULL, score INTEGER, updated_at TEXT)")
    for t in tenders:
        con.execute("INSERT INTO tenders(key,payload,score,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, score=excluded.score, updated_at=CURRENT_TIMESTAMP",
                    (t.key, json.dumps(asdict(t), ensure_ascii=False), t.score))
    con.commit(); con.close()

def report(tenders: list[Tender], top: int) -> Path:
    out = Path("reports"); out.mkdir(exist_ok=True)
    p = out / f"{datetime.now(TAIPEI).strftime('%Y-%m-%d')}.md"
    ranked = sorted(tenders, key=lambda x: x.score, reverse=True)[:top]
    lines = [f"# WishRail Tender Radar — {datetime.now(TAIPEI).strftime('%Y-%m-%d')}", "",
             "|分數|標案|機關|預算|截止|Incumbent|", "|---:|---|---|---:|---|---|"]
    for t in ranked:
        inc = t.incumbent_risk + (f" / {t.incumbent_vendor}" if t.incumbent_vendor else "")
        title = f"[{t.title}]({t.url})" if t.url else t.title
        money = "未知" if t.budget is None else f"NTD {t.budget:,}"
        lines.append(f"|**{t.score}**|{title}|{t.unit_name}|{money}|{t.deadline[:10] or '未知'}|{inc}|")
    for t in ranked:
        lines += ["", f"## {t.score}/100 — {t.title}", "",
                  f"- 機關：{t.unit_name}",
                  f"- 加分：{'；'.join(t.reasons) or '無'}",
                  f"- 風險：{'；'.join(t.risks) or '暫無明顯風險'}"]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p

def main(days: int, top: int, skip_incumbent: bool) -> int:
    cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
    api = PCC()
    summaries, seen = [], set()
    now = datetime.now(TAIPEI)
    for i in range(days):
        ds = (now - timedelta(days=i)).strftime("%Y%m%d")
        records = api.list_date(ds)
        print(f"{ds}: {len(records)} announcements")
        for r in records:
            key = (r.get("unit_id"), r.get("job_number"))
            if None in key or key in seen:
                continue
            seen.add(key)
            if prefilter(r, cfg):
                summaries.append(r)

    candidates = []
    for r in summaries:
        try:
            t = to_tender(r, api.tender(str(r["unit_id"]), str(r["job_number"])))
        except Exception as e:
            print("detail failed", r.get("job_number"), e)
            continue
        if not t:
            continue
        if t.budget is not None and not (cfg["min_budget"] <= t.budget <= cfg["max_budget"]):
            continue
        if t.unit_name and not has(t.unit_name + " " + t.detail_text, cfg["regions"]):
            continue
        if not skip_incumbent:
            incumbent(t, api, cfg)
        score(t, cfg)
        candidates.append(t)
        print(t.score, t.incumbent_risk, t.title)

    save_db(candidates)
    path = report(candidates, top)
    print("report:", path)
    return 0

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--skip-incumbent", action="store_true")
    a = ap.parse_args()
    raise SystemExit(main(a.days, a.top, a.skip_incumbent))
