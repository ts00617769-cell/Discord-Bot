# 波拉西亞戰記 Discord 戰情機器人

專為《波拉西亞戰記》旅團／戰情室打造的 Discord bot：排名掃描、經驗測速、轉服尋人、聯賽與娛樂指令。

## 需求

- Python 3.10+
- 穩定網路（需存取 beanfun 官網 Ranking API）

## 安裝

```bash
pip install -r requirements.txt
copy .env.example .env   # Windows
# cp .env.example .env   # macOS / Linux
```

編輯 `.env`，至少填入：

| 變數 | 說明 |
|------|------|
| `DISCORD_TOKEN` | 機器人 Token（必填） |
| `ALLOWED_COMMAND_CHANNELS` | 機密指令白名單頻道 ID（逗號分隔；**留空則全部拒絕**） |
| `EXP_ALERT_CHANNEL_ID` | 超速警報頻道 |
| `BOSS_REMINDER_CHANNEL_ID` | 時空縫隙提醒頻道 |
| `QUIZ_CHANNEL_ID` | 每日測驗頻道 |

其餘選填見 `.env.example`（含 `DB_PATH`、`RANKING_CACHE_TTL` 等）。

## 啟動

```bash
python bot.py
```

看到登入成功與模組掛載訊息即表示就緒。同一台機器若重複啟動，檔案鎖會阻擋第二個實例（避免指令回兩次）。

## 架構摘要

| 目錄／模組 | 職責 |
|------------|------|
| `bot.py` | 啟動、單例鎖、指令去重、載入 cogs |
| `cogs/` | Discord 指令與排程 |
| `db/` | SQLite 連線 PRAGMA、版本化 schema／索引 |
| `services/` | 與 Discord 解耦的純邏輯（尋人匹配等） |
| `game_data.py` | `SERVER_MAP`、時空縫隙時間表 |
| `cleanup_db.py` | **離線**清理過期資料並 VACUUM |

### 資料庫

- 預設檔案：`prasia_data.db`（可用 `DB_PATH` 改到更快的磁碟）
- WAL + `busy_timeout` + mmap／cache 調校，適合單寫入長跑
- Schema 由 `db.schema.apply_migrations` 版本化管理
- 大表索引在背景建立；超過約 5 萬筆時啟動會略過以免鎖寫入
- 壓縮空間請先停 bot，再執行：`python cleanup_db.py`

### 官方 API

- 客戶端：`cogs/ranking_api.py`（全 bot 共用 session + 併發上限 5）
- 失敗自動重試（timeout／5xx）；成功結果短 TTL 快取（預設 45 秒，`RANKING_CACHE_TTL`）
- 開服／合服後請更新 `SERVER_MAP`，並用 `!伺服器檢查` 探活

## 常用指令

| 指令 | 說明 |
|------|------|
| `!指令` / `!機密指令` | 說明手冊 |
| `!排名` / `!測速` / `!尋人` | 機密（限白名單頻道） |
| `!聯賽` / `!討伐排名` / `!稅收` / `!時空王` | 公開 |
| `!警報 開/關` | 超速警報 |
| `!reload <模組>` | 擁有者熱重載 |

完整列表見遊戲內 `!指令`。

## 開發與測試

```bash
pip install -r requirements-dev.txt
pytest -q
```

推送／PR 會跑 GitHub Actions（本機同款 `pytest`，不連 Discord／beanfun）。

### 架構補充

- `services/`：轉服／測速等純邏輯（可單測）
- `cogs/beanfun_http.py`：聯賽／稅收／時空王／排名共用的 JSON POST（重試＋快取）
- Schema v3 起含 quiz／星座快取表；勿在 cog 內自行 `CREATE TABLE`
- 戰情室每週日 09:00（UTC+8）發送健康摘要；大量 DELETE 後請離線 `python cleanup_db.py`

## 授權與資料來源

排名資料來源：[Wars of Prasia 官網](https://warsofprasia.beanfun.com/) Ranking API。本專案為非官方旅團輔助工具。
