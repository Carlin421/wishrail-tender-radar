from __future__ import annotations

import argparse
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TAIPEI = ZoneInfo("Asia/Taipei")
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


def latest_report(report_dir: str = "reports") -> Path:
    folder = Path(report_dir)
    today = folder / f"{datetime.now(TAIPEI).strftime('%Y-%m-%d')}.md"
    if today.exists():
        return today
    reports = sorted(folder.glob("*.md"), reverse=True)
    if not reports:
        raise FileNotFoundError("No tender report found")
    return reports[0]


def parse_top_rows(markdown: str, limit: int = 5) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for raw in markdown.splitlines():
        line = raw.strip()
        if not line.startswith("|**"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 8:
            continue
        score = re.sub(r"[*]", "", cells[0]).strip()
        title_cell = cells[2]
        m = re.match(r"\[(.*?)\]\((.*?)\)", title_cell)
        if m:
            title, url = m.group(1).strip(), m.group(2).strip()
        else:
            title, url = title_cell, ""
        rows.append({
            "score": score,
            "recommendation": cells[1],
            "title": title,
            "url": url,
            "unit": cells[3],
            "budget": cells[4],
            "deadline": cells[5],
            "incumbent": cells[6],
            "category": cells[7],
        })
        if len(rows) >= limit:
            break
    return rows


def format_line_message(markdown: str, limit: int = 5) -> str:
    rows = parse_top_rows(markdown, limit)
    date_match = re.search(r"WishRail Tender Radar — (\d{4}-\d{2}-\d{2})", markdown)
    date_text = date_match.group(1) if date_match else datetime.now(TAIPEI).strftime("%Y-%m-%d")

    lines = [f"🔎 WishRail Tender Radar | {date_text}"]
    if not rows:
        lines += ["", "今天沒有符合目前條件的中部軟體標案。"]
        return "\n".join(lines)

    for i, row in enumerate(rows, 1):
        lines += [
            "",
            f"{i}. {row['score']}/100 {row['recommendation']}",
            row["title"],
            f"{row['unit']} | {row['budget']}",
            f"截止：{row['deadline']}",
            f"Incumbent：{row['incumbent']}",
        ]
        if row["url"]:
            lines.append(row["url"])

    lines += ["", "完整分析請看 GitHub daily report。"]
    return "\n".join(lines)[:4900]


def push_line(message: str, token: str, target_id: str) -> None:
    response = requests.post(
        LINE_PUSH_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "to": target_id,
            "messages": [{"type": "text", "text": message}],
        },
        timeout=20,
    )
    response.raise_for_status()


def main() -> int:
    parser = argparse.ArgumentParser(description="Push Tender Radar Top 5 to LINE")
    parser.add_argument("--report", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    report_path = Path(args.report) if args.report else latest_report()
    markdown = report_path.read_text(encoding="utf-8")
    message = format_line_message(markdown)

    if args.dry_run:
        print(message)
        return 0

    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    target_id = os.getenv("LINE_USER_ID", "").strip()
    if not token or not target_id:
        print("LINE secrets are not configured; skipping push.")
        return 0

    push_line(message, token, target_id)
    print(f"LINE push sent using report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
