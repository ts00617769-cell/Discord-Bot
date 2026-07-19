"""SQLite 連線與效能 PRAGMA（單寫入、WAL 模式）。"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

# 預設放專案根目錄；可用 DB_PATH 改到更快的磁碟
DEFAULT_DB_NAME = "prasia_data.db"


def resolve_db_path(base_dir: str | Path | None = None) -> Path:
    """解析資料庫路徑：環境變數 DB_PATH 優先。"""
    env = (os.getenv("DB_PATH") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    root = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent
    return (root / DEFAULT_DB_NAME).resolve()


async def configure_connection(db: aiosqlite.Connection) -> None:
    """套用適合長跑 bot 的 PRAGMA（WAL + 合理快取，降低鎖庫機率）。"""
    # WAL：讀寫較不易互擋；busy_timeout：忙時等待而非立刻失敗
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=30000")
    # NORMAL：WAL 下足夠安全，比 FULL 寫入負擔小
    await db.execute("PRAGMA synchronous=NORMAL")
    # 負值單位為 KB：約 64MB page cache
    await db.execute("PRAGMA cache_size=-65536")
    await db.execute("PRAGMA temp_store=MEMORY")
    # mmap 加速大表掃描（尋人／轉服）；失敗則略過
    try:
        await db.execute("PRAGMA mmap_size=268435456")
    except (OSError, ValueError) as e:
        logger.warning(f"PRAGMA mmap_size 略過: {e}")
    # 外鍵預留（目前 schema 未強制 FK，但開啟無害）
    await db.execute("PRAGMA foreign_keys=ON")


async def connect_db(db_path: str | Path | None = None) -> aiosqlite.Connection:
    """開啟並設定好 PRAGMA 的 aiosqlite 連線。"""
    path = Path(db_path) if db_path else resolve_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(path))
    await configure_connection(db)
    logger.info(f"✅ 資料庫已連接: {path}")
    return db
