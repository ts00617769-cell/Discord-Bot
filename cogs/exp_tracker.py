import asyncio
import datetime
import logging
import sqlite3
import traceback

import discord
from discord.ext import commands, tasks

from db import ensure_search_indexes
from db.connection import read_db
from game_data import SERVER_MAP
from services.alert_dedupe import mark_overspeed_sent, overspeed_already_sent
from services.error_handler import (
    min_complete_snapshot_servers,
    min_snapshot_players,
    parse_env_channel_ids,
    parse_env_float,
)
from services.exp_snapshots import (
    EXP_HISTORY_INSERT_SQL,
    PLAYER_PROFILE_UPSERT_SQL,
    players_to_insert_batch,
    profiles_from_insert_batch,
)
from services.exp_speed import (
    collect_overspeed,
    pick_interval_baseline,
)
from services.ranking_api import get_ranking_client
from services.text_display import pad_text
from services.timeutil import now_naive_taipei
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


class ExpTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        threshold_raw = parse_env_float("EXP_ALERT_THRESHOLD", 4000 * 100_000_000)
        self.SPEED_LIMIT = threshold_raw / 100_000_000
        self.alerts_enabled = False
        self.alert_count = 30
        self.alert_server = "全服"
        self.alert_interval_minutes = 30

    @property
    def ALERT_CHANNEL_IDS(self):
        return parse_env_channel_ids(env_name="EXP_ALERT_CHANNEL_ID")

    @property
    def TRANSFER_ALERT_CHANNEL_IDS(self):
        return parse_env_channel_ids(env_name="TRANSFER_ALERT_CHANNEL_ID")

    async def cog_load(self):
        # schema 已在 bot.setup_hook 套用；!reload 時不必再 migrate
        await self._load_alert_settings()
        if not self.TRANSFER_ALERT_CHANNEL_IDS:
            logger.warning(
                "⚠️ TRANSFER_ALERT_CHANNEL_ID 未設定或無效：轉服警報不會發送。"
            )
        self._index_task = asyncio.create_task(self._ensure_search_indexes())
        self.auto_fetch_exp.start()
        self._validate_task = asyncio.create_task(self._startup_validate_servers())

    def cog_unload(self):
        self.auto_fetch_exp.cancel()
        for attr in ("_index_task", "_validate_task"):
            task = getattr(self, attr, None)
            if task and not task.done():
                task.cancel()

    async def _load_alert_settings(self):
        """從 DB 還原警報開關，避免重啟後漏監控。"""
        try:
            async with self.bot.db.execute(
                "SELECT key, value FROM bot_settings WHERE key LIKE 'alert_%'"
            ) as cursor:
                rows = {k: v for k, v in await cursor.fetchall()}
            if "alert_enabled" in rows:
                self.alerts_enabled = rows["alert_enabled"] == "1"
            if "alert_count" in rows:
                try:
                    self.alert_count = max(1, min(100, int(rows["alert_count"])))
                except ValueError:
                    logger.warning(
                        f"忽略無效 alert_count={rows['alert_count']!r}，沿用 {self.alert_count}"
                    )
            if "alert_server" in rows and rows["alert_server"]:
                self.alert_server = rows["alert_server"]
            logger.info(
                f"警報設定已載入: enabled={self.alerts_enabled} "
                f"count={self.alert_count} server={self.alert_server}"
            )
        except sqlite3.DatabaseError as e:
            logger.error(f"載入警報設定失敗: {e}")

    async def _save_alert_settings(self):
        pairs = [
            ("alert_enabled", "1" if self.alerts_enabled else "0"),
            ("alert_count", str(self.alert_count)),
            ("alert_server", self.alert_server),
        ]
        await self.bot.db.executemany(
            '''
            INSERT INTO bot_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            ''',
            pairs,
        )
        await self.bot.db.commit()

    async def _setting_exists(self, key: str) -> bool:
        """相容舊 bot_settings overspeed:*；新寫入走 alert_dedupe。"""
        return await overspeed_already_sent(read_db(self.bot), key)

    async def _mark_setting(self, key: str, value: str = "1") -> None:
        now = now_naive_taipei().strftime("%Y-%m-%d %H:%M:%S")
        await mark_overspeed_sent(self.bot.db, key, created_at=now)

    async def _startup_validate_servers(self):
        try:
            for _ in range(60):
                if self.bot.is_closed():
                    return
                if self.bot.is_ready():
                    break
                await asyncio.sleep(1)
            if self.bot.is_closed():
                return
            client = get_ranking_client(self.bot)
            results = await client.validate_server_map(SERVER_MAP)
            dead = [name for name, r in results.items() if not r.get("ok")]
            if dead:
                logger.warning(
                    f"⚠️ SERVER_MAP 探活失敗（官網 Ranking API 無資料）: {', '.join(dead)}"
                )
            else:
                logger.info(f"✅ SERVER_MAP 探活通過，共 {len(results)} 服")
        except asyncio.CancelledError:
            return
        except (asyncio.TimeoutError, OSError) as e:
            logger.warning(f"啟動伺服器探活略過: {e}")

    async def _ensure_search_indexes(self):
        try:
            for _ in range(120):
                if self.bot.is_closed():
                    return
                if self.bot.is_ready():
                    break
                await asyncio.sleep(1)
            if self.bot.is_closed():
                return
            await ensure_search_indexes(self.bot.db)
        except asyncio.CancelledError:
            return
        except sqlite3.DatabaseError as e:
            logger.error(f"❌ 檢查/建立尋人索引時發生資料庫錯誤: {e}")

    @tasks.loop(minutes=10.0)
    async def auto_fetch_exp(self):
        try:
            now_time = now_naive_taipei().replace(second=0, microsecond=0)
            logger.info(f"[{now_time.strftime('%H:%M:%S')}] 哨兵出動：掃描全服前100名...")
            client = get_ranking_client(self.bot)

            servers_ok = []
            servers_failed = []
            servers_thin = []
            min_players = min_snapshot_players()

            for server_name, (g_id, w_id) in SERVER_MAP.items():
                try:
                    result = await client.fetch_server(g_id, w_id)
                    if not result.ok:
                        servers_failed.append(server_name)
                        logger.warning(
                            f"⚠️ 伺服器 {server_name} 抓取失敗，略過寫入: {result.error}"
                        )
                        continue

                    insert_batch = players_to_insert_batch(
                        now_time, server_name, result.players
                    )

                    if insert_batch:
                        await self.bot.db.executemany(
                            EXP_HISTORY_INSERT_SQL, insert_batch
                        )
                        profile_batch = profiles_from_insert_batch(insert_batch)
                        if profile_batch:
                            await self.bot.db.executemany(
                                PLAYER_PROFILE_UPSERT_SQL, profile_batch
                            )
                        await self.bot.db.commit()

                    # 總榜成功且人數達標才算完整服；薄名冊仍寫入但不計入轉服門檻
                    quality_ok = (
                        result.overall_ok and len(result.players) >= min_players
                    )
                    if quality_ok:
                        servers_ok.append(server_name)
                    else:
                        servers_thin.append(server_name)
                        logger.warning(
                            f"⚠️ 伺服器 {server_name} 快照品質不足 "
                            f"(overall_ok={result.overall_ok} "
                            f"players={len(result.players)}/{min_players} "
                            f"partial={result.partial})；已寫入但不計入完整快照"
                        )
                except sqlite3.DatabaseError as e:
                    servers_failed.append(server_name)
                    logger.error(f"DB error processing server {server_name}: {e}")
                    continue
                except (asyncio.TimeoutError, OSError) as e:
                    servers_failed.append(server_name)
                    logger.error(f"Network/IO error processing server {server_name}: {e}")
                    continue
                await asyncio.sleep(0.5)

            snapshot_complete = len(servers_ok) >= min_complete_snapshot_servers()
            if not snapshot_complete:
                logger.warning(
                    f"⚠️ 本輪快照不完整 ok={len(servers_ok)}/"
                    f"{min_complete_snapshot_servers()} "
                    f"(map={len(SERVER_MAP)}) failed={servers_failed} "
                    f"thin={servers_thin}；跳過轉服偵測"
                )
            await self.check_for_alerts(now_time, run_transfers=snapshot_complete)
        except sqlite3.DatabaseError as e:
            logger.error(f"🚨 [經驗值雷達] 資料庫錯誤：{e}\n{traceback.format_exc()}")
        except (asyncio.TimeoutError, OSError) as e:
            logger.error(f"🚨 [經驗值雷達] 網路/IO 錯誤：{e}\n{traceback.format_exc()}")
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"🚨 [經驗值雷達] 資料格式錯誤：{e}\n{traceback.format_exc()}")
        except discord.HTTPException as e:
            logger.error(f"🚨 [經驗值雷達] Discord 發送失敗：{e}")
        except Exception as e:
            logger.error(f"🚨 [經驗值雷達] 未預期錯誤：{e}\n{traceback.format_exc()}")

    async def check_for_alerts(self, current_time, *, run_transfers: bool = True):
        min_servers = min_complete_snapshot_servers()
        sql_times = """
            SELECT record_time
            FROM exp_history
            GROUP BY record_time
            HAVING COUNT(DISTINCT server_name) >= ?
            ORDER BY record_time DESC LIMIT 10
        """
        async with read_db(self.bot).execute(sql_times, (min_servers,)) as cursor:
            times = [tuple(r) for r in await cursor.fetchall()]

        if len(times) < 2:
            return
        time_now, time_prev_scan = times[0][0], times[1][0]

        fmt = "%Y-%m-%d %H:%M:%S"

        should_alert = self.alerts_enabled
        if should_alert and isinstance(current_time, datetime.datetime):
            should_alert = (current_time.minute % self.alert_interval_minutes) == 0

        if should_alert:
            time_prev, minutes_diff = pick_interval_baseline(
                times, self.alert_interval_minutes, fmt=fmt
            )
            if time_prev and minutes_diff > 0:
                if self.alert_server == "全服":
                    sql = '''
                        SELECT DISTINCT t1.player_name, t1.server_name, t1.level, t1.exp, t2.exp
                        FROM exp_history t1
                        JOIN exp_history t2
                          ON t1.player_name = t2.player_name AND t1.server_name = t2.server_name
                        WHERE t1.record_time = ? AND t2.record_time = ?
                    '''
                    params: tuple = (time_now, time_prev)
                else:
                    sql = '''
                        SELECT DISTINCT t1.player_name, t1.server_name, t1.level, t1.exp, t2.exp
                        FROM exp_history t1
                        JOIN exp_history t2
                          ON t1.player_name = t2.player_name AND t1.server_name = t2.server_name
                        WHERE t1.record_time = ? AND t2.record_time = ? AND t1.server_name = ?
                    '''
                    params = (time_now, time_prev, self.alert_server)

                async with read_db(self.bot).execute(sql, params) as cursor:
                    records = [tuple(r) for r in await cursor.fetchall()]

                alert_list = collect_overspeed(
                    records, minutes_diff, self.SPEED_LIMIT
                )[: self.alert_count]

                if alert_list:
                    dedupe_key = (
                        f"overspeed:{time_now}|{time_prev}|"
                        f"{self.alert_server}|{self.alert_count}"
                    )
                    try:
                        if await self._setting_exists(dedupe_key):
                            logger.info(
                                f"略過重複超速警報 interval={time_prev}→{time_now} "
                                f"server={self.alert_server}"
                            )
                            alert_list = []
                    except sqlite3.DatabaseError as e:
                        logger.error(f"Overspeed dedupe check failed: {e}")
                        alert_list = []

                if alert_list:
                    chunk_size = 50
                    for i in range(0, len(alert_list), chunk_size):
                        chunk = alert_list[i : i + chunk_size]
                        embed = discord.Embed(
                            title=f"🚨 超速警報 ({self.alert_server} ≥{self.SPEED_LIMIT:,.0f}億 Top {self.alert_count})",
                            color=0xff0000,
                        )
                        desc = ""
                        if i == 0:
                            desc += f"以下是時速超過 **{self.SPEED_LIMIT:,.0f} 億** 的前 {len(alert_list)} 名玩家：\n"
                        desc += "```yaml\n"
                        for p in chunk:
                            name_padded = pad_text(p["name"], 14)
                            desc += f"[{p['server']}] {name_padded} | Lv.{p['level']} | 時速: {p['speed']:,.0f}億\n"
                        desc += "```"
                        embed.description = desc
                        if i + chunk_size >= len(alert_list):
                            embed.set_footer(
                                text=f"掃描時間: {time_now} (監控週期: {int(minutes_diff)}min)"
                            )
                        for channel_id in self.ALERT_CHANNEL_IDS:
                            channel = self.bot.get_channel(channel_id)
                            if channel:
                                try:
                                    await channel.send(embed=embed)
                                except discord.HTTPException as e:
                                    logger.error(f"Failed to send alert to {channel_id}: {e}")
                    try:
                        await self._mark_setting(dedupe_key)
                    except sqlite3.DatabaseError as e:
                        logger.error(f"Failed to persist overspeed dedupe: {e}")

        if run_transfers:
            await self.check_for_transfers(time_now, time_prev_scan, complete_times=times)
        else:
            logger.info("本輪略過轉服偵測（快照不完整）")

    async def _get_potential_transfers(self, time_now, time_prev, name_margin, class_margin):
        """轉服候選：同名跨服最優先；異名僅允許同職+同討伐+等級接近+較小經驗差。"""
        async with read_db(self.bot).execute(
            POTENTIAL_TRANSFERS_SQL,
            (time_prev, time_prev, name_margin, class_margin, time_now, time_prev),
        ) as cursor:
            return await cursor.fetchall()

    async def _send_transfer_alert(
        self, time_now, new_name, new_server, old_name, old_server,
        new_lvl, new_cls, new_sub_grade, status_str, exp_diff,
        *,
        old_guild: str = "",
        new_guild: str = "",
    ) -> int:
        """發送轉服警報；回傳成功送達的頻道數。"""
        if not self.TRANSFER_ALERT_CHANNEL_IDS:
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
            color=0xf1c40f,
        )
        success = 0
        for channel_id in self.TRANSFER_ALERT_CHANNEL_IDS:
            channel = self.bot.get_channel(channel_id)
            if channel:
                try:
                    await channel.send(embed=embed)
                    success += 1
                except discord.HTTPException as e:
                    logger.error(f"Failed to send transfer alert to {channel_id}: {e}")
        return success

    async def check_for_transfers(self, time_now, time_prev, complete_times=None):
        try:
            in_active = is_transfer_active_period(str(time_now))
            write_db = self.bot.db
            db = read_db(self.bot)

            # 轉移活躍期：維護消失佇列（連續缺席／同服回歸）
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
                await self._get_potential_transfers(
                    time_now, time_prev, NAME_MARGIN, CLASS_MARGIN
                )
            )

            if in_active:
                try:
                    newcomers = await fetch_newcomers(
                        db, time_now=str(time_now), time_prev=str(time_prev)
                    )
                    open_missing = await fetch_open_missing(db)
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
            already_alerted = await lookup_alerted_pairs(db, candidate_keys)
            viable = await filter_viable_ranked(
                db, ranked, already_alerted, miss_times
            )

            for pair in pick_unique_pairs(
                viable, already_alerted, in_active_period=in_active
            ):
                sent = await self._send_transfer_alert(
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
                    await write_db.execute(
                        '''
                        INSERT INTO transfer_alerts_log
                        (old_name, old_server, new_name, new_server, alert_time)
                        VALUES (?, ?, ?, ?, ?)
                        ''',
                        (*pair["pair_key"], time_now),
                    )
                    await mark_missing_resolved(
                        write_db,
                        pair["old_name"],
                        pair["old_server"],
                        resolved_at=str(time_now),
                    )
                    await write_db.commit()
                    already_alerted.add(pair["pair_key"])
                else:
                    logger.warning(
                        f"轉服警報送出失敗，未寫入 dedupe："
                        f"{pair['old_name']}@{pair['old_server']} -> "
                        f"{pair['new_name']}@{pair['new_server']}"
                    )

            # 清理過舊佇列（窗結束 + grace + 7 天）
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

    @auto_fetch_exp.before_loop
    async def before_auto_fetch(self):
        await self.bot.wait_until_ready()
        now = now_naive_taipei()
        next_run = now.replace(
            minute=(now.minute // 10) * 10, second=30, microsecond=0
        ) + datetime.timedelta(minutes=10)
        if now.minute % 10 == 0 and now.second < 30:
             next_run = now.replace(second=30, microsecond=0)
        seconds_to_wait = (next_run - now).total_seconds()
        if seconds_to_wait > 0:
            logger.info(f"等待 {seconds_to_wait:.1f} 秒以對齊官方每 10 分鐘更新時間...")
            await asyncio.sleep(seconds_to_wait)

async def setup(bot):
    await bot.add_cog(ExpTracker(bot))
