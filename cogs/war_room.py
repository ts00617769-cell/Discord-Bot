import discord
from discord.ext import commands, tasks
import traceback
import datetime
import logging
from .error_handler import parse_env_channel_id

logger = logging.getLogger(__name__)

class WarRoom(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.log_channel_id = parse_env_channel_id("WAR_ROOM_CHANNEL_ID", 0)
        self._ready_announced = False

    async def cog_load(self):
        if not self.db_cleanup_task.is_running():
            self.db_cleanup_task.start()

    def cog_unload(self):
        if self.db_cleanup_task.is_running():
            self.db_cleanup_task.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info('[模組載入] WarRoom (戰情室監控與排程) 運作中')
        if self._ready_announced or not self.log_channel_id:
            return
        self._ready_announced = True
        channel = self.bot.get_channel(self.log_channel_id)
        if channel:
            try:
                await channel.send("🟢 **【系統廣播】** 戰情雷達已重新啟動，後勤監視與自動排程上線。")
            except Exception as e:
                logger.error(f"Failed to send war room ready message: {e}")

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if isinstance(error, (commands.CommandNotFound, commands.NotOwner)):
            return
        if isinstance(error, commands.CheckFailure) and str(error) == "duplicate_invoke":
            return
        # CheckFailure / MissingRequiredArgument 等一般使用錯誤不進戰情室洗版
        if isinstance(error, (commands.UserInputError, commands.CheckFailure, commands.CommandOnCooldown)):
            return

        if not self.log_channel_id:
            return

        channel = self.bot.get_channel(self.log_channel_id)
        if channel:
            error_msg = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
            cmd_name = getattr(ctx.command, "qualified_name", "unknown")
            await channel.send(
                f"🔴 **【系統報錯】**\n出錯頻道：<#{ctx.channel.id}>\n出錯指令：`!{cmd_name}`\n"
                f"```python\n{error_msg[:1900]}\n```"
            )

    tz = datetime.timezone(datetime.timedelta(hours=8))
    clean_time = datetime.time(hour=4, minute=0, tzinfo=tz)

    @tasks.loop(time=clean_time)
    async def db_cleanup_task(self):
        """每天凌晨 4 點自動刪除 180 天前的過期資料"""
        try:
            async with self.bot.db.execute("""
                DELETE FROM exp_history
                WHERE record_time < datetime('now', 'localtime', '-180 days')
            """) as cursor:
                deleted_rows = cursor.rowcount

            await self.bot.db.commit()

            log_channel = self.bot.get_channel(self.log_channel_id)
            if log_channel and deleted_rows > 0:
                await log_channel.send(
                    f"🧹 **【資料庫維護】** 系統已於凌晨自動清理 `{deleted_rows}` 筆 180 天前的過期紀錄，釋放儲存空間。"
                )

        except Exception as e:
            log_channel = self.bot.get_channel(self.log_channel_id)
            if log_channel:
                await log_channel.send(f"⚠️ **【資料庫清理異常】**\n```python\n{e}\n```")

    @db_cleanup_task.before_loop
    async def before_db_cleanup_task(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(WarRoom(bot))
