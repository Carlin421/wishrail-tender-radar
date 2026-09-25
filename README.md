# WishRail Tender Radar

給 **贊尼工作室 / WishRail 心願快線** 使用的台灣政府資訊標案 Bid/No-Bid 雷達。

## 目前會做什麼

- 每日抓政府採購公告
- 鎖定台中、南投、彰化、雲林、嘉義、苗栗
- 預設鎖定 NT$15–150 萬
- 篩軟體 / 網站 / AI / API / 資料庫類標案
- 自動讀預算、截止、資格與決標資訊
- 分析同機關歷史相似案件
- 計算 incumbent dominance（既有廠商優勢）
- 依「贊尼第一案」策略給 0–100 Bid/No-Bid 分數
- 產生 `reports/YYYY-MM-DD.md`

## 本機執行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

pytest -q

# 快速版
python radar.py --days 2 --skip-incumbent

# 完整版（含歷史得標廠商）
python radar.py --days 2
```

## GitHub Actions

`.github/workflows/daily.yml`

- Push：跑測試 + 快速真實 API 掃描
- 週一到週五台灣約 08:10：跑完整 incumbent 分析
- 成功後自動 commit 當日報告

如果匿名 API 遇到流量限制，可設定 repo secret：

`OPENFUN_TOKEN`

Token 可從 data.openfun.tw 取得。

> 分數是 Bid/No-Bid 優先級，不是得標機率。正式投標前仍須人工閱讀完整招標文件、資格、契約、驗收與資安要求。
