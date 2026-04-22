import discord
from discord.ext import commands

# 這裡定義一個「測驗系統」的模組類別
class QuizSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot  # 讓這張擴充卡可以跟主機板 (bot) 溝通

    # 注意！在模組裡面，裝飾器要改成 @commands.command()
    # 並且函式括號裡第一個參數一定要放 self
    @commands.command()
    async def 測驗(self, ctx):
        await ctx.send("🤖 模組化測驗系統連線成功！這是一道來自 cogs/quiz.py 的測試題。")

# 這個 setup 函式是必須的，主機板開機時會呼叫它來「插上」這張卡
async def setup(bot):
    await bot.add_cog(QuizSystem(bot))