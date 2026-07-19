import discord
from discord.ext import commands, tasks
import traceback
import datetime
import logging
import sqlite3
from services.error_handler import parse_env_channel_id

from services.timeutil import TAIPEI, now_taipei, taipei_cutoff_str, today_taipei_str
from services.settings_prune import (
    PRUNE_DEDUPE_SQL,
    boss_reminder_prune_bound,
    overspeed_prune_bound,
)

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
        channel = self.bot.get_channel(self.log_channel_id)
        if channel:
            try:
                await channel.send(
                    "🟢 **【系統廣播】** 戰情雷達已重新啟動，後勤監視與自動排程上線。"
                )
            except discord.HTTPException as e:
                logger.error(f"Failed to send war room ready message: {e}")

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CheckFailure) and str(error) == "duplicate_invoke":
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

        channel = self.bot.get_channel(self.log_channel_id)
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
        stats = {
            "exp_rows": 0,
            "transfer_rows": 0,
            "last_snapshot": None,
            "server_count_last": 0,
        }
        async with self.bot.db.execute("SELECT COUNT(*) FROM exp_history") as cursor:
            stats["exp_rows"] = (await cursor.fetchone())[0]
        async with self.bot.db.execute(
            "SELECT COUNT(*) FROM transfer_alerts_log"
        ) as cursor:
            stats["transfer_rows"] = (await cursor.fetchone())[0]
        async with self.bot.db.execute(
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
        return stats

    @tasks.loop(time=clean_time)
    async def db_cleanup_task(self):
        """每天凌晨 4 點清理超過 60 天的資料（不在線上 VACUUM，避免鎖庫）。"""
        try:
            cutoff = taipei_cutoff_str(60)
            async with self.bot.db.execute(
                """
                DELETE FROM exp_history
                WHERE record_time < ?
                """,
                (cutoff,),
            ) as cursor:
                deleted_exp = cursor.rowcount or 0

            async with self.bot.db.execute(
                """
                DELETE FROM transfer_alerts_log
                WHERE alert_time < ?
                """,
                (cutoff,),
            ) as cursor:
                deleted_transfer = cursor.rowcount or 0

            # 超速／Boss 去重 key：依字串前綴與截止時間比較清理
            cutoff_date_only = cutoff[:10] if len(cutoff) >= 10 else today_taipei_str()
            async with self.bot.db.execute(
                PRUNE_DEDUPE_SQL,
                (
                    overspeed_prune_bound(cutoff),
                    boss_reminder_prune_bound(cutoff_date_only),
                ),
            ) as cursor:
                deleted_settings = cursor.rowcount or 0

            await self.bot.db.commit()
            try:
                await self.bot.db.execute("PRAGMA optimize")
            except sqlite3.DatabaseError as e:
                logger.warning(f"PRAGMA optimize 略過: {e}")

            log_channel = self.bot.get_channel(self.log_channel_id)
            if log_channel and (
                deleted_exp > 0 or deleted_transfer > 0 or deleted_settings > 0
            ):
                vacuum_hint = ""
                if deleted_exp >= 5000:
                    vacuum_hint = (
                        "\n💡 刪除量較大，建議停 bot 後執行 `python cleanup_db.py` 釋放磁碟。"
                    )
                await log_channel.send(
                    f"🧹 **【資料庫維護】** 清理 `exp_history` {deleted_exp} 筆、"
                    f"`transfer_alerts_log` {deleted_transfer} 筆、"
                    f"`bot_settings` 去重 key {deleted_settings} 筆。"
                    f"（VACUUM 請離線執行 `cleanup_db.py`）{vacuum_hint}"
                )

        except sqlite3.DatabaseError as e:
            log_channel = self.bot.get_channel(self.log_channel_id)
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
        channel = self.bot.get_channel(self.log_channel_id)
        if not channel:
            return
        try:
            stats = await self._gather_health_stats()
            last = stats["last_snapshot"] or "尚無"
            vacuum_note = ""
            if stats["exp_rows"] > 50_000:
                vacuum_note = (
                    "\n⚠️ `exp_history` 超過 5 萬筆；若啟動略過索引，"
                    "請離線執行 `python cleanup_db.py`。"
                )
            await channel.send(
                f"📊 **【每週健康摘要】**\n"
                f"> `exp_history`：{stats['exp_rows']:,} 筆\n"
                f"> `transfer_alerts_log`：{stats['transfer_rows']:,} 筆\n"
                f"> 最近快照：`{last}`（{stats['server_count_last']} 服）"
                f"{vacuum_note}"
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
