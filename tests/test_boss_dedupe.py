"""boss alert_dedupe 讀寫相容。"""
from __future__ import annotations

import aiosqlite
import pytest

from db.schema import apply_migrations


@pytest.mark.asyncio
async def test_boss_dedupe_table_roundtrip(tmp_path):
    db_path = tmp_path / "boss.db"
    db = await aiosqlite.connect(str(db_path))
    try:
        await apply_migrations(db)
        key = "boss_reminder:2026-07-27:23"
        await db.execute(
            """
            INSERT INTO alert_dedupe (kind, dedupe_key, created_at)
            VALUES ('boss_reminder', ?, '2026-07-27 22:50:00')
            """,
            (key,),
        )
        await db.commit()
        async with db.execute(
            "SELECT 1 FROM alert_dedupe WHERE kind=? AND dedupe_key=?",
            ("boss_reminder", key),
        ) as cur:
            assert await cur.fetchone() is not None
        # 舊 bot_settings 相容路徑仍可存在
        await db.execute(
            "INSERT INTO bot_settings (key, value) VALUES (?, '1')",
            (key,),
        )
        await db.commit()
        async with db.execute(
            "SELECT 1 FROM bot_settings WHERE key=?", (key,)
        ) as cur:
            assert await cur.fetchone() is not None
    finally:
        await db.close()
