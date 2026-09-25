from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

BASE = "https://web.pcc.gov.tw"
DAILY_URL = BASE + "/prkms/tender/common/noticeDate/readPublish"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0"

ACTIVE_SECTIONS = {
    "公開招標公告",
    "公開招標更正公告",
    "經公開評選或公開徵求之限制性招標公告",
    "經公開評選或公開徵求之限制性招標更正公告",
    "公開取得報價單或企劃書公告",
    "公開取得報價單或企劃書更正公告",
}

@dataclass
class Tender:
    unit: str
    job_number: str
    title: str
    category: str = ""
    publish_date: str = ""
    url: str = ""
    summary: str = ""
    budget: int | None = None
    deadline: str = ""
    award_type: str = ""
    qualification: str = ""
    bid_bond: str = ""
    performance_bond: str = ""
    detail_text: str = ""
    incumbent_risk: str = "UNKNOWN"
    incumbent_vendor: str = ""
    incumbent_ratio: float | None = None
    similar_awards: int = 0
    historical_note: str = ""
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.unit}::{self.job_number}::{self.title}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

def clean(node) -> str:
    return " ".join(node.stripped_strings) if node else ""

def parse_money(value: Any) -> int | None:
    s = str(value or "").replace("新臺幣", "").replace("元", "").strip()
    m = re.search(r"([\d,.]+)\s*萬", s)
    if m:
        return int(float(m.group(1).replace(",", "")) * 10000)
    m = re.search(r"\d[\d,]*", s)
    return int(m.group(0).replace(",", "")) if m else None

