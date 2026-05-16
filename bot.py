import discord
from discord.ext import commands
import os
from dotenv import load_dotenv

# 1. 加載環境變數
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# 2. 機器人初始化
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

# 3. 自動掛載所有 cogs 資料夾下的擴充卡
@bot.event
async def setup_hook():
    for filename in os.listdir('./cogs'):
        if filename.endswith('.py') and not filename.startswith('__'):
            await bot.load_extension(f'cogs.{filename[:-3]}')
            print(f"✅ 模組 {filename} 已成功掛載！")

@bot.event
async def on_ready():
    print(f'🤖 {bot.user} 已成功登入 Discord 並準備就緒！')

# 4. 啟動機器人
bot.run(TOKEN)