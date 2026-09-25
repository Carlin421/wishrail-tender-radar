# WishRail Tender Radar

給 **贊尼工作室 / WishRail 心願快線** 使用的台灣政府資訊標案 Bid/No-Bid 雷達。

## 目標

每天自動從 **政府電子採購網官方公告** 找出比較適合小型軟體團隊切入的案件，不只做關鍵字搜尋，而是幫案件做 Bid / No-Bid 初篩。

預設策略偏向贊尼第一案：

- 地區：臺中、南投、彰化、雲林、嘉義、苗栗
- 預算：NT$15–150 萬
- 優先：新建 / 改版 / PoC / Web / 後台 / API / AI / 資料庫
- 加分：公開取得企劃書、評選/最有利標、小型地方機關
- 扣分：既有系統維運、硬體、駐點、高資安門檻、政府實績門檻
- 歷史訊號：同機關相似案件、既有得標廠商集中度、平均投標家數、單一投標比例

> Radar 分數是「值得花時間研究這案嗎？」的優先級，不是得標機率。

## 資料流程

1. 讀取政府電子採購網每日公告。
2. 先依地區與軟體關鍵字初篩。
3. 若列表已能取得預算，先淘汰明顯超出範圍的案件。
4. 進入官方標案 detail page，取得：
   - 預算
   - 截止投標
   - 決標方式
   - 廠商資格
   - 押標 / 履約保證資訊
5. 對可投案件做 scoring。
6. 嘗試補充歷史競爭 / incumbent 訊號。
7. 產生 `reports/YYYY-MM-DD.md`。

官方 detail parser 使用政府採購網目前的穩定欄位，例如 `budget`、`spdt`、`fkPmsAwardWay`、`vendorDescInput`，並帶 retry 以降低 PCC 限流造成的缺欄位。

## 本機執行

```bash
git clone https://github.com/Carlin421/wishrail-tender-radar.git
cd wishrail-tender-radar

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

PYTHONPATH=. pytest -q

# 正式掃最近 5 天
python official_radar.py --days 5 --top 15
```

輸出：

```text
reports/YYYY-MM-DD.md
```

## GitHub Actions

`.github/workflows/daily.yml`

- 程式碼 push：跑測試 + 最近 2 天快速 smoke test
- 週一到週五台灣約 08:10：跑最近 5 天正式掃描
- 只有正式排程 / 手動執行才會更新 daily report
- push smoke test **不會覆蓋正式報告**

這樣可以避免週末漏案，也避免開發測試把正式報告洗成空白。


## LINE 每日推播

排程掃描完成後可以直接把 **Top 5 標案** 推到 LINE。

程式使用 LINE Messaging API。請在 GitHub Repo：

`Settings → Secrets and variables → Actions → New repository secret`

加入：

- `LINE_CHANNEL_ACCESS_TOKEN`：LINE Messaging API 的 Channel access token
- `LINE_USER_ID`：要接收推播的 LINE User ID

沒有設定這兩個 Secret 時，Radar 仍會正常掃描與產生報告，只會略過 LINE 推播。

本機可先測格式：

```bash
python line_push.py --dry-run
```

正式排程時會自動執行：

```bash
python line_push.py
```

LINE 訊息會包含分數、建議、標案名稱、機關、預算、截止時間、Incumbent Risk 與官方連結。

## 報告怎麼看

粗略可解讀為：

- **78+**：優先 BID / 領標研究
- **65–77**：值得研究
- **52–64**：觀察，先看完整規格
- **<52**：低優先

如果預算或截止時間沒有成功取得，系統會限制最高分，避免不完整資料被誤判為高優先案件。

## 重要限制

自動化只能幫你做第一層 Bid / No-Bid。

正式投標前仍必須人工確認：

- 廠商資格
- 完整需求規格書
- 評選項目 / 配分
- 契約與付款條件
- 驗收條件
- 原始碼 / 智財權
- 保固與維護
- 資安要求
- 紙本 / 電子投標方式

歷史 incumbent / 投標家數資料屬於輔助訊號；樣本不足時會標示 UNKNOWN / LOW_CONFIDENCE，而不是硬猜。
