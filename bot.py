import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
import aiohttp
import aiosqlite # 🌟 引入非同步資料庫

# 1. 加載環境變數
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

class PrasiaBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix='!', intents=intents)

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        # ✨ 建立全域的非同步資料庫連線
        self.db = await aiosqlite.connect('prasia_data.db') 
        
        for filename in os.listdir('./cogs'):
            if filename.endswith('.py') and not filename.startswith('__'):
                await self.load_extension(f'cogs.{filename[:-3]}')
                print(f"✅ 模組 {filename} 已成功掛載！")

    async def close(self):
        await self.session.close()
        # ✨ 安全關閉資料庫
        await self.db.close() 
        await super().close()

bot = PrasiaBot()

@bot.event
async def on_ready():
    print(f'🤖 {bot.user} 已成功登入 Discord 並準備就緒！')

# ==========================================
# 👇 推薦新功能：開發者熱重載指令
# ==========================================
@bot.command(name="reload", hidden=True)
@commands.is_owner() # 🛡️ 資安防護：只允許機器人的「擁有者」執行這個指令
async def reload_cog(ctx, extension: str):
    """(開發者專用) 重新載入特定的模組，不用重開機器人！"""
    try:
        # discord.py 內建的重新載入功能
        await bot.reload_extension(f"cogs.{extension}")
        await ctx.send(f"✅ 模組 `cogs.{extension}` 重新載入成功！")
    except Exception as e:
        await ctx.send(f"❌ 重新載入失敗：\n```py\n{e}\n```")

# 4. 啟動機器人
if __name__ == '__main__':
    if not TOKEN:
        print("❌ 未設定 DISCORD_TOKEN！請檢查 .env 檔案。")
    else:
        bot.run(TOKEN)