def parse_date(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    m = re.match(r"^(\d{2,3})[/.-](\d{1,2})[/.-](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", s)
    if m:
        y, mo, d, hh, mm = m.groups()
        y = int(y)
        if y < 1911:
            y += 1911
        return datetime(y, int(mo), int(d), int(hh or 0), int(mm or 0)).strftime("%Y-%m-%d %H:%M:%S")
    return s

def roc_date(yyyymmdd: str) -> str:
    dt = datetime.strptime(yyyymmdd, "%Y%m%d")
    return f"{dt.year - 1911}年{dt.month:02d}月{dt.day:02d}日"

def field_value(soup: BeautifulSoup, label: str) -> str:
    target = label.rstrip("：:")
    for cell in soup.find_all(["td", "th"]):
        if clean(cell).rstrip("：:") == target:
            sibling = cell.find_next_sibling(["td", "th"])
            return clean(sibling) if sibling else ""
    return ""

def parse_listing(text: str) -> tuple[str, str, str] | None:
    text = re.sub(r"^<\d+>\s*", "", text.strip())
    if "：" not in text:
        return None
    unit, rest = text.split("：", 1)
    if " - " in rest:
        job, title = rest.split(" - ", 1)
    elif "-" in rest:
        job, title = rest.split("-", 1)
    else:
        return None
    return unit.strip(), job.strip(), title.strip()


def text_after_label(text: str, label: str, max_len: int = 240) -> str:
    """Best-effort extraction from PCC printable text when table/ID parsing differs by notice type."""
    compact = re.sub(r"[\t\r ]+", " ", text or "")
    patterns = [
        rf"{re.escape(label)}\s*[：:]?\s*([^\n]{{1,{max_len}}})",
        rf"{re.escape(label)}\s*[：:]?\s*(.{{1,{max_len}}}?)(?=\s{{2,}}|\n|$)",
    ]
    for pattern in patterns:
        m = re.search(pattern, compact, flags=re.S)
        if m:
            return " ".join(m.group(1).strip().split())
    return ""

def money_from_text(text: str) -> int | None:
    for label in ("預算金額", "採購金額", "預算金額是否公開"):
        value = text_after_label(text, label)
        amount = parse_money(value)
        if amount:
            return amount
    # PCC often renders "預算金額 1,450,000元" in a flat printable block.
    for pattern in (
        r"預算金額[^\d]{0,40}([\d,]{4,})\s*元",
        r"預算金額[^\d]{0,40}([\d.]+)\s*萬",
    ):
        m = re.search(pattern, text or "", flags=re.S)
        if m:
            if "萬" in m.group(0):
                return int(float(m.group(1).replace(",", "")) * 10000)
            return int(m.group(1).replace(",", ""))
    return None

def deadline_from_text(text: str) -> str:
    for label in ("截止投標", "截止收件時間", "截止投標時間"):
        value = text_after_label(text, label)
        parsed = parse_date(value)
        if parsed and parsed != value:
            return parsed
        m = re.search(r"(\d{2,4}[/-]\d{1,2}[/-]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)", value)
        if m:
            return parse_date(m.group(1))
    m = re.search(r"截止投標[^\d]{0,40}(\d{2,4}[/-]\d{1,2}[/-]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)", text or "", flags=re.S)
    return parse_date(m.group(1)) if m else ""

class PCC:
    def __init__(self, delay: float = 0.3):
        self.delay = delay
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
        })

    def _get(self, url: str, **kwargs) -> requests.Response:
        last = None
        for attempt in range(4):
            try:
                time.sleep(self.delay)
                r = self.s.get(url, timeout=35, allow_redirects=True, **kwargs)
                r.raise_for_status()
                r.encoding = "utf-8"
                if "Web Page Blocked" in r.text:
                    raise RuntimeError("PCC WAF blocked response")
                return r
            except Exception as exc:
                last = exc
                if attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"Official PCC request failed: {url}: {last}")

    def daily(self, yyyymmdd: str) -> list[Tender]:
        r = self._get(DAILY_URL, params={"dateStr": roc_date(yyyymmdd)})
        soup = BeautifulSoup(r.text, "html.parser")
        out: list[Tender] = []
        for table in soup.select("table.tenderCase"):
            link = table.select_one("a.tenderLinkPublish")
            if not link:
                continue
            anchor = table.find_previous("a", id=True)
            category = str(anchor.get("id", "")).strip() if anchor else ""
            if category not in ACTIVE_SECTIONS:
                continue
            parsed = parse_listing(clean(link))
            if not parsed:
                continue
            unit, job, title = parsed
            href = str(link.get("href", "")).strip()
            url = ""
            if href:
                url = BASE + "/prkms/tender/common/noticeDate/redirectPublic?" + urlencode(
                    {"ds": yyyymmdd, "fn": href}
                )
            out.append(Tender(
                unit=unit, job_number=job, title=title, category=category,
                publish_date=yyyymmdd, url=url,
                summary=clean(table.select_one("td.summary")),
            ))
        return out

    def hydrate(self, tender: Tender) -> Tender:
        if not tender.url:
            tender.risks.append("官方 detail URL 缺失")
            return tender
        try:
            fresh = requests.Session()
            fresh.headers.update({
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
            })
            response = fresh.get(tender.url, timeout=35, allow_redirects=True)
            response.raise_for_status()
            response.encoding = "utf-8"
            html = response.text
            if "Web Page Blocked" in html:
                raise RuntimeError("PCC WAF blocked detail response")
            soup = BeautifulSoup(html, "html.parser")
        except Exception as exc:
            tender.risks.append(f"官方 detail 讀取失敗: {exc}")
            return tender

        main = soup.select_one("#print_area") or soup.body
        tender.detail_text = clean(main)[:50000] if main else ""
        flat_text = tender.detail_text or clean(soup)

        tender.budget = parse_money(field_value(soup, "預算金額"))
        if tender.budget is None:
            budget_node = soup.find(id="budget")
            if budget_node:
                tender.budget = parse_money(budget_node.get("value") or clean(budget_node))
        if tender.budget is None:
            tender.budget = money_from_text(flat_text)

        tender.deadline = parse_date(field_value(soup, "截止投標"))
        if not tender.deadline:
            for node_id in ("spdt", "tenderDeadline", "deadline"):
                deadline_node = soup.find(id=node_id)
                if deadline_node:
                    tender.deadline = parse_date(deadline_node.get("value") or clean(deadline_node))
                    if tender.deadline:
                        break
        if not tender.deadline:
            tender.deadline = deadline_from_text(flat_text)

        tender.award_type = field_value(soup, "決標方式")
        if not tender.award_type:
            award_node = soup.find(id="fkPmsAwardWay")
            if award_node:
                tender.award_type = award_node.get("value") or clean(award_node)
        if not tender.award_type:
            tender.award_type = text_after_label(flat_text, "決標方式")

        tender.qualification = (
            field_value(soup, "廠商資格摘要")
            or field_value(soup, "投標廠商資格及資格文件之附加說明")
            or text_after_label(flat_text, "廠商資格摘要", 1200)
            or text_after_label(flat_text, "投標廠商資格及資格文件之附加說明", 1200)
        )
        tender.bid_bond = (
            field_value(soup, "是否須繳納押標金")
            or field_value(soup, "押標金")
            or text_after_label(flat_text, "是否須繳納押標金")
            or text_after_label(flat_text, "押標金")
        )
        tender.performance_bond = (
            field_value(soup, "是否須繳納履約保證金")
            or field_value(soup, "履約保證金")
            or text_after_label(flat_text, "是否須繳納履約保證金")
            or text_after_label(flat_text, "履約保證金")
        )
        return tender
