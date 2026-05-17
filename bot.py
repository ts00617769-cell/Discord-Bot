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

# 4. 啟動機器人
if __name__ == '__main__':
    bot.run(TOKEN)