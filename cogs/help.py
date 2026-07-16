import discord
from discord.ext import commands
from .error_handler import parse_env_channel_ids, is_allowed_command_channel

class HelpCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.allowed_channel_ids = parse_env_channel_ids(env_name="ALLOWED_COMMAND_CHANNELS")
        
    @commands.command(name="機密指令", help="戰情室專屬的進階指令手冊")
    async def secret_help(self, ctx):
        if not is_allowed_command_channel(ctx.channel.id, self.allowed_channel_ids):
            return

        # 只有在指定頻道才會發送這張機密卡片
        embed = discord.Embed(
            title="🚨 戰情室專屬：進階雷達操作手冊", 
            description="以下指令會調用系統深層資料庫，請小心使用。", 
            color=0xE74C3C # 警戒的紅色
        )

        embed.add_field(name="🏆 即時排名", value="`!排名 [數量] [伺服器]`\n例：`!排名 50 萊涅01`", inline=False)
        embed.add_field(name="📜 歷史排名 (支援職業與日期篩選)", value="`!歷史排名 [數量] [日期] [伺服器] [職業]`\n條件順序可隨意打亂。\n例：`!歷史 2026-05-08 萊涅04 太陽監視者`", inline=False)
        embed.add_field(name="🏎️ 練功測速", value="`!測速 [數量] [伺服器]`\n例：`!測速 30 全服`", inline=False)
        embed.add_field(name="🚨 飆車警報", value="`!警報 [開/關]`\n例：`!警報 開 50 萊涅01` 或 `!警報 關`", inline=False)
        embed.add_field(name="🕵️ 尋人", value="`!尋人 [玩家名稱]`\n利用經驗值特徵追蹤改名或轉服的玩家。\n例：`!尋人 驕傲o`", inline=False)
        embed.add_field(name="📝 尋人回報", value="`!尋人回報 [目前名稱] [前身1] [前身2]...`\n手動標記玩家前身身分 (支援一次輸入多個，或使用「清除」移除)。\n例：`!尋人回報 驕傲o 某某某 艾雲o`", inline=False)
        embed.add_field(name="🛫 轉服掃描", value="`!轉服掃描`\n全服掃描近期利用轉服空窗期改名或移動的玩家。", inline=False)
        
        embed.set_footer(text="機密層級：最高 | 系統：O(1) 戰情終端機")
        await ctx.send(embed=embed)

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
            "**`!討伐排名`**\n"
            "查詢全服前 100 名討伐等級排行。\n\n"
            "**`!時空王 [伺服器]`**\n"
            "查詢時間隙縫(交叉王)首領擊殺與MVP戰報。例：`!時空王 萊涅01`\n\n"
            "**`!稅收 [數量] [伺服器]`**\n"
            "掃描據點鑽石/紅寶石稅收。例：`!稅收 全服` 或 `!稅收 20 萊涅01`\n\n"
            "**`!聯賽 [季] [回合] [級別]`**\n"
            "查詢宇宙聯賽陣營與公會貢獻。例：`!聯賽 3 3 1`"
        )
        embed.add_field(name="⚔️ 戰情與數據掃描", value=intel_desc, inline=False)


        # 🔮 3. 遊戲輔助與工具
        tools_desc = (
            "**`!時空`**\n查詢當日「時空縫隙首領」召喚時間表。\n\n"
            "**`!鍊成 [階級]`**\n模擬裝備四合一鍊成(需連過四柱)。例：`!鍊成 英雄`\n\n"
            "**`!塔羅`**\n抽取當日專屬大阿爾克那塔羅牌，預測遊戲運勢。\n\n"
            "**`!星座 [星座名稱]`**\n查詢真實每日星座運勢。例：`!星座 天蠍座`\n\n"
            "**`!求籤 [你的問題]`**\n向菩薩請示。例：`!求籤 今年運勢`"
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