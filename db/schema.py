"""版本化 schema：集中建表／遷移，避免各 cog 各自 ALTER。"""
from __future__ import annotations

import logging
import sqlite3
from typing import Iterable

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 7

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
    (
        3,
        "quiz + horoscope tables",
        [
            """
            CREATE TABLE IF NOT EXISTS quiz_history (
                quiz_id TEXT PRIMARY KEY,
                used_date TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS active_quiz_status (
                id INTEGER PRIMARY KEY,
                is_active INTEGER,
                quiz_id TEXT,
                channel_id TEXT,
                date_str TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS quiz_votes (
                user_id TEXT PRIMARY KEY,
                user_name TEXT,
                choice TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS horoscope_cache (
                date TEXT,
                sign TEXT,
                content TEXT,
                PRIMARY KEY (date, sign)
            )
            """,
        ],
    ),
    (
        4,
        "player_profile for search class lookup",
        [
            """
            CREATE TABLE IF NOT EXISTS player_profile (
                player_name TEXT NOT NULL,
                server_name TEXT NOT NULL,
                class_name TEXT NOT NULL DEFAULT '未知',
                updated_at TIMESTAMP,
                PRIMARY KEY (player_name, server_name)
            )
            """,
        ],
    ),
    (
        5,
        "player_profile denorm stats + covering search indexes",
        [
            # 欄位以 _ensure_player_profile_columns 補齊（舊庫 ALTER）
        ],
    ),
    (
        6,
        "alert_dedupe table + transfer log time index",
        [
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
            CREATE TABLE IF NOT EXISTS alert_dedupe (
                kind TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                PRIMARY KEY (kind, dedupe_key)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_alert_dedupe_created
            ON alert_dedupe(created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_transfer_alerts_time
            ON transfer_alerts_log(alert_time)
            """,
        ],
    ),
    (
        7,
        "guild_name on history/profile + transfer_missing queue",
        [
            """
            CREATE TABLE IF NOT EXISTS transfer_missing (
                player_name TEXT NOT NULL,
                server_name TEXT NOT NULL,
                last_seen TIMESTAMP NOT NULL,
                last_exp REAL NOT NULL,
                level INTEGER,
                class_name TEXT,
                subjugation_grade INTEGER,
                guild_name TEXT,
                miss_count INTEGER NOT NULL DEFAULT 1,
                window_label TEXT,
                created_at TIMESTAMP NOT NULL,
                resolved_at TIMESTAMP,
                PRIMARY KEY (player_name, server_name)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_transfer_missing_open
            ON transfer_missing(resolved_at, last_seen)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_transfer_missing_guild
            ON transfer_missing(guild_name, server_name, resolved_at)
            """,
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
    (
        "idx_exp_player_server",
        """
        CREATE INDEX IF NOT EXISTS idx_exp_player_server
        ON exp_history(exp, player_name, server_name)
        """,
    ),
    (
        "idx_player_server_time",
        """
        CREATE INDEX IF NOT EXISTS idx_player_server_time
        ON exp_history(player_name, server_name, record_time)
        """,
    ),
    (
        "idx_profile_min_exp",
        "CREATE INDEX IF NOT EXISTS idx_profile_min_exp ON player_profile(min_exp)",
    ),
    (
        "idx_profile_max_exp",
        "CREATE INDEX IF NOT EXISTS idx_profile_max_exp ON player_profile(max_exp)",
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


_PRAGMA_TABLE_ALLOWLIST = frozenset(
    {
        "exp_history",
        "player_profile",
        "member_registry",
        "transfer_alerts_log",
        "bot_settings",
        "cmd_dedupe",
        "schema_meta",
        "quiz_history",
        "active_quiz_status",
        "quiz_votes",
        "horoscope_cache",
        "alert_dedupe",
        "transfer_missing",
    }
)


async def _table_columns(db, table: str) -> set[str]:
    if table not in _PRAGMA_TABLE_ALLOWLIST:
        raise ValueError(f"PRAGMA table_info refused for table={table!r}")
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
    if "guild_name" not in cols:
        await db.execute(
            "ALTER TABLE exp_history ADD COLUMN guild_name TEXT DEFAULT ''"
        )


_PLAYER_PROFILE_STAT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("min_exp", "REAL"),
    ("max_exp", "REAL"),
    ("first_seen", "TIMESTAMP"),
    ("last_seen", "TIMESTAMP"),
    ("max_level", "INTEGER"),
    ("max_sub_grade", "INTEGER"),
    ("guild_name", "TEXT"),
)


async def _ensure_player_profile_columns(db) -> None:
    """確保 player_profile 存在且含 denorm 統計欄位。"""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS player_profile (
            player_name TEXT NOT NULL,
            server_name TEXT NOT NULL,
            class_name TEXT NOT NULL DEFAULT '未知',
            updated_at TIMESTAMP,
            min_exp REAL,
            max_exp REAL,
            first_seen TIMESTAMP,
            last_seen TIMESTAMP,
            max_level INTEGER,
            max_sub_grade INTEGER,
            guild_name TEXT DEFAULT '',
            PRIMARY KEY (player_name, server_name)
        )
        """
    )
    cols = await _table_columns(db, "player_profile")
    for name, decl in _PLAYER_PROFILE_STAT_COLUMNS:
        if name not in cols:
            await db.execute(f"ALTER TABLE player_profile ADD COLUMN {name} {decl}")


def _ensure_player_profile_columns_sync(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS player_profile (
            player_name TEXT NOT NULL,
            server_name TEXT NOT NULL,
            class_name TEXT NOT NULL DEFAULT '未知',
            updated_at TIMESTAMP,
            min_exp REAL,
            max_exp REAL,
            first_seen TIMESTAMP,
            last_seen TIMESTAMP,
            max_level INTEGER,
            max_sub_grade INTEGER,
            guild_name TEXT DEFAULT '',
            PRIMARY KEY (player_name, server_name)
        )
        """
    )
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(player_profile)").fetchall()
    }
    for name, decl in _PLAYER_PROFILE_STAT_COLUMNS:
        if name not in cols:
            conn.execute(f"ALTER TABLE player_profile ADD COLUMN {name} {decl}")


def _ensure_exp_history_columns_sync(conn: sqlite3.Connection) -> None:
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(exp_history)").fetchall()
    }
    if not cols:
        return
    if "class_name" not in cols:
        conn.execute(
            "ALTER TABLE exp_history ADD COLUMN class_name TEXT DEFAULT '未知'"
        )
    if "subjugation_grade" not in cols:
        conn.execute(
            "ALTER TABLE exp_history ADD COLUMN subjugation_grade INTEGER DEFAULT 0"
        )
    if "guild_name" not in cols:
        conn.execute(
            "ALTER TABLE exp_history ADD COLUMN guild_name TEXT DEFAULT ''"
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
        if version >= 4:
            await _ensure_player_profile_columns(db)
        if version >= 7:
            await _ensure_exp_history_columns(db)
            await _ensure_player_profile_columns(db)
        await _set_schema_version(db, version)
        await db.commit()
        current = version
        logger.info(f"✅ schema 已至 v{version}")

    # 即使已是最新版，仍確保欄位齊全（手動改庫情境）
    await _ensure_exp_history_columns(db)
    await _ensure_player_profile_columns(db)
    if current < SCHEMA_VERSION:
        await _set_schema_version(db, SCHEMA_VERSION)
        current = SCHEMA_VERSION
    await db.commit()
    return current


def list_missing_search_indexes(conn: sqlite3.Connection) -> list[str]:
    """回傳尚未建立的尋人索引名稱。"""
    existing = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    return [name for name, _ in _SEARCH_INDEXES if name not in existing]


def build_search_indexes_sync(
    conn: sqlite3.Connection,
    *,
    index_names: Iterable[str] | None = None,
) -> list[str]:
    """離線建立尋人索引（無列數上限；供 cleanup_db 使用）。

    回傳本次新建的索引名稱列表。
    """
    wanted = list(_SEARCH_INDEXES)
    if index_names is not None:
        allow = set(index_names)
        wanted = [item for item in wanted if item[0] in allow]

    existing = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    created: list[str] = []
    for name, ddl in wanted:
        if name in existing:
            continue
        if name.startswith("idx_profile_"):
            _ensure_player_profile_columns_sync(conn)
        logger.info("⏳ 離線建立資料庫索引 %s...", name)
        try:
            conn.execute(ddl)
            conn.commit()
            created.append(name)
            existing.add(name)
            logger.info("✅ 索引 %s 建立完成", name)
        except sqlite3.DatabaseError as e:
            logger.warning("⚠️ 無法建立索引 %s: %s", name, e)
    return created


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
            " 請停 bot 後執行 `python cleanup_db.py --build-indexes`。"
        )
        return []

    created: list[str] = []
    for name, ddl in missing:
        if name.startswith("idx_profile_"):
            await _ensure_player_profile_columns(db)
        logger.info(f"⏳ 背景建立資料庫索引 {name}...")
        try:
            await db.execute(ddl)
            await db.commit()
            created.append(name)
            logger.info(f"✅ 索引 {name} 建立完成")
        except sqlite3.DatabaseError as e:
            logger.warning(f"⚠️ 無法建立索引 {name}: {e}")
    return created


def rebuild_player_profiles_sync(conn: sqlite3.Connection) -> int:
    """離線重建 player_profile（職業 + min/max EXP／首末見／等級／討伐）。回傳寫入列數。"""
    _ensure_exp_history_columns_sync(conn)
    _ensure_player_profile_columns_sync(conn)
    try:
        conn.execute("DELETE FROM player_profile")
        conn.execute(_PLAYER_PROFILE_REBUILD_INSERT_SQL)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    row = conn.execute("SELECT COUNT(*) FROM player_profile").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


_PLAYER_PROFILE_REBUILD_INSERT_SQL = """
        INSERT INTO player_profile (
            player_name, server_name, class_name, updated_at,
            min_exp, max_exp, first_seen, last_seen, max_level, max_sub_grade,
            guild_name
        )
        SELECT
            a.player_name,
            a.server_name,
            COALESCE(latest.class_name, '未知'),
            a.last_seen,
            a.min_exp,
            a.max_exp,
            a.first_seen,
            a.last_seen,
            a.max_level,
            a.max_sub_grade,
            COALESCE(latest.guild_name, '')
        FROM (
            SELECT player_name, server_name,
                   MIN(exp) AS min_exp,
                   MAX(exp) AS max_exp,
                   MIN(record_time) AS first_seen,
                   MAX(record_time) AS last_seen,
                   MAX(level) AS max_level,
                   MAX(subjugation_grade) AS max_sub_grade
            FROM exp_history
            GROUP BY player_name, server_name
        ) a
        LEFT JOIN (
            SELECT player_name, server_name, class_name, guild_name
            FROM (
                SELECT player_name, server_name, class_name, guild_name,
                       ROW_NUMBER() OVER (
                           PARTITION BY player_name, server_name
                           ORDER BY record_time DESC
                       ) AS rn
                FROM exp_history
            )
            WHERE rn = 1
        ) latest
          ON latest.player_name = a.player_name
         AND latest.server_name = a.server_name
        """


async def rebuild_player_profiles(db) -> int:
    """線上重建 player_profile；失敗必 rollback，避免留下空表。"""
    await _ensure_exp_history_columns(db)
    await _ensure_player_profile_columns(db)
    try:
        await db.execute("DELETE FROM player_profile")
        await db.execute(_PLAYER_PROFILE_REBUILD_INSERT_SQL)
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            logger.warning("rebuild_player_profiles rollback 失敗", exc_info=True)
        raise
    async with db.execute("SELECT COUNT(*) FROM player_profile") as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


async def prune_orphaned_player_profiles(db) -> int:
    """刪除已無 exp_history 對應的 player_profile 列；回傳刪除數。"""
    await _ensure_player_profile_columns(db)
    try:
        cursor = await db.execute(
            """
            DELETE FROM player_profile
            WHERE NOT EXISTS (
                SELECT 1 FROM exp_history e
                WHERE e.player_name = player_profile.player_name
                  AND e.server_name = player_profile.server_name
            )
            """
        )
        deleted = cursor.rowcount or 0
        await db.commit()
        return int(deleted)
    except Exception:
        try:
            await db.rollback()
        except Exception:
            logger.warning("prune_orphaned_player_profiles rollback 失敗", exc_info=True)
        raise


# 線上清理：刪除量達此門檻才考慮全量 rebuild（其餘用 orphan prune + backfill）
ONLINE_FULL_REBUILD_EXP_DELETED_THRESHOLD = 5_000
# denorm 覆蓋率低於此值則不走 denorm 快路徑
DENORM_COVERAGE_READY_RATIO = 0.85


async def denorm_coverage_stats(db) -> tuple[int, int]:
    """回傳 (total_profiles, filled_with_min_exp)。"""
    async with db.execute("SELECT COUNT(*) FROM player_profile") as cursor:
        total = int((await cursor.fetchone())[0] or 0)
    async with db.execute(
        "SELECT COUNT(*) FROM player_profile WHERE min_exp IS NOT NULL"
    ) as cursor:
        filled = int((await cursor.fetchone())[0] or 0)
    return total, filled


async def denorm_is_ready(db, *, min_ratio: float = DENORM_COVERAGE_READY_RATIO) -> bool:
    """覆蓋率足夠才允許 denorm 快路徑。"""
    total, filled = await denorm_coverage_stats(db)
    if total <= 0:
        return False
    return (filled / total) >= min_ratio

async def backfill_player_profile_denorm(db, *, batch_limit: int = 500) -> int:
    """增量回填 player_profile 缺 denorm 統計的列；回傳本批更新數。

    僅處理 min_exp IS NULL 的列，避免全表重建鎖庫。
    """
    await _ensure_player_profile_columns(db)
    async with db.execute(
        """
        SELECT player_name, server_name
        FROM player_profile
        WHERE min_exp IS NULL
        LIMIT ?
        """,
        (batch_limit,),
    ) as cursor:
        targets = await cursor.fetchall()
    if not targets:
        return 0

    updated = 0
    for player_name, server_name in targets:
        async with db.execute(
            """
            SELECT
                MIN(exp), MAX(exp),
                MIN(record_time), MAX(record_time),
                MAX(level), MAX(subjugation_grade)
            FROM exp_history
            WHERE player_name = ? AND server_name = ?
            """,
            (player_name, server_name),
        ) as cursor:
            row = await cursor.fetchone()
        if not row or row[0] is None:
            continue
        min_exp, max_exp, first_seen, last_seen, max_level, max_sub = row
        await db.execute(
            """
            UPDATE player_profile
            SET min_exp = ?, max_exp = ?,
                first_seen = ?, last_seen = ?,
                max_level = ?, max_sub_grade = ?,
                updated_at = COALESCE(?, updated_at)
            WHERE player_name = ? AND server_name = ?
            """,
            (
                min_exp,
                max_exp,
                first_seen,
                last_seen,
                max_level,
                max_sub,
                last_seen,
                player_name,
                server_name,
            ),
        )
        updated += 1
    if updated:
        await db.commit()
    return updated


async def list_missing_search_indexes_async(db) -> list[str]:
    """aiosqlite 版：回傳尚未建立的尋人索引名稱。"""
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ) as cursor:
        existing = {row[0] for row in await cursor.fetchall()}
    return [name for name, _ in _SEARCH_INDEXES if name not in existing]
