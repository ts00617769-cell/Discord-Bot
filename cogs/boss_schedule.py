import datetime
import logging
import sqlite3

import discord
from discord.ext import commands, tasks

from game_data import GAP_BOSS_SCHEDULE, WEEKDAY_NAMES
from services.alert_dedupe import (
    KIND_BOSS_REMINDER,
    alert_already_sent,
    release_alert_claim,
    try_claim_alert,
)
from services.error_handler import parse_env_channel_id, resolve_bot_channel
from services.timeutil import now_naive_taipei, now_taipei, today_taipei_str

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

    async def _already_reminded(self, key: str) -> bool:
        return await alert_already_sent(self.bot.db, KIND_BOSS_REMINDER, key)

    async def _try_claim_reminder(self, key: str) -> bool:
        """先寫入 dedupe（INSERT OR IGNORE）；成功取得 claim 才回 True。"""
        now = now_naive_taipei().strftime("%Y-%m-%d %H:%M:%S")
        return await try_claim_alert(
            self.bot.db, KIND_BOSS_REMINDER, key, created_at=now
        )

    async def _release_reminder_claim(self, key: str) -> None:
        await release_alert_claim(self.bot.db, KIND_BOSS_REMINDER, key)

    @tasks.loop(minutes=1)
    async def auto_boss_reminder(self):
        if not self.REMINDER_CHANNEL_ID:
            return

        now = now_taipei()
        ten_mins_later = now + datetime.timedelta(minutes=10)
        target_hour = ten_mins_later.hour
        target_minute = ten_mins_later.minute
        weekday = ten_mins_later.weekday()

        if target_minute != 0 or target_hour not in GAP_BOSS_SCHEDULE.get(weekday, []):
            return

        # 同一提醒窗只發一次（重啟也不會重複 @everyone）
        dedupe_key = f"boss_reminder:{today_taipei_str()}:{target_hour:02d}"
        try:
            if await self._already_reminded(dedupe_key):
                return
            if not await self._try_claim_reminder(dedupe_key):
                return
        except sqlite3.DatabaseError as e:
            logger.error(f"Boss reminder dedupe check failed: {e}")
            return

        channel = await resolve_bot_channel(
            self.bot,
            self.REMINDER_CHANNEL_ID,
            label="boss reminder channel",
        )
        if not channel:
            logger.warning(f"Boss reminder channel {self.REMINDER_CHANNEL_ID} not found")
            try:
                await self._release_reminder_claim(dedupe_key)
            except sqlite3.DatabaseError:
                logger.critical("Boss reminder 頻道不存在且無法釋放 claim", exc_info=True)
            return
        try:
            time_str = "點、".join(map(str, GAP_BOSS_SCHEDULE[weekday])) + "點"
            embed = discord.Embed(
                title="🕒 時空縫隙首領召喚提醒",
                description=(
                    f"**10 分鐘後** 將開始召喚首領！\n\n"
                    f"今天召喚時段\n✅ **{time_str}**"
                ),
                color=discord.Color.red(),
            )
            await channel.send(content="@everyone", embed=embed)
        except discord.HTTPException as e:
            logger.error(f"Failed to send boss reminder: {e}")
            try:
                await self._release_reminder_claim(dedupe_key)
            except sqlite3.DatabaseError:
                logger.critical("Boss reminder 發送失敗且無法釋放 claim", exc_info=True)

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
