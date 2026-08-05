"""多頻道 Discord 送出輔助（暫態錯誤重試 + embed 批次化）。"""
from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import aiohttp
import discord

from services.error_handler import resolve_bot_channel

logger = logging.getLogger(__name__)

_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BASE_DELAY = 0.5

# Discord 單則訊息上限：10 個 embed、總長 6000 字元（留安全邊際）
MAX_EMBEDS_PER_MESSAGE = 10
MAX_EMBED_CHARS_PER_MESSAGE = 5500

_RETRYABLE_NETWORK_ERRORS = (
    aiohttp.ClientError,
    asyncio.TimeoutError,
    ConnectionError,
    OSError,
)


def _is_retryable(exc: BaseException) -> bool:
    """429／5xx 與網路層暫態錯誤可重試；4xx 用戶端錯誤不重試。"""
    if isinstance(exc, discord.HTTPException):
        status = getattr(exc, "status", None)
        if status is None:
            status = getattr(getattr(exc, "response", None), "status", None)
        if status == 429:
            return True
        return isinstance(status, int) and status >= 500
    return isinstance(exc, _RETRYABLE_NETWORK_ERRORS)


async def _run_with_retry(
    action: Callable[[], Awaitable[None]],
    *,
    label: str,
    channel_id: int,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    base_delay: float = _DEFAULT_BASE_DELAY,
) -> bool:
    """執行 action；對暫態錯誤指數退避重試。成功回 True。"""
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            await action()
            return True
        except (discord.HTTPException, *_RETRYABLE_NETWORK_ERRORS) as e:
            last_exc = e
            if attempt >= max_retries or not _is_retryable(e):
                break
            delay = base_delay * (2**attempt)
            retry_after = getattr(e, "retry_after", None)
            if retry_after is not None:
                try:
                    delay = max(delay, float(retry_after))
                except (TypeError, ValueError):
                    pass
            logger.warning(
                "Retrying send to %s %s after %.1fs (attempt %s/%s): %s",
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


def chunk_embeds(
    embeds: Sequence[discord.Embed],
    *,
    max_count: int = MAX_EMBEDS_PER_MESSAGE,
    max_chars: int = MAX_EMBED_CHARS_PER_MESSAGE,
) -> list[list[discord.Embed]]:
    """把 embeds 打包成符合 Discord 單則訊息上限的批次。

    盡量塞滿同一則訊息，讓多數情況只送一次 → 不會有「送一半」的中間狀態。
    """
    batches: list[list[discord.Embed]] = []
    current: list[discord.Embed] = []
    current_len = 0
    for embed in embeds:
        try:
            size = len(embed)
        except TypeError:
            size = 0
        if current and (
            len(current) >= max_count or (current_len + size) > max_chars
        ):
            batches.append(current)
            current = []
            current_len = 0
        current.append(embed)
        current_len += size
    if current:
        batches.append(current)
    return batches


async def send_to_channel(
    channel,
    *,
    send_fn: Callable[[Any], Awaitable[None]],
    label: str = "channel",
    max_retries: int = _DEFAULT_MAX_RETRIES,
) -> bool:
    """對已解析的頻道物件送出（呼叫端自行 resolve 時使用）。"""
    return await _run_with_retry(
        functools.partial(send_fn, channel),
        label=label,
        channel_id=getattr(channel, "id", 0),
        max_retries=max_retries,
    )


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
        ok = await _run_with_retry(
            functools.partial(send_fn, channel),
            label=label,
            channel_id=channel_id,
            max_retries=max_retries,
        )
        if ok:
            successful.add(channel_id)
    return successful


async def send_text_to_channels(
    bot,
    channel_ids: Sequence[int],
    content: str,
    *,
    label: str = "log channel",
    max_retries: int = _DEFAULT_MAX_RETRIES,
) -> set[int]:
    """純文字訊息（戰情室日誌／廣播用）。"""

    async def _send(channel) -> None:
        await channel.send(content)

    return await send_to_channels(
        bot, channel_ids, send_fn=_send, label=label, max_retries=max_retries
    )


async def _send_embed_batch(channel, batch: Sequence[discord.Embed]) -> None:
    await channel.send(embeds=list(batch))


async def send_embeds_to_channels(
    bot,
    channel_ids: Sequence[int],
    embeds: Sequence[discord.Embed],
    *,
    label: str = "alert channel",
    max_retries: int = _DEFAULT_MAX_RETRIES,
) -> set[int]:
    """批次送出 embeds；回傳「不應再重送」的頻道集合。

    embeds 會先合併成最少的訊息數。若首批就失敗，該頻道視為完全未送達
    （呼叫端可安全釋放 claim 重試）；若前幾批已送達、後續批次失敗，
    仍視為已送達並記錄 CRITICAL，避免重試時把前面的內容再貼一次。
    """
    if not embeds:
        return set()

    batches = chunk_embeds(embeds)
    delivered: set[int] = set()
    for channel_id in channel_ids:
        channel = await resolve_bot_channel(bot, channel_id, label=label)
        if channel is None:
            continue
        sent_batches = 0
        for batch in batches:
            ok = await _run_with_retry(
                functools.partial(_send_embed_batch, channel, batch),
                label=label,
                channel_id=channel_id,
                max_retries=max_retries,
            )
            if not ok:
                break
            sent_batches += 1

        if sent_batches == len(batches):
            delivered.add(channel_id)
        elif sent_batches > 0:
            delivered.add(channel_id)
            logger.critical(
                "%s %s 僅送出 %s/%s 批 embed；不重送以免重複，"
                "缺漏內容已遺失",
                label,
                channel_id,
                sent_batches,
                len(batches),
            )
    return delivered
