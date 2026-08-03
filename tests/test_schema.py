"""版本化 schema 遷移。"""
from __future__ import annotations

import sqlite3

import aiosqlite
import pytest

from db.schema import (
    SCHEMA_VERSION,
    apply_migrations,
    build_search_indexes_sync,
    ensure_search_indexes,
    list_missing_search_indexes,
    rebuild_player_profiles,
    rebuild_player_profiles_sync,
)


@pytest.mark.asyncio
async def test_apply_migrations_fresh_db(tmp_path):
    db_path = tmp_path / "t.db"
    db = await aiosqlite.connect(str(db_path))
    try:
        ver = await apply_migrations(db)
        assert ver == SCHEMA_VERSION
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ) as cur:
            tables = {row[0] for row in await cur.fetchall()}
        assert "exp_history" in tables
        assert "bot_settings" in tables
        assert "cmd_dedupe" in tables
        assert "schema_meta" in tables
        assert "quiz_history" in tables
        assert "horoscope_cache" in tables
        assert "active_quiz_status" in tables
        assert "quiz_votes" in tables

        # 冪等
        ver2 = await apply_migrations(db)
        assert ver2 == SCHEMA_VERSION

        assert "player_profile" in tables
        assert "alert_dedupe" in tables
        assert "transfer_missing" in tables
        assert "member_registry" not in tables

        async with db.execute("PRAGMA table_info(player_profile)") as cur:
            pp_cols = {row[1] for row in await cur.fetchall()}
        assert "min_exp" in pp_cols

        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_class_exp_time'"
        ) as cur:
            assert await cur.fetchone() is None
        assert "max_exp" in pp_cols
        assert "first_seen" in pp_cols
        assert "guild_name" in pp_cols

        async with db.execute("PRAGMA table_info(exp_history)") as cur:
            eh_cols = {row[1] for row in await cur.fetchall()}
        assert "guild_name" in eh_cols

        created = await ensure_search_indexes(db, skip_if_rows_above=1_000_000)
        assert "idx_exp" in created or created == []
        # 再跑一次應無新建
        created2 = await ensure_search_indexes(db, skip_if_rows_above=1_000_000)
        assert created2 == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_rebuild_player_profiles_fills_stats(tmp_path):
    db_path = tmp_path / "prof.db"
    db = await aiosqlite.connect(str(db_path))
    try:
        await apply_migrations(db)
        await db.execute(
            """
            INSERT INTO exp_history
            (record_time, server_name, player_name, level, exp, class_name, subjugation_grade)
            VALUES
            ('2026-07-01 10:00:00', 'S1', 'A', 10, 100.0, '戰士', 1),
            ('2026-07-02 10:00:00', 'S1', 'A', 11, 200.0, '法師', 2)
            """
        )
        await db.commit()
    finally:
        await db.close()

    conn = sqlite3.connect(str(db_path))
    try:
        n = rebuild_player_profiles_sync(conn)
        assert n == 1
        row = conn.execute(
            "SELECT class_name, min_exp, max_exp, max_level, max_sub_grade FROM player_profile"
        ).fetchone()
        assert row == ("法師", 100.0, 200.0, 11, 2)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_rebuild_player_profiles_async(tmp_path):
    db_path = tmp_path / "async_prof.db"
    db = await aiosqlite.connect(str(db_path))
    try:
        await apply_migrations(db)
        await db.execute(
            """
            INSERT INTO exp_history
            (record_time, server_name, player_name, level, exp, class_name, subjugation_grade)
            VALUES
            ('2026-07-01 10:00:00', 'S1', 'A', 10, 100.0, '戰士', 1),
            ('2026-07-02 10:00:00', 'S1', 'A', 11, 200.0, '法師', 2)
            """
        )
        await db.commit()
        n = await rebuild_player_profiles(db)
        assert n == 1
        async with db.execute(
            "SELECT class_name, min_exp, max_exp FROM player_profile"
        ) as cur:
            row = await cur.fetchone()
        assert row == ("法師", 100.0, 200.0)
    finally:
        await db.close()


def test_build_search_indexes_sync(tmp_path):
    db_path = tmp_path / "sync.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE exp_history (
                record_time TIMESTAMP,
                server_name TEXT,
                player_name TEXT,
                level INTEGER,
                exp REAL,
                class_name TEXT,
                subjugation_grade INTEGER
            )
            """
        )
        conn.commit()
        missing = list_missing_search_indexes(conn)
        assert "idx_exp" in missing
        assert "idx_player_server" in missing
        created = build_search_indexes_sync(conn)
        assert "idx_exp" in created
        assert list_missing_search_indexes(conn) == []
        assert build_search_indexes_sync(conn) == []
    finally:
        conn.close()

@pytest.mark.asyncio
async def test_legacy_column_backfill(tmp_path):
    db_path = tmp_path / "legacy.db"
    db = await aiosqlite.connect(str(db_path))
    try:
        await db.execute(
            """
            CREATE TABLE exp_history (
                record_time TIMESTAMP,
                server_name TEXT,
                player_name TEXT,
                level INTEGER,
                exp REAL
            )
            """
        )
        await db.commit()
        await apply_migrations(db)
        async with db.execute("PRAGMA table_info(exp_history)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        assert "class_name" in cols
        assert "subjugation_grade" in cols
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v8_drops_unused_and_migrates_legacy_dedupe(tmp_path):
    db_path = tmp_path / "v8.db"
    db = await aiosqlite.connect(str(db_path))
    try:
        # 先套用到 v7，再塞舊資料，最後跑完整 migration 觸發 v8
        await db.execute(
            """
            CREATE TABLE schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        await db.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('version', '7')"
        )
        await db.execute(
            """
            CREATE TABLE exp_history (
                record_time TIMESTAMP,
                server_name TEXT,
                player_name TEXT,
                level INTEGER,
                exp REAL,
                class_name TEXT DEFAULT '未知'
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE member_registry (
                player_name TEXT PRIMARY KEY,
                original_identity TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE alert_dedupe (
                kind TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                PRIMARY KEY (kind, dedupe_key)
            )
            """
        )
        await db.execute(
            "CREATE INDEX idx_class_exp_time ON exp_history(class_name, exp, record_time)"
        )
        await db.execute(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?)",
            ("overspeed:legacy-a", "1"),
        )
        await db.execute(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?)",
            ("boss_reminder:2026-01-01:23", "1"),
        )
        await db.execute(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?)",
            ("alert_enabled", "1"),
        )
        await db.commit()

        ver = await apply_migrations(db)
        assert ver == SCHEMA_VERSION

        async with db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='member_registry'"
        ) as cur:
            assert await cur.fetchone() is None
        async with db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_class_exp_time'"
        ) as cur:
            assert await cur.fetchone() is None

        async with db.execute(
            "SELECT kind, dedupe_key FROM alert_dedupe ORDER BY kind, dedupe_key"
        ) as cur:
            rows = await cur.fetchall()
        assert ("boss_reminder", "boss_reminder:2026-01-01:23") in rows
        assert ("overspeed", "overspeed:legacy-a") in rows

        async with db.execute(
            "SELECT key FROM bot_settings ORDER BY key"
        ) as cur:
            keys = [r[0] for r in await cur.fetchall()]
        assert keys == ["alert_enabled"]
    finally:
        await db.close()
