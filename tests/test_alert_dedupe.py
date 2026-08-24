"""alert_dedupe 寫入／讀取。"""
from __future__ import annotations

import pytest

from db.schema import apply_migrations
from services.alert_dedupe import KIND_OVERSPEED, alert_already_sent, try_claim_alert


@pytest.mark.asyncio
async def test_overspeed_dedupe_write_and_read(tmp_path):
    import aiosqlite

    db = await aiosqlite.connect(str(tmp_path / "dedupe.db"))
    try:
        await apply_migrations(db)
        key = "overspeed:2026-07-27 12:00:00|2026-07-27 11:30:00|全服|30"
        assert await alert_already_sent(db, KIND_OVERSPEED, key) is False
        assert await try_claim_alert(
            db, KIND_OVERSPEED, key, created_at="2026-07-27 12:00:00"
        )
        assert await alert_already_sent(db, KIND_OVERSPEED, key) is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_overspeed_dedupe_ignores_bot_settings_keys(tmp_path):
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
        assert await alert_already_sent(db, KIND_OVERSPEED, key) is False
    finally:
        await db.close()
