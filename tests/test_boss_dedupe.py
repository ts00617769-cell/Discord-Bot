"""boss alert_dedupe：先 claim 再送，避免重複 @everyone。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import aiosqlite
import discord
import pytest

from cogs.boss_schedule import BossSchedule
from db.schema import apply_migrations
from game_data import GAP_BOSS_SCHEDULE
from services.boss_reminder import run_boss_reminder


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
async def test_release_reminder_claim_allows_retry(tmp_path):
    db = await aiosqlite.connect(tmp_path / "release.db")
    try:
        await apply_migrations(db)
        bot = MagicMock()
        bot.db = db
        cog = BossSchedule(bot)
        key = "boss_reminder:2026-08-05:20"
        assert await cog._try_claim_reminder(key) is True
        await cog._release_reminder_claim(key)
        assert await cog._already_reminded(key) is False
        assert await cog._try_claim_reminder(key) is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_boss_reminder_claims_before_send():
    bot = MagicMock()
    bot.db = MagicMock()
    channel = MagicMock()
    order: list[str] = []

    async def claim(*_a, **_k) -> bool:
        order.append("claim")
        return True

    async def send(*_a, **_k):
        order.append("send")

    channel.send = AsyncMock(side_effect=send)

    weekday_with_boss = next(iter(GAP_BOSS_SCHEDULE.keys()))
    hour = GAP_BOSS_SCHEDULE[weekday_with_boss][0]

    with patch(
        "services.boss_reminder.now_taipei"
    ) as mock_now, patch(
        "services.boss_reminder.today_taipei_str", return_value="2026-08-05"
    ), patch(
        "services.boss_reminder.alert_already_sent",
        new_callable=AsyncMock,
        return_value=False,
    ), patch(
        "services.boss_reminder.try_claim_alert",
        new_callable=AsyncMock,
        side_effect=claim,
    ), patch(
        "services.boss_reminder.send_to_channels",
        new_callable=AsyncMock,
        side_effect=lambda *_a, **_k: (order.append("send") or {1}),
    ):
        import datetime

        from services.timeutil import TAIPEI

        base = datetime.datetime(2026, 8, 3, hour, 0, tzinfo=TAIPEI)
        delta = (weekday_with_boss - base.weekday()) % 7
        target = base + datetime.timedelta(days=delta)
        mock_now.return_value = target - datetime.timedelta(minutes=10)
        ok = await run_boss_reminder(bot, write_db=bot.db, channel_id=1)

    assert ok is True
    assert order == ["claim", "send"]


@pytest.mark.asyncio
async def test_boss_reminder_releases_claim_when_send_fails():
    bot = MagicMock()
    bot.db = MagicMock()
    weekday = next(iter(GAP_BOSS_SCHEDULE))
    hour = GAP_BOSS_SCHEDULE[weekday][0]
    release = AsyncMock()

    with patch("services.boss_reminder.now_taipei") as mock_now, patch(
        "services.boss_reminder.today_taipei_str", return_value="2026-08-05"
    ), patch(
        "services.boss_reminder.alert_already_sent",
        new_callable=AsyncMock,
        return_value=False,
    ), patch(
        "services.boss_reminder.try_claim_alert",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "services.boss_reminder.send_to_channels",
        new_callable=AsyncMock,
        return_value=set(),
    ), patch(
        "services.boss_reminder.release_alert_claim",
        release,
    ):
        import datetime

        from services.timeutil import TAIPEI

        base = datetime.datetime(2026, 8, 3, hour, 0, tzinfo=TAIPEI)
        target = base + datetime.timedelta(days=(weekday - base.weekday()) % 7)
        mock_now.return_value = target - datetime.timedelta(minutes=10)
        ok = await run_boss_reminder(bot, write_db=bot.db, channel_id=1)

    assert ok is False
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_cog_auto_boss_reminder_delegates_to_service():
    bot = MagicMock()
    cog = BossSchedule(bot)
    with patch.object(
        BossSchedule,
        "REMINDER_CHANNEL_ID",
        new_callable=PropertyMock,
        return_value=99,
    ), patch(
        "cogs.boss_schedule.run_boss_reminder",
        new_callable=AsyncMock,
        return_value=True,
    ) as runner:
        await cog.auto_boss_reminder()
    runner.assert_awaited_once()
    assert runner.await_args.kwargs["channel_id"] == 99
