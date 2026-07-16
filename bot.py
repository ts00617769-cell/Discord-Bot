import discord
from discord.ext import commands
import os
import sys
import atexit
from collections import Counter
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

LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.bot.lock')
_lock_fp = None


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Windows：行程不存在時常拋 OSError
        return False
    return True


def acquire_singleton_lock():
    """防止同一台機器開兩個 bot 實例（會造成指令回覆兩次）。"""
    global _lock_fp
    if os.path.exists(LOCK_PATH):
        try:
            with open(LOCK_PATH, 'r', encoding='utf-8') as f:
                old_pid = int((f.read() or '0').strip() or '0')
        except (ValueError, OSError):
            old_pid = 0
        if old_pid and old_pid != os.getpid() and _pid_is_running(old_pid):
            print(f"❌ 偵測到機器人已在執行中 (PID {old_pid})。")
            print("   請先關閉舊程序再啟動，否則指令會回覆兩次。")
            print(f"   Windows 可執行: taskkill /PID {old_pid} /F")
            sys.exit(1)
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass

    _lock_fp = open(LOCK_PATH, 'w', encoding='utf-8')
    _lock_fp.write(str(os.getpid()))
    _lock_fp.flush()

    def _release():
        try:
            if _lock_fp:
                _lock_fp.close()
            if os.path.exists(LOCK_PATH):
                with open(LOCK_PATH, 'r', encoding='utf-8') as f:
                    locked_pid = int((f.read() or '0').strip() or '0')
                if locked_pid == os.getpid():
                    os.remove(LOCK_PATH)
        except OSError:
            pass

    atexit.register(_release)


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
                    ext_name = f'cogs.{filename[:-3]}'
                    if ext_name in self.extensions:
                        logger.warning(f"⚠️ 模組 {filename} 已掛載，略過重複載入")
                        continue
                    try:
                        await self.load_extension(ext_name)
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
    print(f'🤖 {bot.user} 已成功登入 Discord 並準備就緒！ (PID {os.getpid()})')
    name_counts = Counter(cmd.name for cmd in bot.commands)
    dupes = [name for name, count in name_counts.items() if count > 1]
    if dupes:
        logger.error(f"❌ 偵測到重複註冊的指令（會造成雙重回覆）: {dupes}")
    else:
        logger.info(f"✅ 指令註冊正常，共 {len(bot.commands)} 個指令")

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
        acquire_singleton_lock()
        bot.run(TOKEN)
