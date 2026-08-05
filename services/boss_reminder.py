"""時空縫隙 Boss 提醒：claim-first + discord_send。"""
from __future__ import annotations

import datetime
import logging
import sqlite3
from typing import Any

import discord

from game_data import GAP_BOSS_SCHEDULE
from services.alert_dedupe import (
    KIND_BOSS_REMINDER,
    alert_already_sent,
    release_alert_claim,
    try_claim_alert,
)
from services.discord_send import send_to_channels
from services.timeutil import now_naive_taipei, now_taipei, today_taipei_str

logger = logging.getLogger(__name__)


def build_boss_reminder_embed(weekday: int) -> discord.Embed:
    time_str = "點、".join(map(str, GAP_BOSS_SCHEDULE[weekday])) + "點"
    return discord.Embed(
        title="🕒 時空縫隙首領召喚提醒",
        description=(
            f"**10 分鐘後** 將開始召喚首領！\n\n"
            f"今天召喚時段\n✅ **{time_str}**"
        ),
        color=discord.Color.red(),
    )


async def run_boss_reminder(
    bot,
    *,
    write_db: Any,
    channel_id: int,
    write_lock: Any | None = None,
) -> bool:
    """若處於提醒窗則 claim 並送出；成功回 True。"""
    if not channel_id:
        return False

    now = now_taipei()
    ten_mins_later = now + datetime.timedelta(minutes=10)
    target_hour = ten_mins_later.hour
    target_minute = ten_mins_later.minute
    weekday = ten_mins_later.weekday()

    if target_minute != 0 or target_hour not in GAP_BOSS_SCHEDULE.get(weekday, []):
        return False

    dedupe_key = f"boss_reminder:{today_taipei_str()}:{target_hour:02d}"
    created_at = now_naive_taipei().strftime("%Y-%m-%d %H:%M:%S")

    async def _claim() -> bool:
        if await alert_already_sent(write_db, KIND_BOSS_REMINDER, dedupe_key):
            return False
        return await try_claim_alert(
            write_db, KIND_BOSS_REMINDER, dedupe_key, created_at=created_at
        )

    try:
        if write_lock is not None:
            async with write_lock:
                claimed = await _claim()
        else:
            claimed = await _claim()
    except sqlite3.DatabaseError as e:
        logger.error(f"Boss reminder dedupe check failed: {e}")
        return False
    if not claimed:
        return False

    embed = build_boss_reminder_embed(weekday)

    async def _send(channel) -> None:
        await channel.send(content="@everyone", embed=embed)

    sent = await send_to_channels(
        bot,
        [channel_id],
        send_fn=_send,
        label="boss reminder channel",
    )
    if channel_id in sent:
        return True

    logger.warning(f"Boss reminder channel {channel_id} send failed; releasing claim")
    try:
        if write_lock is not None:
            async with write_lock:
                await release_alert_claim(write_db, KIND_BOSS_REMINDER, dedupe_key)
        else:
            await release_alert_claim(write_db, KIND_BOSS_REMINDER, dedupe_key)
    except sqlite3.DatabaseError:
        logger.critical("Boss reminder 發送失敗且無法釋放 claim", exc_info=True)
    return False
