"""Discord 頻道 cache miss 時使用 REST fallback。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from services.error_handler import resolve_bot_channel


@pytest.mark.asyncio
async def test_resolve_bot_channel_uses_cache():
    bot = MagicMock()
    channel = object()
    bot.get_channel.return_value = channel
    bot.fetch_channel = AsyncMock()
    assert await resolve_bot_channel(bot, 123) is channel
    bot.fetch_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_bot_channel_fetches_cache_miss():
    bot = MagicMock()
    channel = object()
    bot.get_channel.return_value = None
    bot.fetch_channel = AsyncMock(return_value=channel)
    assert await resolve_bot_channel(bot, 123) is channel
    bot.fetch_channel.assert_awaited_once_with(123)


@pytest.mark.asyncio
async def test_resolve_bot_channel_returns_none_on_http_failure():
    bot = MagicMock()
    bot.get_channel.return_value = None
    response = MagicMock(status=404, reason="Not Found")
    bot.fetch_channel = AsyncMock(
        side_effect=discord.HTTPException(response, "missing")
    )
    assert await resolve_bot_channel(bot, 123) is None
