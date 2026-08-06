# 波拉西亞戰記 Discord 戰情機器人

專為《波拉西亞戰記》旅團／戰情室打造的 Discord bot：即時排名、經驗測速、轉服尋人、稅收掃描，以及時空縫隙與娛樂指令。

資料來源：[Wars of Prasia 官網](https://warsofprasia.beanfun.com/) Ranking 等 API（非官方工具）。

---

## 需求

- Python **3.10+**
- 可連外網（beanfun 官網 API）
- Discord 應用程式需開啟 **Message Content Intent**（本 bot **不需** Members Intent）
- 專案根目錄需有 `omikuji.json`（`!求籤`）、`quiz.json`（心理測驗）

---

## 安裝

```bash
pip install -r requirements.txt
copy .env.example .env   # Windows
# cp .env.example .env   # macOS / Linux
```

編輯 `.env` 後啟動：

```bash
python bot.py
```

看到登入成功與模組掛載即就緒。同一台機器若重複啟動，檔案鎖（`.bot.lock`）會擋住第二個實例，避免指令回兩次。

開發依賴（測試／lint）：

```bash
pip install -r requirements-dev.txt
pytest -q
ruff check .
```

---

## 環境變數

完整註解見 [`.env.example`](.env.example)。摘要如下。

### 必填／強烈建議

| 變數 | 說明 |
|------|------|
| `DISCORD_TOKEN` | 機器人 Token |
| `ALLOWED_COMMAND_CHANNELS` | 機密指令白名單頻道 ID（逗號分隔）。**留空＝全部拒絕（fail-closed）** |
| `EXP_ALERT_CHANNEL_ID` | 超速警報頻道 |
| `BOSS_REMINDER_CHANNEL_ID` | 時空縫隙 Boss 提醒頻道 |
| `QUIZ_CHANNEL_ID` | 每日盲投測驗頻道 |

### 選填

| 變數 | 預設／說明 |
|------|------------|
| `TRANSFER_ALERT_CHANNEL_ID` | 轉服／改名警報頻道（未設則不發） |
| `WAR_ROOM_CHANNEL_ID` | 戰情室日誌／錯誤／健康摘要 |
| `QUIZ_POST_TIME` / `QUIZ_REVEAL_TIME` | 盲投發布／開獎時間（台北，預設 `12:00` / `18:00`） |
| `EXP_ALERT_THRESHOLD` | 超速門檻（經驗／小時，預設 `200000000000`＝2000 億） |
| `EXP_ALERT_SEND_CLEAR` | 設 `1` 時無超速者也送綠訊巡檢（預設關閉） |
| `SNAPSHOT_MIN_SERVERS` | 完整快照最少「品質達標」服數（預設＝`SERVER_MAP` 全服） |
| `SNAPSHOT_MIN_PLAYERS` | 單服算完整所需最少玩家人數（預設 `30`） |
| `DB_PATH` | SQLite 路徑（預設專案根 `prasia_data.db`） |
| `SKIP_DB_QUICK_CHECK` | 設 `1` 略過啟動 `PRAGMA quick_check`（大庫／NAS 可暫用） |
| `RANKING_CACHE_TTL` | Ranking 成功快取秒數（預設 `45`；`0`＝關閉） |

跨主機共用同一 `DB_PATH` 時，啟動會以 `bot_instance_lock` heartbeat 互斥，避免雙開重複警報。

開服／合服後請更新 [`game_data.py`](game_data.py) 的 `SERVER_MAP`，再用擁有者指令 `!伺服器檢查` 探活。

---

## 指令一覽

前綴皆為 `!`；`尋人`／`排名`／`測速` 另支援 hybrid slash。遊戲內可用 `!指令`／`!機密指令` 查看。

### 公開（任何頻道）

| 指令 | 說明 |
|------|------|
| `!指令` | 公開操作手冊 |
| `!討伐` / `!討伐排名` | 各服總榜+職業榜合併後依討伐重排 TOP 100 |
| `!稅收 [數量] [伺服器]` | 據點鑽石／紅寶石稅收 |
| `!時空王 [伺服器]` | 時空縫隙擊殺／MVP 戰報（別名：`!交叉王` 等） |
| `!時空` | 今日時空縫隙召喚時段 |
| `!鍊成 [階級]` | 四合一鍊成模擬 |
| `!塔羅` | 抽大阿爾克那 |
| `!星座` / `!運勢 [星座]` | 每日星座運勢 |
| `!求籤 [問題]` | 廟宇求籤 |
| `!測驗` | 即開心理測驗 |

### 機密（限 `ALLOWED_COMMAND_CHANNELS`，討論串／論壇貼文會一併檢查父頻道）

| 指令 | 說明 |
|------|------|
| `!機密指令` | 機密手冊 |
| `!排名 [數量] [伺服器] [職業…]` | 即時經驗排名；職業名可對應 API 時只打該職業榜 |
| `!測速 [數量] [伺服器]` | 依快照計算練功時速 |
| `!警報 開 [數量] [伺服器] [旅團名稱]` / `!警報 關` | 僅監控指定伺服器與旅團的自動超速警報（設定會寫入 DB，重啟保留） |
| `!尋人 [名稱] [伺服器?]` | 依經驗特徵追蹤改名／轉服；可加伺服器縮小範圍 |
| `!轉服掃描` / `!移民清單` / `!抓包` | 近期轉服候選清單 |
| `!測試轉移警報` | 確認轉服警報頻道（白名單或轉移警報頻道皆可） |

### 擁有者

| 指令 | 說明 |
|------|------|
| `!定時測驗` / `!測試開獎` | 手動發布盲投／提早開獎 |
| `!重建履歷` / `!重建履歷 全量` | 增量或全量重建 `player_profile` denorm（尋人變慢或健康摘要提示時） |
| `!伺服器檢查` | 對 `SERVER_MAP` 打 Ranking API 探活 |
| `!檢查廟宇` | 廟宇資料維護 |

---

## 背景排程（無需下指令）

| 任務 | 行為 |
|------|------|
| 經驗快照 | 約每 **10 分鐘** 抓各服排名寫入 `exp_history` |
| 超速巡檢 | 需指定伺服器與旅團後執行 `!警報 開`；每 10 分鐘一輪，完整區間去重避免重發；綠訊需 `EXP_ALERT_SEND_CLEAR=1`，旅團查無資料的黃訊每小時最多一次 |
| 轉服偵測 | 僅在本輪「品質達標服數」≥ `SNAPSHOT_MIN_SERVERS` 時執行；即使未設轉服警報頻道，仍會定期清理過期的 `transfer_missing` 佇列 |
| Boss 提醒 | 召喚前 10 分鐘內 `@everyone`；錯過（重啟／延遲）會在剩餘分鐘內補送，DB 去重確保只送一次 |
| 每日盲投 | 依 `QUIZ_POST_TIME`／`QUIZ_REVEAL_TIME` 發布與開獎 |
| DB 清理 | 每日台北時間 **04:15** 執行；從最近 1 次官方轉移窗到現在連續保留，近 **3** 天保留 10 分鐘快照，較舊橋接區同角同服只留首尾；另清過期轉服 log／`alert_dedupe`／`transfer_missing`／`cmd_dedupe`；大量刪除後建議離線 VACUUM |
| 健康摘要 | 每週日 09:00（台北）發到 `WAR_ROOM_CHANNEL_ID`（若有設） |

快照「品質達標」條件：該服**總榜成功**，且合併玩家人數 ≥ `SNAPSHOT_MIN_PLAYERS`。部分職業榜失敗仍會寫入可用資料，但該服不計入完整快照門檻，以降低假轉服。

---

## 架構

| 路徑 | 職責 |
|------|------|
| [`bot.py`](bot.py) | 啟動、單例鎖、共用 session／DB、指令去重、自動載入 cogs |
| [`cogs/`](cogs/) | Discord 指令與 `tasks.loop` 排程 |
| [`services/`](services/) | 與 Discord 解耦的邏輯（HTTP、排名、測速、轉服、時區、顯示） |
| [`db/`](db/) | SQLite 連線 PRAGMA、版本化 schema／索引 |
| [`game_data.py`](game_data.py) | `SERVER_MAP`、Boss 時段等常數 |
| [`cleanup_db.py`](cleanup_db.py) | **離線**刪過期資料並 VACUUM（會讀 `.env` 的 `DB_PATH`） |
| [`Dockerfile`](Dockerfile) / [`compose.example.yaml`](compose.example.yaml) | 映像建置時安裝依賴；部署請 `cp compose.example.yaml compose.yaml` 並用 `.env` |
| [`tests/`](tests/) | 純邏輯單元測試（不連 Discord／beanfun） |

### 資料庫

- 預設檔：`prasia_data.db`（可用 `DB_PATH` 指向 SSD）
- WAL + `busy_timeout` + mmap／cache；適合單寫入長跑
- Schema 由 `db.schema.apply_migrations` 版本化；**勿在 cog 內自行 `CREATE TABLE`**
- 大表索引背景建立；列數過大時啟動會略過以免鎖寫入
- 壓縮請先**停止 bot**，再執行：

```bash
python cleanup_db.py
python cleanup_db.py --days 30 --dry-run
python cleanup_db.py --for-search --dry-run   # 尋人導向：近3天 ∪ 最近1次轉移窗～結束後再+3天；窗+pad 同角同服只留首尾
python cleanup_db.py --for-search             # 實際刪除、VACUUM，並建立尋人索引 + player_profile（NAS 大庫必做）
python cleanup_db.py --build-indexes          # 僅離線建索引（大庫啟動時會略過）
python cleanup_db.py --wipe-history   # 清空歷史表含 transfer_missing（慎用）
```

> `exp_history` 超過約 5 萬筆時，bot 啟動**不會**自動建索引（避免鎖庫）。清庫後務必跑 `--for-search` 或 `--build-indexes`，否則 `!尋人` 仍會全表掃描而很慢。
>
> NAS 部署：最近 3 天每 10 分鐘保留一輪，確保警報即時；上次官方轉移窗至最近 3 天之間會連續保留，但每日清理成同角同服首尾，避免時間斷層與容量暴增。尋人逾時時請**停 bot** 後跑 `--for-search`（含 VACUUM）。
>
> `--wipe-history` 會一併清空 `transfer_missing`（消失佇列），避免舊列在下一轉移窗誤報。
>
> 跨主機防雙開依賴所有實例使用同一個 `DB_PATH`；若兩台各用自己的 DB，任何 SQLite 鎖都無法互相看見。
>
> 離線 `cleanup_db.py` 除本機 `.bot.lock` 外，也會拒絕在仍有活躍 `bot_instance_lock` heartbeat 的共享庫上清理（避免他機 bot 仍在寫入）。請先停掉所有 bot 實例，或等 heartbeat 過期；緊急才加 `--force`（會印出警告）。

### HTTP／Ranking

- 共用 [`services/beanfun_http.py`](services/beanfun_http.py)：semaphore（預設併發 5）、timeout／5xx 重試、短 TTL 快取
- 排名客戶端：[`services/ranking_api.py`](services/ranking_api.py)
- 時間一律 **Asia/Taipei**（[`services/timeutil.py`](services/timeutil.py)）

---

## 開發與 CI

```bash
pytest -q
ruff check .
python -X utf8 -m mypy services db bot.py cogs cleanup_db.py
```

推送／PR 會跑 GitHub Actions：本機同款 `ruff` + `pytest` + `mypy`（不連 Discord／beanfun）。

---

## 授權與免責

本專案為旅團自用輔助工具，與遊戲橘子／官方無關。排名與戰況資料來自公開官網 API，使用請遵守 Discord 與遊戲服務條款。
