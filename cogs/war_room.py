import asyncio
import datetime
import logging
import sqlite3
import traceback

import discord
from discord.ext import commands, tasks

from db.connection import read_db
from db.schema import (
    ONLINE_FULL_REBUILD_EXP_DELETED_THRESHOLD,
    backfill_player_profile_denorm,
    denorm_coverage_stats,
    list_missing_search_indexes_async,
    prune_orphaned_player_profiles,
    rebuild_player_profiles,
)
from services.error_handler import parse_env_channel_id, resolve_bot_channel
from services.game_event_windows import transfer_calendar_health_notes
from services.retention_windows import (
    DEFAULT_MAX_TRANSFER_WINDOWS,
    DEFAULT_RECENT_DAYS,
    DEFAULT_TRANSFER_PAD_DAYS,
    ONLINE_CHECKPOINT_EVERY_BATCHES,
    ONLINE_DELETE_BATCH_SIZE,
    build_bridge_thin_ranges,
    build_search_keep_ranges,
    build_transfer_thin_ranges,
    exp_history_outside_keep_batch_sql,
    exp_history_transfer_middle_batch_sql,
    search_retention_cutoff,
    transfer_alert_retention_cutoff,
)
from services.settings_prune import (
    PRUNE_ALERT_DEDUPE_SQL,
    PRUNE_DEDUPE_SQL,
    boss_reminder_prune_bound,
    overspeed_prune_bound,
)
from services.timeutil import TAIPEI, now_taipei, today_taipei_str

logger = logging.getLogger(__name__)


