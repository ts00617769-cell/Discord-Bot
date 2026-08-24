import logging

import discord
from discord.ext import commands, tasks

from game_data import GAP_BOSS_SCHEDULE, WEEKDAY_NAMES
from services.boss_reminder import run_boss_reminder
from services.db_lock import bot_write_lock
from services.error_handler import parse_env_channel_id
from services.timeutil import now_taipei

logger = logging.getLogger(__name__)


class BossSchedule(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def REMINDER_CHANNEL_ID(self) -> int:
        return parse_env_channel_id("BOSS_REMINDER_CHANNEL_ID", 0)

    async def cog_load(self):
        if not self.auto_boss_reminder.is_running():
            self.auto_boss_reminder.start()

    def cog_unload(self):
        if self.auto_boss_reminder.is_running():
            self.auto_boss_reminder.cancel()

    @tasks.loop(minutes=1)
    async def auto_boss_reminder(self):
        await run_boss_reminder(
            self.bot,
            write_db=self.bot.db,
            channel_id=self.REMINDER_CHANNEL_ID,
            write_lock=bot_write_lock(self.bot),
        )

    @auto_boss_reminder.before_loop
    async def before_auto_boss_reminder(self):
        await self.bot.wait_until_ready()

    @commands.command(name="時空", help="顯示今天的時空縫隙召喚時間表。")
    async def gap_boss_info(self, ctx):
        now = now_taipei()
        today_index = now.weekday()
        today_name = WEEKDAY_NAMES[today_index]
        times = GAP_BOSS_SCHEDULE.get(today_index, [])

        if not times:
            await ctx.send(f"📅 今天是 {today_name}，目前沒有設定召喚。")
            return

        time_str = "點、".join(map(str, times)) + "點"
        embed = discord.Embed(
            title="🕒 時空縫隙首領召喚時間表",
            description=f"今天是 **{today_name}**",
            color=discord.Color.purple(),
        )
        embed.add_field(name="今天召喚時段", value=f"✅ **{time_str}**", inline=False)
        embed.set_footer(text=f"伺服器目前時間：{now.strftime('%H:%M')}")
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(BossSchedule(bot))
