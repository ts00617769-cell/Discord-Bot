import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
import aiohttp

# 1. 加載環境變數
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# 2. 機器人初始化 (使用繼承來管理 Session 生命週期)
class PrasiaBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix='!', intents=intents)

    async def setup_hook(self):
        # ✨ 建立全域的 ClientSession (所有模組共用)
        self.session = aiohttp.ClientSession()
        
        # 掛載 Cogs
        for filename in os.listdir('./cogs'):
            if filename.endswith('.py') and not filename.startswith('__'):
                await self.load_extension(f'cogs.{filename[:-3]}')
                print(f"✅ 模組 {filename} 已成功掛載！")

    async def close(self):
        # ✨ 機器人關閉時，安全釋放連線池資源
        await self.session.close()
        await super().close()

bot = PrasiaBot()

@bot.event
async def on_ready():
    print(f'🤖 {bot.user} 已成功登入 Discord 並準備就緒！')

# 4. 啟動機器人
if __name__ == '__main__':
    bot.run(TOKEN)