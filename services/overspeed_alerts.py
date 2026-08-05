"""超速警報：embed 組裝、claim-first 去重、多頻道送出。"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any, Protocol

import discord

from services.alert_dedupe import (
    KIND_OVERSPEED,
    alert_already_sent,
    release_alert_claim,
    try_claim_alert,
)
from services.discord_send import send_embeds_to_channels
from services.exp_speed import collect_overspeed, pick_interval_baseline
from services.text_display import pad_text
from services.timeutil import now_naive_taipei

logger = logging.getLogger(__name__)


class OverspeedSettings(Protocol):
    alerts_enabled: bool
    alert_server: str
    alert_guild: str
    alert_count: int
    alert_interval_minutes: int
    alert_speed_window_minutes: int
    SPEED_LIMIT: float
    ALERT_CHANNEL_IDS: list[int]


def send_clear_patrol_enabled() -> bool:
    """EXP_ALERT_SEND_CLEAR=1 時才發送「無人超速」綠訊（預設關閉）。"""
    raw = (os.getenv("EXP_ALERT_SEND_CLEAR", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def build_overspeed_embeds(
    settings: OverspeedSettings,
    alert_list: list[dict],
    *,
    record_count: int,
    time_now: str,
    minutes_diff: float,
    include_clear: bool | None = None,
) -> list[discord.Embed]:
    """產生超速／巡檢 embeds。

    無超速者時：預設不送綠訊（可用 EXP_ALERT_SEND_CLEAR=1 開啟）；
    旅團無資料時仍送黃訊提醒設定問題。
    """
    footer = f"掃描時間: {time_now} (監控週期: {int(minutes_diff)}min)"
    if not alert_list:
        if not record_count:
            description = (
                f"本輪找不到「{settings.alert_server}／{settings.alert_guild}」"
                "可比較的玩家資料，請確認旅團名稱與榜單資料。"
            )
            embed = discord.Embed(
                title=(
                    f"✅ 10 分鐘超速巡檢 "
                    f"({settings.alert_server}／{settings.alert_guild})"
                ),
                description=description,
                color=0xF1C40F,
            )
            embed.set_footer(text=footer)
            return [embed]
        if include_clear is None:
            include_clear = send_clear_patrol_enabled()
        if not include_clear:
            return []
        description = (
            f"本輪已比對 **{record_count}** 名玩家，"
            f"沒有人超過 **{settings.SPEED_LIMIT:,.0f} 億／小時**。"
        )
        embed = discord.Embed(
            title=(
                f"✅ 10 分鐘超速巡檢 "
                f"({settings.alert_server}／{settings.alert_guild})"
            ),
            description=description,
            color=0x2ECC71,
        )
        embed.set_footer(text=footer)
        return [embed]

    embeds: list[discord.Embed] = []
    chunk_size = 50
    for i in range(0, len(alert_list), chunk_size):
        chunk = alert_list[i : i + chunk_size]
        desc = ""
        if i == 0:
            desc += (
                f"以下是時速超過 **{settings.SPEED_LIMIT:,.0f} 億** "
                f"的前 {len(alert_list)} 名玩家：\n"
            )
        desc += "```yaml\n"
        for player in chunk:
            name_padded = pad_text(player["name"], 14)
            desc += (
                f"[{player['server']}] {name_padded} | "
                f"Lv.{player['level']} | 時速: {player['speed']:,.0f}億\n"
            )
        desc += "```"
        embed = discord.Embed(
            title=(
                f"🚨 超速警報 ({settings.alert_server}／{settings.alert_guild} "
                f"≥{settings.SPEED_LIMIT:,.0f}億 Top {settings.alert_count})"
            ),
            description=desc,
            color=0xFF0000,
        )
        if i + chunk_size >= len(alert_list):
            embed.set_footer(text=footer)
        embeds.append(embed)
    return embeds


async def run_overspeed_patrol(
    bot,
    settings: OverspeedSettings,
    *,
    read_db: Any,
    write_db: Any,
    times: list[tuple],
    current_time,
    fmt: str = "%Y-%m-%d %H:%M:%S",
    write_lock: Any | None = None,
) -> None:
    """對啟用中的旅團超速監控執行 claim-first 送出。"""
    import datetime

    should_alert = (
        settings.alerts_enabled
        and settings.alert_server != "全服"
        and bool(settings.alert_guild)
    )
    if should_alert and isinstance(current_time, datetime.datetime):
        should_alert = (current_time.minute % settings.alert_interval_minutes) == 0
    if not should_alert or len(times) < 2:
        return

    time_now = times[0][0]
    time_prev, minutes_diff = pick_interval_baseline(
        times, settings.alert_speed_window_minutes, fmt=fmt
    )
    if not time_prev or minutes_diff <= 0:
        return

    sql = """
        SELECT DISTINCT t1.player_name, t1.server_name, t1.level, t1.exp, t2.exp
        FROM exp_history t1
        JOIN exp_history t2
          ON t1.player_name = t2.player_name AND t1.server_name = t2.server_name
        WHERE t1.record_time = ? AND t2.record_time = ?
          AND t1.server_name = ?
          AND lower(t1.guild_name) = lower(?)
          AND lower(t2.guild_name) = lower(?)
    """
    params = (
        time_now,
        time_prev,
        settings.alert_server,
        settings.alert_guild,
        settings.alert_guild,
    )
    async with read_db.execute(sql, params) as cursor:
        records = [tuple(r) for r in await cursor.fetchall()]

    alert_list = collect_overspeed(
        records, minutes_diff, settings.SPEED_LIMIT
    )[: settings.alert_count]

    embeds = build_overspeed_embeds(
        settings,
        alert_list,
        record_count=len(records),
        time_now=time_now,
        minutes_diff=minutes_diff,
    )
    if not embeds:
        logger.info(
            "超速巡檢無違規且未啟用綠訊，略過送出 "
            f"server={settings.alert_server} guild={settings.alert_guild}"
        )
        return

    dedupe_key = (
        f"overspeed:{time_now}|{time_prev}|"
        f"{settings.alert_server}|{settings.alert_guild}|{settings.alert_count}"
    )
    claimed_channels: list[int] = []
    created_at = now_naive_taipei().strftime("%Y-%m-%d %H:%M:%S")

    async def _claim_channels() -> list[int]:
        claimed: list[int] = []
        for channel_id in settings.ALERT_CHANNEL_IDS:
            channel_key = f"{dedupe_key}|channel:{channel_id}"
            try:
                if await alert_already_sent(read_db, KIND_OVERSPEED, channel_key):
                    continue
                if not await try_claim_alert(
                    write_db, KIND_OVERSPEED, channel_key, created_at=created_at
                ):
                    continue
            except sqlite3.DatabaseError as e:
                logger.error(
                    "Overspeed dedupe claim failed for channel %s: %s",
                    channel_id,
                    e,
                )
                continue
            claimed.append(channel_id)
        return claimed

    try:
        if write_lock is not None:
            async with write_lock:
                claimed_channels = await _claim_channels()
        else:
            claimed_channels = await _claim_channels()
    except sqlite3.DatabaseError as e:
        logger.error("Overspeed claim phase failed: %s", e)
        return

    if not claimed_channels:
        logger.info(
            f"略過重複超速巡檢 interval={time_prev}→{time_now} "
            f"server={settings.alert_server} guild={settings.alert_guild}"
        )
        return

    sent_channels = await send_embeds_to_channels(
        bot,
        claimed_channels,
        embeds,
        label="overspeed alert channel",
    )
    failed_channels = set(claimed_channels) - sent_channels

    async def _release_failed() -> None:
        for channel_id in failed_channels:
            channel_key = f"{dedupe_key}|channel:{channel_id}"
            try:
                await release_alert_claim(write_db, KIND_OVERSPEED, channel_key)
            except sqlite3.DatabaseError as e:
                logger.error(
                    "Failed to release overspeed claim for channel %s: %s",
                    channel_id,
                    e,
                )

    if failed_channels:
        if write_lock is not None:
            async with write_lock:
                await _release_failed()
        else:
            await _release_failed()
        logger.warning(
            "超速巡檢部分頻道送出失敗，已釋放該頻道 claim："
            f"{settings.alert_server}/{settings.alert_guild} "
            f"interval={time_prev}→{time_now} "
            f"channels={sorted(failed_channels)}"
        )
