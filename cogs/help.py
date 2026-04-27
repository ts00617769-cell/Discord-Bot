import discord
from discord.ext import commands

class HelpCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="指令", help="顯示所有可用的機器人指令操作手冊")
    async def show_help(self, ctx):
        # 建立一個具有科技感的 Embed 卡片
        embed = discord.Embed(
            title="🤖 波拉西亞戰記 - 旅團戰情終端機", 
            description="以下是系統支援的核心指令操作手冊：\n*(註：戰情數據掃描功能僅限特定頻道使用)*", 
            color=0x2E86C1
        )

        # ⚔️ 1. 戰情與數據掃描系統
        intel_desc = (
            "**`!排名 [數量] [伺服器]`**\n"
            "跨服彙整玩家經驗值排行。例：`!排名 全服` 或 `!排名 25 萊涅01`\n\n"
            "**`!稅收 [數量] [伺服器]`**\n"
            "掃描據點鑽石/紅寶石稅收。例：`!稅收 全服` 或 `!稅收 20 萊涅01`\n\n"
            "**`!聯賽 [季] [回合] [級別]`**\n"
            "查詢宇宙聯賽陣營與公會貢獻。例：`!聯賽 3 3 1`"
        )
        embed.add_field(name="⚔️ 戰情與數據掃描", value=intel_desc, inline=False)

        # 🏦 2. 旅團金庫管理系統
        bank_desc = (
            "**`!記帳 [金額] [說明]`**\n"
            "記錄旅團收入或支出。例：`!記帳 20000 宇宙鑽石` 或 `!記帳 -10000 出席分配`\n\n"
            "**`!金庫`**\n"
            "查看旅團目前總結餘與最近 5 筆收支明細。"
        )
        embed.add_field(name="🏦 旅團金庫管理", value=bank_desc, inline=False)

        # 🔮 3. 遊戲輔助與工具
        tools_desc = (
            "**`!時空`**\n查詢當日「時空縫隙首領」召喚時間表。\n\n"
            "**`!抽卡 [次數]`**\n模擬遊戲抽卡(1~1000抽)，結果將私訊傳送。例：`!抽卡 11`\n\n"
            "**`!抽 [最大數字]`**\n隨機抽取 1~N 的數字，適合分配戰利品。例：`!抽 100`\n\n"
            "**`!鍊成 [階級]`**\n模擬裝備四合一鍊成(需連過四柱)。例：`!鍊成 英雄`\n\n"
            "**`!塔羅`**\n抽取當日專屬大阿爾克那塔羅牌，預測遊戲運勢。\n\n"
            "**`!星座 [星座名稱]`**\n查詢真實每日星座運勢。例：`!星座 天蠍座`"
        )
        embed.add_field(name="🔮 遊戲輔助與工具", value=tools_desc, inline=False)

        # 🧠 4. 互動心理測驗系統
        quiz_desc = (
            "**`!測驗`**\n隨機發送一則普通心理測驗，點擊按鈕立即顯示結果。\n\n"
            "**`!定時測驗`**\n強制手動觸發今日「盲投心理測驗」題目。\n\n"
            "**`!測試開獎`**\n強制提早結算當前盲投測驗並公布結果。"
        )
        embed.add_field(name="🧠 互動心理測驗", value=quiz_desc, inline=False)

        embed.set_footer(text="輸入指令時請注意空格。祝武運昌隆！")

        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(HelpCog(bot))