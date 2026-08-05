"""轉服警報編排（Discord 送出由呼叫端注入）。"""
from __future__ import annotations

import datetime
import logging
import sqlite3
import traceback
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import discord

from services.game_event_windows import (
    TRANSFER_LOGIN_GRACE_DAYS,
    is_transfer_active_period,
)
from services.transfer_alert_flow import (
    filter_viable_ranked,
    lookup_alerted_pairs,
    pair_key_from_row,
)
from services.transfer_detect import (
    CLASS_MARGIN,
    NAME_MARGIN,
    POTENTIAL_TRANSFERS_SQL,
    format_exp_diff,
    pick_unique_pairs,
    rank_transfer_candidates,
)
from services.transfer_missing import (
    build_missing_queue_rows,
    bump_still_missing,
    fetch_newcomers,
    fetch_open_missing,
    mark_missing_resolved,
    prune_stale_missing,
    resolve_reappeared,
    upsert_disappeared,
)

logger = logging.getLogger(__name__)

SendTransferAlert = Callable[..., Awaitable[int]]


async def fetch_potential_transfers(
    db, time_now, time_prev, name_margin=NAME_MARGIN, class_margin=CLASS_MARGIN
):
    async with db.execute(
        POTENTIAL_TRANSFERS_SQL,
        (time_prev, time_prev, name_margin, class_margin, time_now, time_prev),
    ) as cursor:
        return await cursor.fetchall()


async def send_transfer_alert_message(
    bot,
    channel_ids: Sequence[int],
    *,
    time_now,
    new_name,
    new_server,
    old_name,
    old_server,
    new_lvl,
    new_cls,
    new_sub_grade,
    status_str,
    exp_diff,
    old_guild: str = "",
    new_guild: str = "",
) -> int:
    """發送轉服警報；回傳成功送達的頻道數。"""
    from services.discord_send import send_to_channels

    if not channel_ids:
        return 0
    diff_str = format_exp_diff(exp_diff)
    old_g = old_guild or "—"
    new_g = new_guild or "—"
    embed = discord.Embed(
        title="【波拉西亞戰記】轉移/旅團變動警報",
        description=(
            f"時間：{time_now}\n{'-' * 30}\n"
            f"✨ [即時轉移辨識] **{old_name}** ({old_server}) ➔\n"
            f"**{new_name}** ({new_server})\n"
            f"[狀態]: {status_str} | [EXP變動]: {diff_str}\n"
            f"[屬性]: Lv.{new_lvl} / {new_cls} / 討伐 {new_sub_grade}\n"
            f"[旅團]: {old_g} ➔ {new_g}"
        ),
        color=0xF1C40F,
    )

    async def _send(channel) -> None:
        await channel.send(embed=embed)

    sent = await send_to_channels(
        bot, channel_ids, send_fn=_send, label="transfer alert channel"
    )
    return len(sent)


async def run_transfer_check(
    *,
    write_db: Any,
    read_db: Any,
    time_now,
    time_prev,
    complete_times=None,
    send_alert: SendTransferAlert,
) -> None:
    try:
        in_active = is_transfer_active_period(str(time_now))

        if in_active:
            try:
                await resolve_reappeared(write_db, time_now=str(time_now))
                await upsert_disappeared(
                    write_db,
                    time_now=str(time_now),
                    time_prev=str(time_prev),
                    created_at=str(time_now),
                )
                await bump_still_missing(write_db, time_now=str(time_now))
            except sqlite3.DatabaseError as e:
                logger.error(f"transfer_missing upsert failed: {e}")

        transfer_records = list(
            await fetch_potential_transfers(read_db, time_now, time_prev)
        )

        if in_active:
            try:
                newcomers = await fetch_newcomers(
                    read_db, time_now=str(time_now), time_prev=str(time_prev)
                )
                open_missing = await fetch_open_missing(read_db)
                queue_rows = build_missing_queue_rows(
                    newcomers, open_missing, appear_time=str(time_now)
                )
                if queue_rows:
                    transfer_records.extend(queue_rows)
            except sqlite3.DatabaseError as e:
                logger.error(f"transfer_missing match failed: {e}")

        if not transfer_records:
            return

        miss_times = [time_now]
        if complete_times and len(complete_times) >= 2:
            miss_times = [complete_times[0][0], complete_times[1][0]]

        ranked = rank_transfer_candidates(
            transfer_records,
            appear_time=str(time_now),
            in_active_period=in_active,
        )
        if not ranked:
            return

        candidate_keys = [pair_key_from_row(row) for row in ranked]
        already_alerted = await lookup_alerted_pairs(read_db, candidate_keys)
        viable = await filter_viable_ranked(
            read_db, ranked, already_alerted, miss_times
        )

        for pair in pick_unique_pairs(
            viable, already_alerted, in_active_period=in_active
        ):
            pair_key = pair["pair_key"]
            try:
                cursor = await write_db.execute(
                    """
                    INSERT OR IGNORE INTO transfer_alerts_log
                    (old_name, old_server, new_name, new_server, alert_time)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (*pair_key, time_now),
                )
                await write_db.commit()
                if (cursor.rowcount or 0) != 1:
                    already_alerted.add(pair_key)
                    continue
            except sqlite3.DatabaseError as e:
                logger.error(f"transfer alert claim failed: {e}")
                continue

            sent = await send_alert(
                time_now,
                pair["new_name"],
                pair["new_server"],
                pair["old_name"],
                pair["old_server"],
                pair["new_lvl"],
                pair["new_cls"],
                pair["new_sub_grade"],
                pair["status"],
                pair["exp_diff"],
                old_guild=pair.get("old_guild") or "",
                new_guild=pair.get("new_guild") or "",
            )
            if sent > 0:
                try:
                    await mark_missing_resolved(
                        write_db,
                        pair["old_name"],
                        pair["old_server"],
                        resolved_at=str(time_now),
                    )
                    await write_db.commit()
                except sqlite3.DatabaseError as e:
                    logger.error(
                        "轉服警報已送出但 mark_missing_resolved 失敗: %s", e
                    )
                already_alerted.add(pair_key)
            else:
                try:
                    await write_db.execute(
                        """
                        DELETE FROM transfer_alerts_log
                        WHERE old_name=? AND old_server=?
                          AND new_name=? AND new_server=?
                        """,
                        pair_key,
                    )
                    await write_db.commit()
                except sqlite3.DatabaseError as e:
                    logger.error("回滾轉服警報 claim 失敗: %s", e)
                logger.warning(
                    f"轉服警報送出失敗，已釋放 claim："
                    f"{pair['old_name']}@{pair['old_server']} -> "
                    f"{pair['new_name']}@{pair['new_server']}"
                )

        try:
            cutoff_dt = datetime.datetime.strptime(
                str(time_now), "%Y-%m-%d %H:%M:%S"
            ) - datetime.timedelta(days=TRANSFER_LOGIN_GRACE_DAYS + 7)
            await prune_stale_missing(
                write_db, before=cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")
            )
        except (ValueError, TypeError, sqlite3.DatabaseError) as e:
            logger.warning(f"prune transfer_missing skipped: {e}")
    except sqlite3.DatabaseError as e:
        logger.error(f"DB error in transfer check: {e}\n{traceback.format_exc()}")
    except (ValueError, TypeError, KeyError) as e:
        logger.error(f"Data error in transfer check: {e}\n{traceback.format_exc()}")
    except discord.HTTPException as e:
        logger.error(f"Discord error in transfer check: {e}")
