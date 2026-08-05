"""時空縫隙 Boss 提醒：claim-first + discord_send。"""
from __future__ import annotations

import datetime
import logging
import sqlite3
from typing import Any, Optional

import discord

from game_data import GAP_BOSS_SCHEDULE
from services.alert_dedupe import (
    KIND_BOSS_REMINDER,
    alert_already_sent,
    release_alert_claim,
    try_claim_alert,
)
from services.db_lock import run_locked
from services.discord_send import send_to_channels
from services.timeutil import now_naive_taipei, now_taipei, today_taipei_str

logger = logging.getLogger(__name__)

# 召喚前多少分鐘內都可送出提醒（重啟／卡頓錯過整分鐘時仍能補送）
REMINDER_LEAD_MINUTES = 10


def find_pending_boss_hour(
    now: datetime.datetime, *, lead_minutes: int = REMINDER_LEAD_MINUTES
) -> Optional[tuple[datetime.datetime, int]]:
    """回傳 (召喚時間, 剩餘分鐘)；不在提醒窗內回 None。

    只要下一個召喚整點落在 lead_minutes 內就會回報，因此錯過剛好第 10
    分鐘的那一輪（重啟、事件圈延遲）仍會在下一分鐘補送。
    """
    for offset in range(1, int(lead_minutes) + 1):
        candidate = now + datetime.timedelta(minutes=offset)
        if candidate.minute != 0:
            continue
        if candidate.hour in GAP_BOSS_SCHEDULE.get(candidate.weekday(), []):
            return candidate, offset
    return None


def build_boss_reminder_embed(
    weekday: int, *, minutes_left: int = REMINDER_LEAD_MINUTES
) -> discord.Embed:
    time_str = "點、".join(map(str, GAP_BOSS_SCHEDULE[weekday])) + "點"
    return discord.Embed(
        title="🕒 時空縫隙首領召喚提醒",
        description=(
            f"**{minutes_left} 分鐘後** 將開始召喚首領！\n\n"
            f"今天召喚時段\n✅ **{time_str}**"
        ),
        color=discord.Color.red(),
    )


async def _claim_reminder(write_db: Any, dedupe_key: str, created_at: str) -> bool:
    if await alert_already_sent(write_db, KIND_BOSS_REMINDER, dedupe_key):
        return False
    return await try_claim_alert(
        write_db, KIND_BOSS_REMINDER, dedupe_key, created_at=created_at
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

    pending = find_pending_boss_hour(now_taipei())
    if pending is None:
        return False
    target, minutes_left = pending

    dedupe_key = f"boss_reminder:{today_taipei_str()}:{target.hour:02d}"
    created_at = now_naive_taipei().strftime("%Y-%m-%d %H:%M:%S")

    try:
        claimed = await run_locked(
            write_lock, _claim_reminder, write_db, dedupe_key, created_at
        )
    except sqlite3.DatabaseError as e:
        logger.error(f"Boss reminder dedupe check failed: {e}")
        return False
    if not claimed:
        return False

    embed = build_boss_reminder_embed(target.weekday(), minutes_left=minutes_left)

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
        await run_locked(
            write_lock,
            release_alert_claim,
            write_db,
            KIND_BOSS_REMINDER,
            dedupe_key,
        )
    except sqlite3.DatabaseError:
        logger.critical("Boss reminder 發送失敗且無法釋放 claim", exc_info=True)
    return False
