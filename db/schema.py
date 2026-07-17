"""版本化 schema：集中建表／遷移，避免各 cog 各自 ALTER。"""
from __future__ import annotations

import logging
import sqlite3
from typing import Iterable

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

# (version, description, sql statements)
_MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        1,
        "core tables",
        [
            """
            CREATE TABLE IF NOT EXISTS exp_history (
                record_time TIMESTAMP,
                server_name TEXT,
                player_name TEXT,
                level INTEGER,
                exp REAL,
                class_name TEXT DEFAULT '未知',
                subjugation_grade INTEGER DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS transfer_alerts_log (
                old_name TEXT,
                old_server TEXT,
                new_name TEXT,
                new_server TEXT,
                alert_time TIMESTAMP,
                PRIMARY KEY (old_name, old_server, new_name, new_server)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS member_registry (
                player_name TEXT PRIMARY KEY,
                original_identity TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS cmd_dedupe (
                message_id INTEGER PRIMARY KEY,
                claimed_at TEXT NOT NULL,
                pid INTEGER,
                host TEXT
            )
            """,
        ],
    ),
    (
        2,
        "legacy column backfill + light indexes",
        [
            # 舊庫可能缺欄位；用 PRAGMA 檢查後在 apply 裡補
        ],
    ),
]

_SEARCH_INDEXES: list[tuple[str, str]] = [
    (
        "idx_exp_history_unique",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_exp_history_unique
        ON exp_history(record_time, server_name, player_name)
        """,
    ),
    (
        "idx_time_server",
        "CREATE INDEX IF NOT EXISTS idx_time_server ON exp_history(record_time, server_name)",
    ),
    (
        "idx_player_server",
        "CREATE INDEX IF NOT EXISTS idx_player_server ON exp_history(player_name, server_name)",
    ),
    (
        "idx_exp",
        "CREATE INDEX IF NOT EXISTS idx_exp ON exp_history(exp)",
    ),
    (
        "idx_class_exp_time",
        "CREATE INDEX IF NOT EXISTS idx_class_exp_time ON exp_history(class_name, exp, record_time)",
    ),
]


async def _get_schema_version(db) -> int:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    async with db.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'"
    ) as cursor:
        row = await cursor.fetchone()
    if row:
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return 0
    # 舊庫：有 exp_history 但沒 meta → 視為已到 v1，再跑後續遷移
    async with db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='exp_history'"
    ) as cursor:
        if await cursor.fetchone():
            return 1
    return 0


async def _set_schema_version(db, version: int) -> None:
    await db.execute(
        """
        INSERT INTO schema_meta (key, value) VALUES ('version', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (str(version),),
    )


async def _table_columns(db, table: str) -> set[str]:
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        rows = await cursor.fetchall()
    return {row[1] for row in rows}


async def _ensure_exp_history_columns(db) -> None:
    cols = await _table_columns(db, "exp_history")
    if not cols:
        return
    if "class_name" not in cols:
        await db.execute(
            "ALTER TABLE exp_history ADD COLUMN class_name TEXT DEFAULT '未知'"
        )
    if "subjugation_grade" not in cols:
        await db.execute(
            "ALTER TABLE exp_history ADD COLUMN subjugation_grade INTEGER DEFAULT 0"
        )


async def apply_migrations(db) -> int:
    """套用至 SCHEMA_VERSION；回傳套用後版本。"""
    current = await _get_schema_version(db)
    for version, desc, statements in _MIGRATIONS:
        if version <= current:
            continue
        logger.info(f"⏳ schema 遷移 v{version}: {desc}")
        for sql in statements:
            sql = sql.strip()
            if sql:
                await db.execute(sql)
        if version >= 1:
            await _ensure_exp_history_columns(db)
        await _set_schema_version(db, version)
        await db.commit()
        current = version
        logger.info(f"✅ schema 已至 v{version}")

    # 即使已是最新版，仍確保欄位齊全（手動改庫情境）
    await _ensure_exp_history_columns(db)
    if current < SCHEMA_VERSION:
        await _set_schema_version(db, SCHEMA_VERSION)
        current = SCHEMA_VERSION
    await db.commit()
    return current


async def ensure_search_indexes(
    db,
    *,
    skip_if_rows_above: int = 50_000,
    index_names: Iterable[str] | None = None,
) -> list[str]:
    """背景建立尋人／測速用索引；大表略過以免鎖寫入。

    回傳本次新建的索引名稱列表。
    """
    wanted = list(_SEARCH_INDEXES)
    if index_names is not None:
        allow = set(index_names)
        wanted = [item for item in wanted if item[0] in allow]

    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ) as cursor:
        existing = {row[0] for row in await cursor.fetchall()}

    missing = [item for item in wanted if item[0] not in existing]
    if not missing:
        return []

    try:
        async with db.execute("SELECT COUNT(*) FROM exp_history") as cursor:
            row_count = (await cursor.fetchone())[0]
    except sqlite3.DatabaseError as e:
        logger.warning(f"無法讀取 exp_history 筆數，略過索引: {e}")
        return []

    if row_count > skip_if_rows_above:
        names = ", ".join(n for n, _ in missing)
        logger.warning(
            f"⚠️ exp_history 約 {row_count} 筆，略過啟動時建立索引: {names}。"
            " 可離線執行 cleanup 後再以較小庫重建，或調高門檻。"
        )
        return []

    created: list[str] = []
    for name, ddl in missing:
        logger.info(f"⏳ 背景建立資料庫索引 {name}...")
        try:
            await db.execute(ddl)
            await db.commit()
            created.append(name)
            logger.info(f"✅ 索引 {name} 建立完成")
        except sqlite3.DatabaseError as e:
            logger.warning(f"⚠️ 無法建立索引 {name}: {e}")
    return created
