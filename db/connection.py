"""SQLite 連線與效能 PRAGMA（單寫入、WAL 模式）。"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

# 預設放專案根目錄；可用 DB_PATH 改到更快的磁碟
DEFAULT_DB_NAME = "prasia_data.db"


class DatabaseIntegrityError(RuntimeError):
    """PRAGMA quick_check 失敗。"""


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


async def integrity_quick_check(db: aiosqlite.Connection) -> None:
    """執行 PRAGMA quick_check；失敗則拋 DatabaseIntegrityError。"""
    async with db.execute("PRAGMA quick_check") as cursor:
        rows = list(await cursor.fetchall())
    if not rows:
        raise DatabaseIntegrityError("PRAGMA quick_check 無結果")
    ok = len(rows) == 1 and str(rows[0][0]).lower() == "ok"
    if not ok:
        detail = "; ".join(str(r[0]) for r in rows[:5])
        raise DatabaseIntegrityError(f"資料庫完整性檢查失敗: {detail}")


async def connect_db(
    db_path: str | Path | None = None,
    *,
    check_integrity: bool = True,
) -> aiosqlite.Connection:
    """開啟並設定好 PRAGMA 的 aiosqlite 連線（讀寫）。"""
    path = Path(db_path) if db_path else resolve_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(path))
    await configure_connection(db)
    if check_integrity:
        try:
            await integrity_quick_check(db)
        except DatabaseIntegrityError:
            await db.close()
            raise
    logger.info(f"✅ 資料庫已連接: {path}")
    return db


async def connect_db_ro(
    db_path: str | Path | None = None,
) -> aiosqlite.Connection:
    """開啟唯讀連線（URI mode=ro），供尋人／健康摘要等重查詢。"""
    path = Path(db_path) if db_path else resolve_db_path()
    if not path.exists():
        # 新庫尚無檔案時，先讓讀寫連線建立，再開 ro
        raise FileNotFoundError(f"資料庫不存在，無法開唯讀連線: {path}")
    uri = path.resolve().as_uri() + "?mode=ro"
    db = await aiosqlite.connect(uri, uri=True)
    await db.execute("PRAGMA busy_timeout=30000")
    await db.execute("PRAGMA cache_size=-65536")
    await db.execute("PRAGMA temp_store=MEMORY")
    try:
        await db.execute("PRAGMA mmap_size=268435456")
    except (OSError, ValueError) as e:
        logger.warning(f"PRAGMA mmap_size (ro) 略過: {e}")
    logger.info(f"✅ 唯讀資料庫已連接: {path}")
    return db


def read_db(bot) -> aiosqlite.Connection:
    """優先使用 bot.db_ro；未開則回退 bot.db。"""
    return getattr(bot, "db_ro", None) or bot.db