class WarRoom(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._ready_announced = False

    @property
    def log_channel_id(self) -> int:
        return parse_env_channel_id("WAR_ROOM_CHANNEL_ID", 0)

    async def cog_load(self):
        if not self.db_cleanup_task.is_running():
            self.db_cleanup_task.start()
        if not self.health_summary_task.is_running():
            self.health_summary_task.start()

    def cog_unload(self):
        if self.db_cleanup_task.is_running():
            self.db_cleanup_task.cancel()
        if self.health_summary_task.is_running():
            self.health_summary_task.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("[模組載入] WarRoom (戰情室監控與排程) 運作中")
        if self._ready_announced or not self.log_channel_id:
            return
        self._ready_announced = True
        channel = await resolve_bot_channel(
            self.bot, self.log_channel_id, label="war room log channel"
        )
        if channel:
            try:
                await channel.send(
                    "🟢 **【系統廣播】** 戰情雷達已重新啟動，後勤監視與自動排程上線。"
                )
            except discord.HTTPException as e:
                logger.error(f"Failed to send war room ready message: {e}")

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CheckFailure) and str(error) in (
            "duplicate_invoke",
            "channel_denied",
        ):
            return

        if isinstance(error, commands.CommandOnCooldown):
            try:
                await ctx.send(
                    f"⏳ 指令冷卻中，請再等 **{error.retry_after:.0f}** 秒。",
                    delete_after=10,
                )
            except discord.HTTPException:
                pass
            return
        if isinstance(error, commands.MaxConcurrencyReached):
            try:
                await ctx.send(
                    "⏳ 相同指令尚在執行中，請稍候完成後再試。", delete_after=10
                )
            except discord.HTTPException:
                pass
            return
        if isinstance(error, commands.UserInputError):
            try:
                usage = getattr(ctx.command, "help", None) or "請檢查指令參數。"
                await ctx.send(f"❌ 參數錯誤。{usage}", delete_after=15)
            except discord.HTTPException:
                pass
            return

        if isinstance(
            error,
            (
                commands.CommandNotFound,
                commands.NotOwner,
                commands.CheckFailure,
            ),
        ):
            return

        logger.error(
            f"Command error in {getattr(ctx.command, 'name', '?')}: {error}\n"
            f"{''.join(traceback.format_exception(type(error), error, error.__traceback__))}"
        )

        try:
            await ctx.send("❌ 指令執行失敗，已記錄。", delete_after=15)
        except discord.HTTPException:
            pass

        if not self.log_channel_id:
            return

        channel = await resolve_bot_channel(
            self.bot, self.log_channel_id, label="war room log channel"
        )
        if channel:
            error_msg = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )
            cmd_name = getattr(ctx.command, "qualified_name", "unknown")
            try:
                await channel.send(
                    f"🔴 **【系統報錯】**\n出錯頻道：<#{ctx.channel.id}>\n出錯指令：`!{cmd_name}`\n"
                    f"```python\n{error_msg[:1900]}\n```"
                )
            except discord.HTTPException as e:
                logger.error(f"Failed to send war room error report: {e}")

    clean_time = datetime.time(hour=4, minute=0, tzinfo=TAIPEI)
    health_time = datetime.time(hour=9, minute=0, tzinfo=TAIPEI)

    async def _gather_health_stats(self) -> dict:
        db = read_db(self.bot)
        stats: dict = {
            "exp_rows": 0,
            "transfer_rows": 0,
            "last_snapshot": None,
            "server_count_last": 0,
            "missing_indexes": [],
            "denorm_total": 0,
            "denorm_filled": 0,
        }
        async with db.execute("SELECT COUNT(*) FROM exp_history") as cursor:
            row = await cursor.fetchone()
            stats["exp_rows"] = row[0] if row else 0
        async with db.execute(
            "SELECT COUNT(*) FROM transfer_alerts_log"
        ) as cursor:
            row = await cursor.fetchone()
            stats["transfer_rows"] = row[0] if row else 0
        async with db.execute(
            """
            SELECT record_time, COUNT(DISTINCT server_name)
            FROM exp_history
            GROUP BY record_time
            ORDER BY record_time DESC
            LIMIT 1
            """
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                stats["last_snapshot"] = row[0]
                stats["server_count_last"] = row[1]
        try:
            stats["missing_indexes"] = await list_missing_search_indexes_async(db)
        except sqlite3.DatabaseError as e:
            logger.warning(f"list missing indexes failed: {e}")
        try:
            total, filled = await denorm_coverage_stats(db)
            stats["denorm_total"] = total
            stats["denorm_filled"] = filled
        except sqlite3.DatabaseError as e:
            logger.warning(f"denorm coverage failed: {e}")
        return stats

    async def _execute_delete_with_busy_retry(self, sql: str, params: tuple):
        """database is locked 時短重試（避免卡住 event loop 用 asyncio.sleep）。"""
        last: BaseException | None = None
        for attempt in range(1, 9):
            try:
                cursor = await self.bot.db.execute(sql, params)
                return cursor
            except sqlite3.OperationalError as e:
                last = e
                msg = str(e).lower()
                if "locked" not in msg and "busy" not in msg:
                    raise
                logger.warning(
                    "線上清庫忙碌（%s），重試 %s/8…", e, attempt
                )
                await asyncio.sleep(min(2.0 * attempt, 10.0))
        assert last is not None
        raise last

    async def _delete_exp_history_in_batches(
        self, sql: str, params: tuple, *, label: str
    ) -> int:
        """分批 DELETE + commit；每數批 WAL checkpoint。"""
        total = 0
        batch_no = 0
        while True:
            cursor = await self._execute_delete_with_busy_retry(sql, params)
            n = cursor.rowcount or 0
            await cursor.close()
            await self.bot.db.commit()
            total += n
            batch_no += 1
            if batch_no % ONLINE_CHECKPOINT_EVERY_BATCHES == 0:
                try:
                    await self.bot.db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except sqlite3.DatabaseError as e:
                    logger.warning("%s checkpoint 略過: %s", label, e)
            if n < ONLINE_DELETE_BATCH_SIZE:
                break
        if total:
            logger.info("線上清庫 %s 刪除 %s 筆（%s 批）", label, total, batch_no)
        return total

    @tasks.loop(time=clean_time)
    async def db_cleanup_task(self):
        """每天凌晨 4 點：尋人導向保留（近 N 天 ∪ 轉移窗）外分批刪除；不過期 VACUUM。"""
        try:
            retention_cutoff = search_retention_cutoff(
                recent_days=DEFAULT_RECENT_DAYS,
                pad_days=DEFAULT_TRANSFER_PAD_DAYS,
                max_transfer_windows=DEFAULT_MAX_TRANSFER_WINDOWS,
            )
            keep_ranges = build_search_keep_ranges(
                recent_days=DEFAULT_RECENT_DAYS,
                pad_days=DEFAULT_TRANSFER_PAD_DAYS,
                max_transfer_windows=DEFAULT_MAX_TRANSFER_WINDOWS,
            )
            outside_sql, outside_params = exp_history_outside_keep_batch_sql(
                keep_ranges, batch_size=ONLINE_DELETE_BATCH_SIZE
            )
            deleted_exp = await self._delete_exp_history_in_batches(
                outside_sql, outside_params, label="保留窗外"
            )

            deleted_middle = 0
            thin_ranges = build_transfer_thin_ranges(
                max_transfer_windows=DEFAULT_MAX_TRANSFER_WINDOWS,
                pad_days=DEFAULT_TRANSFER_PAD_DAYS,
            )
            thin_ranges.extend(
                build_bridge_thin_ranges(
                    recent_days=DEFAULT_RECENT_DAYS,
                    max_transfer_windows=DEFAULT_MAX_TRANSFER_WINDOWS,
                    pad_days=DEFAULT_TRANSFER_PAD_DAYS,
                )
            )
            thin_ranges.sort(key=lambda item: item[0])
            for i, (start, end) in enumerate(thin_ranges, 1):
                thin_sql, thin_params = exp_history_transfer_middle_batch_sql(
                    start, end, batch_size=ONLINE_DELETE_BATCH_SIZE
                )
                deleted_middle += await self._delete_exp_history_in_batches(
                    thin_sql, thin_params, label=f"轉移窗中間#{i}"
                )

            pruned_profiles = 0
            rebuilt_profiles = 0
            backfilled = 0
            profile_touch = deleted_exp + deleted_middle
            if profile_touch > 0:
                # 輕量：清掉已無歷史的履歷；大刪除量才全量 rebuild
                pruned_profiles = await prune_orphaned_player_profiles(self.bot.db)
                if profile_touch >= ONLINE_FULL_REBUILD_EXP_DELETED_THRESHOLD:
                    rebuilt_profiles = await rebuild_player_profiles(self.bot.db)
                else:
                    backfilled = await backfill_player_profile_denorm(
                        self.bot.db, batch_limit=1000
                    )

            async with self.bot.db.execute(
                """
                DELETE FROM transfer_alerts_log
                WHERE alert_time < ?
                """,
                (transfer_alert_retention_cutoff(),),
            ) as cursor:
                deleted_transfer = cursor.rowcount or 0

            cutoff_date_only = (
                retention_cutoff[:10] if len(retention_cutoff) >= 10 else today_taipei_str()
            )
            async with self.bot.db.execute(
                PRUNE_DEDUPE_SQL,
                (
                    overspeed_prune_bound(retention_cutoff),
                    boss_reminder_prune_bound(cutoff_date_only),
                ),
            ) as cursor:
                deleted_settings = cursor.rowcount or 0

            async with self.bot.db.execute(
                PRUNE_ALERT_DEDUPE_SQL, (retention_cutoff,)
            ) as cursor:
                deleted_alert_dedupe = cursor.rowcount or 0

            await self.bot.db.commit()
            try:
                await self.bot.db.execute("PRAGMA optimize")
            except sqlite3.DatabaseError as e:
                logger.warning(f"PRAGMA optimize 略過: {e}")

            log_channel = await resolve_bot_channel(
                self.bot, self.log_channel_id, label="war room log channel"
            )
            if log_channel and (
                deleted_exp > 0
                or deleted_middle > 0
                or deleted_transfer > 0
                or deleted_settings > 0
                or deleted_alert_dedupe > 0
                or rebuilt_profiles > 0
                or pruned_profiles > 0
                or backfilled > 0
            ):
                vacuum_hint = ""
                if profile_touch >= 5000:
                    vacuum_hint = (
                        "\n💡 刪除量較大，建議停 bot 後執行 `python cleanup_db.py` 釋放磁碟。"
                    )
                profile_note = ""
                if rebuilt_profiles > 0:
                    profile_note = f"、`player_profile` 全量重建 {rebuilt_profiles:,} 筆"
                elif pruned_profiles or backfilled:
                    profile_note = (
                        f"、履歷 prune {pruned_profiles}／backfill {backfilled}"
                    )
                middle_note = (
                    f"（窗外 {deleted_exp}、轉移窗中間 {deleted_middle}）"
                    if deleted_middle
                    else ""
                )
                await log_channel.send(
                    f"🧹 **【資料庫維護】** 尋人保留窗外清理 "
                    f"`exp_history` {deleted_exp + deleted_middle} 筆{middle_note}、"
                    f"`transfer_alerts_log` {deleted_transfer} 筆、"
                    f"`bot_settings` 去重 key {deleted_settings} 筆、"
                    f"`alert_dedupe` {deleted_alert_dedupe} 筆"
                    f"{profile_note}。"
                    f"（VACUUM 請離線執行 `cleanup_db.py`）{vacuum_hint}"
                )

        except sqlite3.DatabaseError as e:
            try:
                await self.bot.db.rollback()
            except sqlite3.DatabaseError:
                pass
            log_channel = await resolve_bot_channel(
                self.bot, self.log_channel_id, label="war room log channel"
            )
            if log_channel:
                await log_channel.send(f"⚠️ **【資料庫清理異常】**\n```python\n{e}\n```")
            logger.error(f"DB cleanup failed: {e}", exc_info=True)

    @db_cleanup_task.before_loop
    async def before_db_cleanup_task(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=health_time)
    async def health_summary_task(self):
        """每週日發送資料庫／掃描健康摘要。"""
        now = now_taipei()
        if now.weekday() != 6:  # Sunday
            return
        if not self.log_channel_id:
            return
        channel = await resolve_bot_channel(
            self.bot, self.log_channel_id, label="war room log channel"
        )
        if not channel:
            return
        try:
            stats = await self._gather_health_stats()
            last = stats["last_snapshot"] or "尚無"
            vacuum_note = ""
            missing = stats.get("missing_indexes") or []
            if missing:
                vacuum_note = (
                    f"\n⚠️ 缺少索引：`{'`, `'.join(missing)}`；"
                    "請離線執行 `python cleanup_db.py --build-indexes`。"
                )
            elif stats["exp_rows"] > 50_000:
                vacuum_note = (
                    "\n💡 `exp_history` 超過 5 萬筆；若尋人變慢，"
                    "請離線執行 `python cleanup_db.py --for-search`。"
                )
            denorm_note = ""
            dt, df = stats.get("denorm_total", 0), stats.get("denorm_filled", 0)
            if dt > 0 and df < dt:
                denorm_note = (
                    f"\n⚠️ denorm 覆蓋 {df:,}/{dt:,}；"
                    "可用 `!重建履歷` 增量或離線 `--for-search`。"
                )
            calendar_note = ""
            for line in transfer_calendar_health_notes():
                calendar_note += f"\n{line}"
            await channel.send(
                f"📊 **【每週健康摘要】**\n"
                f"> `exp_history`：{stats['exp_rows']:,} 筆\n"
                f"> `transfer_alerts_log`：{stats['transfer_rows']:,} 筆\n"
                f"> 最近快照：`{last}`（{stats['server_count_last']} 服）"
                f"{vacuum_note}{denorm_note}{calendar_note}"
            )
        except sqlite3.DatabaseError as e:
            logger.error(f"Health summary failed: {e}", exc_info=True)
            try:
                await channel.send(f"⚠️ **【健康摘要失敗】**\n```python\n{e}\n```")
            except discord.HTTPException:
                pass

    @health_summary_task.before_loop
    async def before_health_summary_task(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(WarRoom(bot))
