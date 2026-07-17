import discord
from discord.ext import commands, tasks
import traceback
import datetime
import logging
import sqlite3
from .error_handler import parse_env_channel_id

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

    def cog_unload(self):
        if self.db_cleanup_task.is_running():
            self.db_cleanup_task.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("[模組載入] WarRoom (戰情室監控與排程) 運作中")
        if self._ready_announced or not self.log_channel_id:
            return
        self._ready_announced = True
        channel = self.bot.get_channel(self.log_channel_id)
        if channel:
            try:
                await channel.send("🟢 **【系統廣播】** 戰情雷達已重新啟動，後勤監視與自動排程上線。")
            except discord.HTTPException as e:
                logger.error(f"Failed to send war room ready message: {e}")

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CheckFailure) and str(error) == "duplicate_invoke":
            return

        # 冷卻／併發／參數錯誤：回覆使用者，避免「下指令沒反應」
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
                await ctx.send("⏳ 相同指令尚在執行中，請稍候完成後再試。", delete_after=10)
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

    tz = datetime.timezone(datetime.timedelta(hours=8))
    clean_time = datetime.time(hour=4, minute=0, tzinfo=tz)

    @tasks.loop(time=clean_time)
    async def db_cleanup_task(self):
        """每天凌晨 4 點清理超過 60 天的資料（不在線上 VACUUM，避免鎖庫）。"""
        try:
            async with self.bot.db.execute(
                """
                DELETE FROM exp_history
                WHERE record_time < datetime('now', 'localtime', '-60 days')
                """
            ) as cursor:
                deleted_exp = cursor.rowcount or 0

            async with self.bot.db.execute(
                """
                DELETE FROM transfer_alerts_log
                WHERE alert_time < datetime('now', 'localtime', '-60 days')
                """
            ) as cursor:
                deleted_transfer = cursor.rowcount or 0

            await self.bot.db.commit()

            log_channel = self.bot.get_channel(self.log_channel_id)
            if log_channel and (deleted_exp > 0 or deleted_transfer > 0):
                await log_channel.send(
                    f"🧹 **【資料庫維護】** 清理 `exp_history` {deleted_exp} 筆、"
                    f"`transfer_alerts_log` {deleted_transfer} 筆。"
                    f"（VACUUM 請離線執行 `cleanup_db.py`）"
                )

        except sqlite3.DatabaseError as e:
            log_channel = self.bot.get_channel(self.log_channel_id)
            if log_channel:
                await log_channel.send(f"⚠️ **【資料庫清理異常】**\n```python\n{e}\n```")
            logger.error(f"DB cleanup failed: {e}", exc_info=True)

    @db_cleanup_task.before_loop
    async def before_db_cleanup_task(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(WarRoom(bot))
