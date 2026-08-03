import discord
from discord.ext import commands

from services.error_handler import allowed_channel


class HelpCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="機密指令", help="戰情室專屬的進階指令手冊")
    @allowed_channel()
    async def secret_help(self, ctx):
        embed = discord.Embed(
            title="🚨 戰情室專屬：進階雷達操作手冊",
            description="以下指令會調用系統深層資料庫，僅限戰情室頻道。",
            color=0xE74C3C,
        )
        embed.add_field(
            name="🏆 即時排名",
            value="`!排名` / `/排名` `[數量] [伺服器] [職業]`\n例：`!排名 50 萊涅01` 或 `!排名 幻影劍士`",
            inline=False,
        )
        embed.add_field(
            name="🏎️ 練功測速",
            value="`!測速` / `/測速` `[數量] [伺服器]`\n例：`!測速 30 全服`",
            inline=False,
        )
        embed.add_field(
            name="🚨 飆車警報",
            value=(
                "`!警報 開 [數量] [伺服器] [旅團名稱]`\n"
                "例：`!警報 開 50 萊涅01 守護者` 或 `!警報 關`"
            ),
            inline=False,
        )
        embed.add_field(
            name="🕵️ 尋人",
            value="`!尋人` / `/尋人` `[玩家名稱] [伺服器?]`\n例：`!尋人 驕傲o`、`!尋人 驕傲o 萊涅01`",
            inline=False,
        )
        embed.add_field(
            name="🛫 轉服掃描",
            value="`!轉服掃描`（別名：`!移民清單`、`!抓包`）",
            inline=False,
        )
        embed.add_field(
            name="🧪 測試轉移警報",
            value="`!測試轉移警報`（限轉移警報頻道或指令白名單）",
            inline=False,
        )
        embed.add_field(
            name="🛠️ 重建履歷",
            value="`!重建履歷 [增量|全量]`（擁有者；補建 player_profile denorm）",
            inline=False,
        )
        embed.set_footer(text="機密層級：最高 | 資料來源：warsofprasia.beanfun.com")
        await ctx.send(embed=embed)

    @commands.command(name="指令", help="顯示所有可用的機器人指令操作手冊")
    async def show_help(self, ctx):
        embed = discord.Embed(
            title="🤖 波拉西亞戰記 - 旅團戰情終端機",
            description=(
                "資料來源：官方即時戰況站 "
                "[warsofprasia.beanfun.com](https://warsofprasia.beanfun.com/)\n"
                "*(測速 / 排名 / 尋人等深層指令請輸入 `!機密指令`，限戰情室頻道)*"
            ),
            color=0x2E86C1,
        )

        intel_desc = (
            "**`!討伐排名`**（公開）\n"
            "各服總榜+職業榜合併後依討伐等級重排 TOP 100。\n\n"
            "**`!時空王 [伺服器]`**（公開）\n"
            "查詢時間隙縫首領擊殺與 MVP 戰報。例：`!時空王 萊涅01`\n\n"
            "**`!稅收 [數量] [伺服器]`**（公開）\n"
            "掃描據點鑽石/紅寶石稅收。例：`!稅收 20 萊涅01`"
        )
        embed.add_field(name="⚔️ 戰情與數據掃描（公開）", value=intel_desc, inline=False)

        tools_desc = (
            "**`!時空`**\n查詢當日時空縫隙首領召喚時間表。\n\n"
            "**`!鍊成 [階級]`**\n模擬裝備四合一鍊成。例：`!鍊成 英雄`\n\n"
            "**`!塔羅`**\n抽取當日專屬大阿爾克那塔羅牌。\n\n"
            "**`!星座 [星座名稱]`**\n查詢真實每日星座運勢。例：`!星座 天蠍座`\n\n"
            "**`!求籤 [你的問題]`**\n向菩薩請示。例：`!求籤 今年運勢`"
        )
        embed.add_field(name="🔮 遊戲輔助與工具", value=tools_desc, inline=False)

        quiz_desc = (
            "**`!測驗`**\n隨機心理測驗（點擊即開獎）。\n\n"
            "**`!定時測驗` / `!測試開獎`**\n（擁有者）盲投測驗手動發布與提早開獎。"
        )
        embed.add_field(name="🧠 互動心理測驗", value=quiz_desc, inline=False)

        embed.set_footer(text="輸入指令時請注意空格。祝武運昌隆！")
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(HelpCog(bot))
