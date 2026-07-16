import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
import aiohttp
import aiosqlite
import logging

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
            self.session = aiohttp.ClientSession(headers=headers)
            # ✨ 建立全域的非同步資料庫連線
            db_path = os.path.join(os.path.dirname(__file__), 'prasia_data.db')
            self.db = await aiosqlite.connect(db_path)
            # 減少並行讀寫時 database is locked 的機率
            await self.db.execute("PRAGMA journal_mode=WAL")
            await self.db.execute("PRAGMA busy_timeout=5000")
            logger.info(f"✅ 資料庫已連接: {db_path}")

            if not os.getenv("ALLOWED_COMMAND_CHANNELS", "").strip():
                logger.warning("⚠️ ALLOWED_COMMAND_CHANNELS 未設定：機密指令目前可在所有頻道使用")

            cogs_dir = os.path.join(os.path.dirname(__file__), 'cogs')
            for filename in os.listdir(cogs_dir):
                if filename.endswith('.py') and not filename.startswith('__') and filename != 'error_handler.py':
                    try:
                        await self.load_extension(f'cogs.{filename[:-3]}')
                        logger.info(f"✅ 模組 {filename} 已成功掛載！")
                    except Exception as e:
                        logger.error(f"❌ 模組 {filename} 掛載失敗: {e}")
        except Exception as e:
            logger.error(f"❌ 初始化失敗: {e}")
            raise

    async def close(self):
        if hasattr(self, 'session'):
            await self.session.close()
        # ✨ 安全關閉資料庫
        if hasattr(self, 'db'):
            await self.db.close()
            logger.info("✅ 資料庫已安全關閉")
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