"""經驗值快照循環：協調超速／轉服警報。"""
from __future__ import annotations

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
from services.error_handler import (
    min_complete_snapshot_servers,
    min_snapshot_players,
    parse_env_channel_ids,
    parse_env_float,
)
from services.exp_snapshots import (
    fetch_recent_complete_snapshot_times,
    normalize_guild,
    persist_snapshot_round,
    players_to_insert_batch,
)
from services.overspeed_alerts import (
    build_overspeed_embeds,
    run_overspeed_patrol,
)
from services.ranking_api import get_ranking_client
from services.search_cache import invalidate_player_search_cache
from services.timeutil import now_naive_taipei
from services.transfer_alert_runner import (
    fetch_potential_transfers,
    run_transfer_check,
    send_transfer_alert_message,
)

logger = logging.getLogger(__name__)


class ExpTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        threshold_raw = parse_env_float("EXP_ALERT_THRESHOLD", 2000 * 100_000_000)
        self.SPEED_LIMIT = threshold_raw / 100_000_000
        self.alerts_enabled = False
        self.alert_count = 30
        self.alert_server = "全服"
        self.alert_guild = ""
        self.alert_interval_minutes = 10
        self.alert_speed_window_minutes = 10

    @property
    def ALERT_CHANNEL_IDS(self):
        return parse_env_channel_ids(env_name="EXP_ALERT_CHANNEL_ID")

    @property
    def TRANSFER_ALERT_CHANNEL_IDS(self):
        return parse_env_channel_ids(env_name="TRANSFER_ALERT_CHANNEL_ID")

    async def cog_load(self):
        await self._load_alert_settings()
        if not self.ALERT_CHANNEL_IDS:
            logger.warning(
                "⚠️ EXP_ALERT_CHANNEL_ID 未設定或無效：超速警報不會發送。"
            )
        if not self.TRANSFER_ALERT_CHANNEL_IDS:
            logger.warning(
                "⚠️ TRANSFER_ALERT_CHANNEL_ID 未設定或無效：轉服警報不會發送。"
            )
        self._index_task = asyncio.create_task(self._ensure_search_indexes())
        if not self.auto_fetch_exp.is_running():
            self.auto_fetch_exp.start()
        self._validate_task = asyncio.create_task(self._startup_validate_servers())

    def cog_unload(self):
        if self.auto_fetch_exp.is_running():
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
            if "alert_guild" in rows:
                self.alert_guild = normalize_guild(rows["alert_guild"])
            if self.alerts_enabled and (
                self.alert_server == "全服" or not self.alert_guild
            ):
                self.alerts_enabled = False
                logger.warning(
                    "舊版警報設定缺少指定伺服器或旅團，已安全停用；"
                    "請重新執行 !警報 開 [數量] [伺服器] [旅團名稱]"
                )
                try:
                    await self._save_alert_settings()
                except sqlite3.DatabaseError as e:
                    logger.error(f"持久化停用舊版警報設定失敗: {e}")
            logger.info(
                f"警報設定已載入: enabled={self.alerts_enabled} "
                f"count={self.alert_count} server={self.alert_server} "
                f"guild={self.alert_guild or '未設定'}"
            )
        except sqlite3.DatabaseError as e:
            logger.error(f"載入警報設定失敗: {e}")

    async def _save_alert_settings(self):
        pairs = [
            ("alert_enabled", "1" if self.alerts_enabled else "0"),
            ("alert_count", str(self.alert_count)),
            ("alert_server", self.alert_server),
            ("alert_guild", self.alert_guild),
        ]
        await self.bot.db.executemany(
            """
            INSERT INTO bot_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            pairs,
        )
        await self.bot.db.commit()

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
            round_batches: list[list[tuple]] = []
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
                        round_batches.append(insert_batch)

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

            async with self.bot.db_write_lock:
                await persist_snapshot_round(
                    getattr(self.bot, "snapshot_db", self.bot.db),
                    round_batches,
                )
            invalidate_player_search_cache()
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
        times = await fetch_recent_complete_snapshot_times(
            read_db(self.bot), min_complete_snapshot_servers(), limit=10
        )
        if len(times) < 2:
            return
        time_now, time_prev_scan = times[0][0], times[1][0]
        write_lock = getattr(self.bot, "db_write_lock", None)

        await run_overspeed_patrol(
            self.bot,
            self,
            read_db=read_db(self.bot),
            write_db=self.bot.db,
            times=times,
            current_time=current_time,
            write_lock=write_lock,
        )

        if run_transfers:
            await self.check_for_transfers(
                time_now, time_prev_scan, complete_times=times
            )
        else:
            logger.info("本輪略過轉服偵測（快照不完整）")

    def _build_overspeed_embeds(
        self,
        alert_list: list[dict],
        *,
        record_count: int,
        time_now: str,
        minutes_diff: float,
        include_clear: bool | None = None,
    ) -> list[discord.Embed]:
        """測試／相容用包裝。"""
        return build_overspeed_embeds(
            self,
            alert_list,
            record_count=record_count,
            time_now=time_now,
            minutes_diff=minutes_diff,
            include_clear=include_clear,
        )

    async def _send_overspeed_embeds(
        self,
        embeds: list,
        *,
        channel_ids: list[int] | None = None,
    ) -> set[int]:
        from services.discord_send import send_embeds_to_channels

        targets = channel_ids if channel_ids is not None else self.ALERT_CHANNEL_IDS
        return await send_embeds_to_channels(
            self.bot, targets, embeds, label="overspeed alert channel"
        )

    async def _get_potential_transfers(self, time_now, time_prev, name_margin, class_margin):
        return await fetch_potential_transfers(
            read_db(self.bot), time_now, time_prev, name_margin, class_margin
        )

    async def _send_transfer_alert(
        self, time_now, new_name, new_server, old_name, old_server,
        new_lvl, new_cls, new_sub_grade, status_str, exp_diff,
        *,
        old_guild: str = "",
        new_guild: str = "",
        channel_ids: list[int] | None = None,
    ) -> set[int]:
        targets = (
            channel_ids
            if channel_ids is not None
            else self.TRANSFER_ALERT_CHANNEL_IDS
        )
        return await send_transfer_alert_message(
            self.bot,
            targets,
            time_now=time_now,
            new_name=new_name,
            new_server=new_server,
            old_name=old_name,
            old_server=old_server,
            new_lvl=new_lvl,
            new_cls=new_cls,
            new_sub_grade=new_sub_grade,
            status_str=status_str,
            exp_diff=exp_diff,
            old_guild=old_guild,
            new_guild=new_guild,
        )

    async def check_for_transfers(self, time_now, time_prev, complete_times=None):
        await run_transfer_check(
            write_db=self.bot.db,
            read_db=read_db(self.bot),
            time_now=time_now,
            time_prev=time_prev,
            complete_times=complete_times,
            channel_ids=self.TRANSFER_ALERT_CHANNEL_IDS,
            send_alert=self._send_transfer_alert,
            write_lock=getattr(self.bot, "db_write_lock", None),
        )

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
