"""boss alert_dedupe：先 claim 再送，避免重複 @everyone。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import aiosqlite
import pytest

from cogs.boss_schedule import BossSchedule
from db.schema import apply_migrations
from game_data import GAP_BOSS_SCHEDULE


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


@pytest.mark.asyncio
async def test_try_claim_reminder_is_exclusive(tmp_path):
    db = await aiosqlite.connect(tmp_path / "claim.db")
    try:
        await apply_migrations(db)
        bot = MagicMock()
        bot.db = db
        cog = BossSchedule(bot)
        key = "boss_reminder:2026-08-05:20"
        assert await cog._try_claim_reminder(key) is True
        assert await cog._try_claim_reminder(key) is False
        assert await cog._already_reminded(key) is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_boss_reminder_claims_before_send():
    bot = MagicMock()
    bot.db = MagicMock()
    channel = MagicMock()
    order: list[str] = []

    async def claim(key: str) -> bool:
        order.append("claim")
        return True

    async def send(*_a, **_k):
        order.append("send")

    channel.send = AsyncMock(side_effect=send)
    cog = BossSchedule(bot)

    weekday_with_boss = next(iter(GAP_BOSS_SCHEDULE.keys()))
    hour = GAP_BOSS_SCHEDULE[weekday_with_boss][0]

    with patch.object(
        BossSchedule,
        "REMINDER_CHANNEL_ID",
        new_callable=PropertyMock,
        return_value=1,
    ), patch(
        "cogs.boss_schedule.now_taipei"
    ) as mock_now, patch(
        "cogs.boss_schedule.resolve_bot_channel",
        new_callable=AsyncMock,
        return_value=channel,
    ), patch.object(cog, "_try_claim_reminder", side_effect=claim), patch.object(
        cog, "_already_reminded", new_callable=AsyncMock, return_value=False
    ), patch(
        "cogs.boss_schedule.today_taipei_str", return_value="2026-08-05"
    ):
        import datetime

        from services.timeutil import TAIPEI

        base = datetime.datetime(2026, 8, 3, hour, 0, tzinfo=TAIPEI)
        delta = (weekday_with_boss - base.weekday()) % 7
        target = base + datetime.timedelta(days=delta)
        mock_now.return_value = target - datetime.timedelta(minutes=10)
        await cog.auto_boss_reminder()

    assert order == ["claim", "send"]
    assert channel.send.await_args.kwargs.get("content") == "@everyone"
