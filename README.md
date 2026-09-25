# WishRail Tender Radar

給 **贊尼工作室 / WishRail 心願快線** 使用的台灣政府資訊標案 Bid/No-Bid 雷達。

## 核心流程

每天自動：

1. 從 PCC/g0v API 抓新公告。
2. 篩軟體 / 網站 / AI / API 類案件。
3. 以台中、南投、彰化、雲林、嘉義、苗栗為目標區域。
4. 讀取預算、截止、資格、押標金、決標方式等明細。
5. 查同機關歷史相似案。
6. 從歷史決標資料抓「得標廠商」並計算 incumbent dominance。
7. 依「贊尼第一案」策略打 0–100 分。
8. 產出每日 Markdown 報告；可選 LINE Messaging API 推播。

> 目標不是找最多案，而是找出「贊尼值得投的案」，並避開被既有廠商長期壟斷的標案。

## 本機執行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -q

python run.py scan --days 2 --skip-incumbent
python run.py scan --days 2
```

## GitHub Actions

`.github/workflows/daily.yml` 預設台灣工作日約 08:10 自動執行，也支援手動觸發。

## 注意

這個分數是 **Bid/No-Bid 優先級**，不是得標機率。正式投標前仍要人工閱讀完整招標文件、資格、契約、驗收與資安要求。
