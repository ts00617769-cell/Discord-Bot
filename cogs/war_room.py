import asyncio
import datetime
import logging
import sqlite3

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
from services.discord_send import send_text_to_channels
from services.error_handler import (
    handle_command_error,
    parse_env_channel_id,
)
from services.game_event_windows import transfer_calendar_health_notes
from services.retention_cleanup import (
    build_retention_plan,
    outside_keep_batch,
    prune_secondary_async,
    thin_range_batches,
)
from services.retention_windows import (
    ONLINE_CHECKPOINT_EVERY_BATCHES,
    ONLINE_DELETE_BATCH_SIZE,
)
from services.search_cache import invalidate_player_search_cache
from services.timeutil import TAIPEI, now_taipei

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
        await send_text_to_channels(
            self.bot,
            [self.log_channel_id],
            "🟢 **【系統廣播】** 戰情雷達已重新啟動，後勤監視與自動排程上線。",
            label="war room log channel",
        )

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        await handle_command_error(
            ctx,
            error,
            log_channel_id=self.log_channel_id or None,
            bot=self.bot,
        )

    # 04:15：錯開整點／:10 快照寫入，降低 NAS database is locked
    clean_time = datetime.time(hour=4, minute=15, tzinfo=TAIPEI)
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
        # 優先用 sqlite_stat1 估計，避免大庫全表 COUNT 卡住週報
        async with db.execute(
            "SELECT SUM(stat) FROM sqlite_stat1 WHERE tbl = 'exp_history'"
        ) as cursor:
            row = await cursor.fetchone()
        if row and row[0] is not None:
            stats["exp_rows"] = int(row[0])
        else:
            async with db.execute("SELECT COUNT(*) FROM exp_history") as cursor:
                row = await cursor.fetchone()
                stats["exp_rows"] = row[0] if row else 0
        async with db.execute(
            "SELECT SUM(stat) FROM sqlite_stat1 WHERE tbl = 'transfer_alerts_log'"
        ) as cursor:
            row = await cursor.fetchone()
        if row and row[0] is not None:
            stats["transfer_rows"] = int(row[0])
        else:
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
            async with self.bot.db_write_lock:
                cursor = await self._execute_delete_with_busy_retry(sql, params)
                n = cursor.rowcount or 0
                await cursor.close()
                await self.bot.db.commit()
            total += n
            batch_no += 1
            if batch_no % ONLINE_CHECKPOINT_EVERY_BATCHES == 0:
                try:
                    async with self.bot.db_write_lock:
                        await self.bot.db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except sqlite3.DatabaseError as e:
                    logger.warning("%s checkpoint 略過: %s", label, e)
            if n < ONLINE_DELETE_BATCH_SIZE:
                break
            # 讓等待中的 10 分鐘快照優先取得寫入鎖，避免清庫長時間霸佔。
            await asyncio.sleep(0.05)
        if total:
            logger.info("線上清庫 %s 刪除 %s 筆（%s 批）", label, total, batch_no)
        return total

    @tasks.loop(time=clean_time)
    async def db_cleanup_task(self):
        """每天凌晨 4:15：尋人導向保留（近 N 天 ∪ 轉移窗）外分批刪除；不過期 VACUUM。"""
        try:
            plan = build_retention_plan()
            outside_sql, outside_params = outside_keep_batch(
                plan, batch_size=ONLINE_DELETE_BATCH_SIZE
            )
            deleted_exp = await self._delete_exp_history_in_batches(
                outside_sql, outside_params, label="保留窗外"
            )

            deleted_middle = 0
            for thin_sql, thin_params, label in thin_range_batches(
                plan, batch_size=ONLINE_DELETE_BATCH_SIZE
            ):
                deleted_middle += await self._delete_exp_history_in_batches(
                    thin_sql, thin_params, label=label
                )

            pruned_profiles = 0
            rebuilt_profiles = 0
            backfilled = 0
            profile_touch = deleted_exp + deleted_middle
            if profile_touch > 0:
                async with self.bot.db_write_lock:
                    pruned_profiles = await prune_orphaned_player_profiles(self.bot.db)
                    if profile_touch >= ONLINE_FULL_REBUILD_EXP_DELETED_THRESHOLD:
                        rebuilt_profiles = await rebuild_player_profiles(self.bot.db)
                    else:
                        backfilled = await backfill_player_profile_denorm(
                            self.bot.db, batch_limit=1000
                        )

            async with self.bot.db_write_lock:
                secondary = await prune_secondary_async(self.bot.db, plan)
            deleted_transfer = secondary.deleted_transfer
            deleted_settings = secondary.deleted_settings
            deleted_alert_dedupe = secondary.deleted_alert_dedupe
            deleted_cmd_dedupe = secondary.deleted_cmd_dedupe
            deleted_transfer_missing = secondary.deleted_transfer_missing

            invalidate_player_search_cache()
            try:
                async with self.bot.db_write_lock:
                    await self.bot.db.execute("PRAGMA optimize")
            except sqlite3.DatabaseError as e:
                logger.warning(f"PRAGMA optimize 略過: {e}")

            if self.log_channel_id and (
                deleted_exp > 0
                or deleted_middle > 0
                or deleted_transfer > 0
                or deleted_settings > 0
                or deleted_alert_dedupe > 0
                or deleted_cmd_dedupe > 0
                or deleted_transfer_missing > 0
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
                missing_note = (
                    f"、`transfer_missing` {deleted_transfer_missing} 筆"
                    if deleted_transfer_missing
                    else ""
                )
                middle_note = (
                    f"（窗外 {deleted_exp}、轉移窗中間 {deleted_middle}）"
                    if deleted_middle
                    else ""
                )
                await send_text_to_channels(
                    self.bot,
                    [self.log_channel_id],
                    f"🧹 **【資料庫維護】** 尋人保留窗外清理 "
                    f"`exp_history` {deleted_exp + deleted_middle} 筆{middle_note}、"
                    f"`transfer_alerts_log` {deleted_transfer} 筆、"
                    f"`bot_settings` 去重 key {deleted_settings} 筆、"
                    f"`alert_dedupe` {deleted_alert_dedupe} 筆、"
                    f"`cmd_dedupe` {deleted_cmd_dedupe} 筆"
                    f"{missing_note}{profile_note}。"
                    f"（VACUUM 請離線執行 `cleanup_db.py`）{vacuum_hint}",
                    label="war room log channel",
                )

        except sqlite3.DatabaseError as e:
            try:
                await self.bot.db.rollback()
            except sqlite3.DatabaseError:
                pass
            if self.log_channel_id:
                await send_text_to_channels(
                    self.bot,
                    [self.log_channel_id],
                    f"⚠️ **【資料庫清理異常】**\n```python\n{e}\n```",
                    label="war room log channel",
                )
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
            await send_text_to_channels(
                self.bot,
                [self.log_channel_id],
                f"📊 **【每週健康摘要】**\n"
                f"> `exp_history`：{stats['exp_rows']:,} 筆\n"
                f"> `transfer_alerts_log`：{stats['transfer_rows']:,} 筆\n"
                f"> 最近快照：`{last}`（{stats['server_count_last']} 服）"
                f"{vacuum_note}{denorm_note}{calendar_note}",
                label="war room log channel",
            )
        except sqlite3.DatabaseError as e:
            logger.error(f"Health summary failed: {e}", exc_info=True)
            await send_text_to_channels(
                self.bot,
                [self.log_channel_id],
                f"⚠️ **【健康摘要失敗】**\n```python\n{e}\n```",
                label="war room log channel",
            )

    @health_summary_task.before_loop
    async def before_health_summary_task(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(WarRoom(bot))
