"""多頻道 Discord 送出輔助（含暫態錯誤重試）。"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import discord

from services.error_handler import resolve_bot_channel

logger = logging.getLogger(__name__)

_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BASE_DELAY = 0.5


def _is_retryable_http(exc: discord.HTTPException) -> bool:
    status = getattr(exc, "status", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status", None)
    if status == 429:
        return True
    if isinstance(status, int) and status >= 500:
        return True
    return False


async def _send_with_retry(
    send_fn: Callable[[Any], Awaitable[None]],
    channel,
    *,
    label: str,
    channel_id: int,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    base_delay: float = _DEFAULT_BASE_DELAY,
) -> bool:
    """執行 send_fn；對 429／5xx 做指數退避重試。成功回 True。"""
    last_exc: discord.HTTPException | None = None
    for attempt in range(max_retries + 1):
        try:
            await send_fn(channel)
            return True
        except discord.HTTPException as e:
            last_exc = e
            if attempt >= max_retries or not _is_retryable_http(e):
                break
            delay = base_delay * (2**attempt)
            retry_after = getattr(e, "retry_after", None)
            if retry_after is not None:
                try:
                    delay = max(delay, float(retry_after))
                except (TypeError, ValueError):
                    pass
            logger.warning(
                "Retrying send to %s %s after %s (attempt %s/%s): %s",
                label,
                channel_id,
                delay,
                attempt + 1,
                max_retries,
                e,
            )
            await asyncio.sleep(delay)
    if last_exc is not None:
        logger.error("Failed to send to %s %s: %s", label, channel_id, last_exc)
    return False


async def send_to_channels(
    bot,
    channel_ids: Sequence[int],
    *,
    send_fn: Callable[[Any], Awaitable[None]],
    label: str = "alert channel",
    max_retries: int = _DEFAULT_MAX_RETRIES,
) -> set[int]:
    """對每個頻道執行 send_fn；成功者加入回傳集合。"""
    successful: set[int] = set()
    for channel_id in channel_ids:
        channel = await resolve_bot_channel(bot, channel_id, label=label)
        if channel is None:
            continue
        ok = await _send_with_retry(
            send_fn,
            channel,
            label=label,
            channel_id=channel_id,
            max_retries=max_retries,
        )
        if ok:
            successful.add(channel_id)
    return successful


async def send_embeds_to_channels(
    bot,
    channel_ids: Sequence[int],
    embeds: Sequence[discord.Embed],
    *,
    label: str = "alert channel",
    max_retries: int = _DEFAULT_MAX_RETRIES,
) -> set[int]:
    """每個頻道完整送出全部 embeds 才列為成功。"""

    async def _send(channel) -> None:
        for embed in embeds:
            await channel.send(embed=embed)

    return await send_to_channels(
        bot,
        channel_ids,
        send_fn=_send,
        label=label,
        max_retries=max_retries,
    )
