import discord
from discord.ext import commands, tasks
import datetime
import pytz
from game_data import GAP_BOSS_SCHEDULE, WEEKDAY_NAMES

class BossSchedule(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.REMINDER_CHANNEL_ID = 1477964998818140326
        self.auto_boss_reminder.start()

    def cog_unload(self):
        self.auto_boss_reminder.cancel()

    @tasks.loop(minutes=1)
    async def auto_boss_reminder(self):
        # ... (原本自動提醒的程式碼，記得 bot.get_channel 改成 self.bot.get_channel)
        tz = pytz.timezone('Asia/Taipei')
        now = datetime.datetime.now(tz)
        ten_mins_later = now + datetime.timedelta(minutes=10)
        target_hour = ten_mins_later.hour
        target_minute = ten_mins_later.minute
        weekday = ten_mins_later.weekday()

        if target_minute == 0 and target_hour in GAP_BOSS_SCHEDULE.get(weekday, []):
            channel = bot.get_channel(REMINDER_CHANNEL_ID)
            if channel:
                time_str = "點、".join(map(str, GAP_BOSS_SCHEDULE[weekday])) + "點"
                embed = discord.Embed(
                    title="🕒 時空縫隙首領召喚提醒",
                    description=f"**10 分鐘後** 將開始召喚首領！\n\n今天召喚時段\n✅ **{time_str}**",
                    color=discord.Color.red()
                )
                await channel.send(content="@everyone", embed=embed)

    @commands.command(name="時空", help="顯示今天的時空縫隙召喚時間表。")
    async def gap_boss_info(self, ctx):
        # ... (原本時空查詢的程式碼)
        tz = pytz.timezone('Asia/Taipei')
        now = datetime.datetime.now(tz)
        today_index = now.weekday() 
        today_name = WEEKDAY_NAMES[today_index]
        times = GAP_BOSS_SCHEDULE.get(today_index, [])
        
        if not times:
            await ctx.send(f"📅 今天是 {today_name}，目前沒有設定召喚。")
            return

        time_str = "點、".join(map(str, times)) + "點"
        embed = discord.Embed(
            title=f"🕒 時空縫隙首領召喚時間表",
            description=f"今天是 **{today_name}**",
            color=discord.Color.purple()
        )
        embed.add_field(name="今天召喚時段", value=f"✅ **{time_str}**", inline=False)
        embed.set_footer(text=f"伺服器目前時間：{now.strftime('%H:%M')}")
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(BossSchedule(bot))