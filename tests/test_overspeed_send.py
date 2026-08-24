"""超速警報：送達成功才寫 dedupe；claim-first。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import discord
import pytest

from cogs.exp_tracker import ExpTracker
from db.schema import apply_migrations
from services.alert_dedupe import KIND_OVERSPEED, alert_already_sent
from services.discord_send import send_embeds_to_channels
from services.overspeed_alerts import build_overspeed_embeds, run_overspeed_patrol


def _tracker_for_embeds() -> ExpTracker:
    cog = ExpTracker.__new__(ExpTracker)
    cog.alert_server = "萊涅01"
    cog.alert_guild = "狼團"
    cog.alert_count = 30
    cog.SPEED_LIMIT = 2000
    return cog


def test_alert_output_and_speed_windows_are_ten_minutes(monkeypatch):
    monkeypatch.delenv("EXP_ALERT_THRESHOLD", raising=False)
    tracker = ExpTracker(MagicMock())
    assert tracker.alert_interval_minutes == 10
    assert tracker.alert_speed_window_minutes == 10
    assert tracker.SPEED_LIMIT == 2000


def test_build_overspeed_embeds_reports_clear_round():
    embeds = build_overspeed_embeds(
        _tracker_for_embeds(),
        [],
        record_count=12,
        time_now="2026-08-04 12:10:00",
        minutes_diff=10,
        include_clear=True,
    )
    assert len(embeds) == 1
    assert "10 分鐘超速巡檢" in embeds[0].title
    assert "監控週期: 10min" in embeds[0].footer.text
    assert "12" in embeds[0].description
    assert "沒有人超過" in embeds[0].description


def test_build_overspeed_embeds_skips_clear_by_default(monkeypatch):
    monkeypatch.delenv("EXP_ALERT_SEND_CLEAR", raising=False)
    embeds = build_overspeed_embeds(
        _tracker_for_embeds(),
        [],
        record_count=12,
        time_now="2026-08-04 12:10:00",
        minutes_diff=10,
    )
    assert embeds == []


def test_build_overspeed_embeds_warns_when_guild_has_no_records():
    embeds = build_overspeed_embeds(
        _tracker_for_embeds(),
        [],
        record_count=0,
        time_now="2026-08-04 12:10:00",
        minutes_diff=10,
    )
    assert "找不到" in embeds[0].description
    assert "狼團" in embeds[0].description


def test_build_overspeed_embeds_uses_new_threshold_and_window():
    embeds = build_overspeed_embeds(
        _tracker_for_embeds(),
        [{"name": "玩家", "server": "萊涅01", "level": 90, "speed": 2500}],
        record_count=1,
        time_now="2026-08-04 12:10:00",
        minutes_diff=10,
    )
    assert "≥2,000億" in embeds[0].title
    assert "監控週期: 10min" in embeds[0].footer.text


@pytest.mark.asyncio
async def test_send_overspeed_embeds_counts_success():
    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel
    bot.fetch_channel = AsyncMock()

    sent = await send_embeds_to_channels(
        bot, [111], [discord.Embed(title="t")], label="overspeed alert channel"
    )
    assert sent == {111}
    channel.send.assert_awaited_once()
    bot.fetch_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_overspeed_embeds_fetch_fallback():
    bot = MagicMock()
    bot.get_channel.return_value = None
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.fetch_channel = AsyncMock(return_value=channel)

    sent = await send_embeds_to_channels(
        bot, [222], [discord.Embed(title="t")], label="overspeed alert channel"
    )
    assert sent == {222}
    bot.fetch_channel.assert_awaited_once_with(222)


@pytest.mark.asyncio
async def test_send_overspeed_embeds_failure_returns_zero():
    bot = MagicMock()
    channel = MagicMock()
    response = MagicMock()
    response.status = 500
    channel.send = AsyncMock(side_effect=discord.HTTPException(response, "fail"))
    bot.get_channel.return_value = channel

    sent = await send_embeds_to_channels(
        bot, [333], [discord.Embed(title="t")], label="overspeed alert channel"
    )
    assert sent == set()


@pytest.mark.asyncio
async def test_send_overspeed_embeds_tracks_success_per_channel():
    bot = MagicMock()
    good = MagicMock()
    good.send = AsyncMock()
    bad = MagicMock()
    response = MagicMock(status=500)
    bad.send = AsyncMock(side_effect=discord.HTTPException(response, "fail"))
    bot.get_channel.side_effect = lambda channel_id: {1: good, 2: bad}[channel_id]

    sent = await send_embeds_to_channels(
        bot, [1, 2], [discord.Embed(title="t")], label="overspeed alert channel"
    )

    assert sent == {1}
    good.send.assert_awaited_once()
    assert bad.send.await_count == 4  # 1 try + 3 retries on 5xx


async def _seed_guild_snapshot(db, t_now: str, t_prev: str) -> None:
    """讓 run_overspeed_patrol 取得到比對資料（records 非空）。"""
    await db.executemany(
        """
        INSERT INTO exp_history
        (record_time, player_name, server_name, level, exp, class_name,
         subjugation_grade, guild_name)
        VALUES (?, ?, ?, ?, ?, '戰士', 1, ?)
        """,
        [
            (t_now, "Alice", "萊涅01", 90, 2e12, "狼團"),
            (t_prev, "Alice", "萊涅01", 90, 1e12, "狼團"),
        ],
    )
    await db.commit()


@pytest.mark.asyncio
async def test_overspeed_claim_before_send_and_release_on_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("EXP_ALERT_SEND_CLEAR", "1")
    db = await aiosqlite.connect(str(tmp_path / "os.db"))
    try:
        await apply_migrations(db)
        await _seed_guild_snapshot(db, "2026-08-05 12:10:00", "2026-08-05 12:00:00")
        bot = MagicMock()
        settings = SimpleNamespace(
            alerts_enabled=True,
            alert_server="萊涅01",
            alert_guild="狼團",
            alert_count=30,
            alert_interval_minutes=10,
            alert_speed_window_minutes=10,
            SPEED_LIMIT=2000.0,
            ALERT_CHANNEL_IDS=[42],
        )
        times = [("2026-08-05 12:10:00",), ("2026-08-05 12:00:00",)]
        import datetime

        current = datetime.datetime(2026, 8, 5, 12, 10, 0)

        with patch(
            "services.overspeed_alerts.pick_interval_baseline",
            return_value=("2026-08-05 12:00:00", 10.0),
        ), patch(
            "services.overspeed_alerts.collect_overspeed", return_value=[]
        ), patch(
            "services.overspeed_alerts.send_embeds_to_channels",
            new_callable=AsyncMock,
            return_value=set(),
        ):
            await run_overspeed_patrol(
                bot,
                settings,
                read_db=db,
                write_db=db,
                times=times,
                current_time=current,
            )

        key = (
            "overspeed:2026-08-05 12:10:00|2026-08-05 12:00:00|"
            "萊涅01|狼團|30|channel:42"
        )
        assert await alert_already_sent(db, KIND_OVERSPEED, key) is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_overspeed_claim_persists_when_send_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("EXP_ALERT_SEND_CLEAR", "1")
    db = await aiosqlite.connect(str(tmp_path / "os_ok.db"))
    try:
        await apply_migrations(db)
        await _seed_guild_snapshot(db, "2026-08-05 12:10:00", "2026-08-05 12:00:00")
        bot = MagicMock()
        settings = SimpleNamespace(
            alerts_enabled=True,
            alert_server="萊涅01",
            alert_guild="狼團",
            alert_count=30,
            alert_interval_minutes=10,
            alert_speed_window_minutes=10,
            SPEED_LIMIT=2000.0,
            ALERT_CHANNEL_IDS=[42],
        )
        times = [("2026-08-05 12:10:00",), ("2026-08-05 12:00:00",)]
        import datetime

        current = datetime.datetime(2026, 8, 5, 12, 10, 0)

        with patch(
            "services.overspeed_alerts.pick_interval_baseline",
            return_value=("2026-08-05 12:00:00", 10.0),
        ), patch(
            "services.overspeed_alerts.collect_overspeed", return_value=[]
        ), patch(
            "services.overspeed_alerts.send_embeds_to_channels",
            new_callable=AsyncMock,
            return_value={42},
        ):
            await run_overspeed_patrol(
                bot,
                settings,
                read_db=db,
                write_db=db,
                times=times,
                current_time=current,
            )

        key = (
            "overspeed:2026-08-05 12:10:00|2026-08-05 12:00:00|"
            "萊涅01|狼團|30|channel:42"
        )
        assert await alert_already_sent(db, KIND_OVERSPEED, key) is True
    finally:
        await db.close()
