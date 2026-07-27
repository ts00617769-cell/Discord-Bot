"""alert_dedupe 寫入／相容舊 bot_settings。"""
from __future__ import annotations

import pytest

from db.schema import apply_migrations
from services.alert_dedupe import mark_overspeed_sent, overspeed_already_sent


@pytest.mark.asyncio
async def test_overspeed_dedupe_write_and_read(tmp_path):
    import aiosqlite

    db = await aiosqlite.connect(str(tmp_path / "dedupe.db"))
    try:
        await apply_migrations(db)
        key = "overspeed:2026-07-27 12:00:00|2026-07-27 11:30:00|全服|30"
        assert await overspeed_already_sent(db, key) is False
        await mark_overspeed_sent(db, key, created_at="2026-07-27 12:00:00")
        assert await overspeed_already_sent(db, key) is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_overspeed_dedupe_falls_back_to_bot_settings(tmp_path):
    import aiosqlite

    db = await aiosqlite.connect(str(tmp_path / "dedupe_legacy.db"))
    try:
        await apply_migrations(db)
        key = "overspeed:legacy-key"
        await db.execute(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?)",
            (key, "1"),
        )
        await db.commit()
        assert await overspeed_already_sent(db, key) is True
    finally:
        await db.close()
