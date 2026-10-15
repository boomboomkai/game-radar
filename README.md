# 遊戲情報雷達

排程跑 Python 抓資料 → 寫成 `data.json` → `index.html` 讀檔顯示。

## 部署(約 5 分鐘)
1. 在 GitHub 建立新 repo,把這個資料夾的所有檔案(含 `.github`)推上去。
2. Settings → Pages → Source 選 `Deploy from a branch`,Branch 選 `main` / `(root)`。
3. Actions 頁 → `update-data` → `Run workflow`,手動跑第一次。
4. 之後每 6 小時自動更新。網址是 `https://<帳號>.github.io/<repo名>/`。

## 本機測試
    python fetch_data.py
    python -m http.server 8000   # 瀏覽 http://localhost:8000(直接開檔案讀不到 data.json)

## 遊戲詳情與價格走勢
- 每款限免/特價遊戲可展開「畫面、影片、評論與價格走勢」,資料來自 Steam 商店,存在 `games.json`。
- 價格走勢是 Actions 每天記錄一筆台幣價格到 `history.json`,**從你部署那天才開始累積**,前幾天圖表會是空的。
- 另外會顯示 CheapShark 記錄的歷史最低價(美元)。
- 只有找得到 Steam 頁面的遊戲才有詳情;Epic 限免會用遊戲名稱去 Steam 比對,名稱不完全相符就不顯示。
- 評論是玩家原文,內容我不會審核。

## 注意
- `data.json` 的 `errors` 會記錄哪個來源失敗,失敗的來源會保留上一次的資料。
- 端點是非官方的,改版時要修 `fetch_data.py` 對應的 `parse_*` 函式。
- Repo 長時間沒活動時,GitHub 可能停用排程,到 Actions 頁重新啟用即可。
