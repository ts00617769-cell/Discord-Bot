"""版本化 schema 遷移。"""
from __future__ import annotations

import pytest

from db.schema import SCHEMA_VERSION, apply_migrations, ensure_search_indexes


@pytest.mark.asyncio
async def test_apply_migrations_fresh_db(tmp_path):
    import aiosqlite

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

        created = await ensure_search_indexes(db, skip_if_rows_above=1_000_000)
        assert "idx_exp" in created or created == []
        # 再跑一次應無新建
        created2 = await ensure_search_indexes(db, skip_if_rows_above=1_000_000)
        assert created2 == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_legacy_column_backfill(tmp_path):
    import aiosqlite

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
