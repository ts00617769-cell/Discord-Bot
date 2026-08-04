"""超速警報：送達成功才寫 dedupe。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import discord
import pytest

from cogs.exp_tracker import ExpTracker


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
