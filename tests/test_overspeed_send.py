"""超速警報：送達成功才寫 dedupe。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import discord
import pytest

from cogs.exp_tracker import ExpTracker


def _tracker_for_embeds() -> ExpTracker:
    cog = ExpTracker.__new__(ExpTracker)
    cog.alert_server = "萊涅01"
    cog.alert_guild = "狼團"
    cog.alert_count = 30
    cog.SPEED_LIMIT = 4000
    return cog


def test_alert_output_and_speed_windows_are_independent():
    tracker = ExpTracker(MagicMock())
    assert tracker.alert_interval_minutes == 10
    assert tracker.alert_speed_window_minutes == 30


def test_build_overspeed_embeds_reports_clear_round():
    embeds = _tracker_for_embeds()._build_overspeed_embeds(
        [],
        record_count=12,
        time_now="2026-08-04 12:10:00",
        minutes_diff=30,
    )
    assert len(embeds) == 1
    assert "10 分鐘超速巡檢" in embeds[0].title
    assert "監控週期: 30min" in embeds[0].footer.text
    assert "12" in embeds[0].description
    assert "沒有人超過" in embeds[0].description


def test_build_overspeed_embeds_warns_when_guild_has_no_records():
    embeds = _tracker_for_embeds()._build_overspeed_embeds(
        [],
        record_count=0,
        time_now="2026-08-04 12:10:00",
        minutes_diff=30,
    )
    assert "找不到" in embeds[0].description
    assert "狼團" in embeds[0].description


@pytest.mark.asyncio
async def test_send_overspeed_embeds_counts_success():
    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel
    bot.fetch_channel = AsyncMock()

    cog = ExpTracker.__new__(ExpTracker)
    cog.bot = bot
    with patch.object(
        ExpTracker, "ALERT_CHANNEL_IDS", new_callable=PropertyMock, return_value=[111]
    ):
        sent = await cog._send_overspeed_embeds([discord.Embed(title="t")])
    assert sent == 1
    channel.send.assert_awaited_once()
    bot.fetch_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_overspeed_embeds_fetch_fallback():
    bot = MagicMock()
    bot.get_channel.return_value = None
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.fetch_channel = AsyncMock(return_value=channel)

    cog = ExpTracker.__new__(ExpTracker)
    cog.bot = bot
    with patch.object(
        ExpTracker, "ALERT_CHANNEL_IDS", new_callable=PropertyMock, return_value=[222]
    ):
        sent = await cog._send_overspeed_embeds([discord.Embed(title="t")])
    assert sent == 1
    bot.fetch_channel.assert_awaited_once_with(222)


@pytest.mark.asyncio
async def test_send_overspeed_embeds_failure_returns_zero():
    bot = MagicMock()
    channel = MagicMock()
    response = MagicMock()
    response.status = 500
    channel.send = AsyncMock(side_effect=discord.HTTPException(response, "fail"))
    bot.get_channel.return_value = channel

    cog = ExpTracker.__new__(ExpTracker)
    cog.bot = bot
    with patch.object(
        ExpTracker, "ALERT_CHANNEL_IDS", new_callable=PropertyMock, return_value=[333]
    ):
        sent = await cog._send_overspeed_embeds([discord.Embed(title="t")])
    assert sent == 0
