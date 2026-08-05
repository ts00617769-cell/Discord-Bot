"""多頻道 Discord 送出輔助。"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import discord

from services.error_handler import resolve_bot_channel

logger = logging.getLogger(__name__)


async def send_to_channels(
    bot,
    channel_ids: Sequence[int],
    *,
    send_fn: Callable[[Any], Awaitable[None]],
    label: str = "alert channel",
) -> set[int]:
    """對每個頻道執行 send_fn；成功者加入回傳集合。"""
    successful: set[int] = set()
    for channel_id in channel_ids:
        channel = await resolve_bot_channel(bot, channel_id, label=label)
        if channel is None:
            continue
        try:
            await send_fn(channel)
        except discord.HTTPException as e:
            logger.error("Failed to send to %s %s: %s", label, channel_id, e)
        else:
            successful.add(channel_id)
    return successful


async def send_embeds_to_channels(
    bot,
    channel_ids: Sequence[int],
    embeds: Sequence[discord.Embed],
    *,
    label: str = "alert channel",
) -> set[int]:
    """每個頻道完整送出全部 embeds 才列為成功。"""

    async def _send(channel) -> None:
        for embed in embeds:
            await channel.send(embed=embed)

    return await send_to_channels(bot, channel_ids, send_fn=_send, label=label)
