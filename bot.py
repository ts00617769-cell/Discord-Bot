import discord
from discord.ext import commands
import os
import sys
import atexit
import socket
import sqlite3
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
    # Windows：用 OpenProcess 較可靠
    if os.name == 'nt':
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def acquire_singleton_lock():
    """以作業系統檔案鎖阻擋同一台機器的第二個實例。"""
    global _lock_fp
    os.makedirs(os.path.dirname(LOCK_PATH) or '.', exist_ok=True)
    _lock_fp = open(LOCK_PATH, 'a+', encoding='utf-8')
    try:
        if os.name == 'nt':
            import msvcrt
            _lock_fp.seek(0)
            # 鎖住檔案開頭 1 byte；已被鎖則代表另一實例仍在跑
            try:
                msvcrt.locking(_lock_fp.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                print("❌ 偵測到本機已有機器人實例正在執行（檔案鎖）。")
                print("   請先結束舊的 python 程序，否則指令會回覆兩次。")
                print("   可執行: taskkill /IM python.exe /F")
                sys.exit(1)
        else:
            import fcntl
            try:
                fcntl.flock(_lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                print("❌ 偵測到本機已有機器人實例正在執行（檔案鎖）。")
                print("   請先結束舊程序，否則指令會回覆兩次。")
                sys.exit(1)
    except Exception as e:
        # 退回 PID 檔檢查
        logger.warning(f"檔案鎖失敗，改用 PID 檢查: {e}")
        _lock_fp.seek(0)
        raw = (_lock_fp.read() or '').strip()
        try:
            old_pid = int(raw) if raw else 0
        except ValueError:
            old_pid = 0
        if old_pid and old_pid != os.getpid() and _pid_is_running(old_pid):
            print(f"❌ 偵測到機器人已在執行中 (PID {old_pid})。")
            print(f"   Windows 可執行: taskkill /PID {old_pid} /F")
            sys.exit(1)

    _lock_fp.seek(0)
    _lock_fp.truncate()
    _lock_fp.write(str(os.getpid()))
    _lock_fp.flush()

    def _release():
        global _lock_fp
        try:
            if _lock_fp:
                if os.name == 'nt':
                    try:
                        import msvcrt
                        _lock_fp.seek(0)
                        msvcrt.locking(_lock_fp.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    try:
                        import fcntl
                        fcntl.flock(_lock_fp.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                _lock_fp.close()
                _lock_fp = None
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
            db_path = os.path.join(os.path.dirname(__file__), 'prasia_data.db')
            self.db = await aiosqlite.connect(db_path)
            await self.db.execute("PRAGMA journal_mode=WAL")
            await self.db.execute("PRAGMA busy_timeout=5000")
            logger.info(f"✅ 資料庫已連接: {db_path}")

            # 跨實例去重：同一個 message_id 只能被執行一次（需共用同一資料庫才有效）
            await self.db.execute('''
                CREATE TABLE IF NOT EXISTS cmd_dedupe (
                    message_id INTEGER PRIMARY KEY,
                    claimed_at TEXT NOT NULL,
                    pid INTEGER,
                    host TEXT
                )
            ''')
            await self.db.commit()

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
        if hasattr(self, 'db'):
            await self.db.close()
            logger.info("✅ 資料庫已安全關閉")
        await super().close()

bot = PrasiaBot()


@bot.before_invoke
async def claim_command_once(ctx: commands.Context):
    """同一則 Discord 訊息只允許一個實例執行指令，避免雙重回覆。"""
    if not hasattr(ctx.bot, 'db') or ctx.message is None:
        return
    host = socket.gethostname()
    try:
        await ctx.bot.db.execute(
            'INSERT INTO cmd_dedupe (message_id, claimed_at, pid, host) VALUES (?, datetime("now"), ?, ?)',
            (ctx.message.id, os.getpid(), host)
        )
        await ctx.bot.db.commit()
    except sqlite3.IntegrityError:
        logger.warning(
            f"⚠️ 略過重複指令: msg={ctx.message.id} cmd={getattr(ctx.command, 'name', '?')} "
            f"pid={os.getpid()} host={host}"
        )
        raise commands.CheckFailure("duplicate_invoke")


@bot.event
async def on_command_error(ctx, error):
    # 去重失敗不需打擾使用者 / 戰情室
    if isinstance(error, commands.CheckFailure) and str(error) == "duplicate_invoke":
        return
    if isinstance(error, (commands.CommandNotFound, commands.NotOwner)):
        return
    # 其餘交給 WarRoom 或其他 handler；這裡只記 log 避免未處理
    if hasattr(error, 'original'):
        logger.error(f"Command error in {ctx.command}: {error.original}")
    else:
        logger.error(f"Command error in {ctx.command}: {error}")


@bot.event
async def on_ready():
    host = socket.gethostname()
    print(f'🤖 {bot.user} 已成功登入 Discord 並準備就緒！ (PID {os.getpid()} @ {host})')
    name_counts = Counter(cmd.name for cmd in bot.commands)
    dupes = [name for name, count in name_counts.items() if count > 1]
    if dupes:
        logger.error(f"❌ 偵測到重複註冊的指令（會造成雙重回覆）: {dupes}")
    else:
        logger.info(f"✅ 指令註冊正常，共 {len(bot.commands)} 個指令")

    # 狀態列顯示主機與 PID，方便辨認是否開了兩個實例
    try:
        await bot.change_presence(
            activity=discord.Game(name=f"戰情雷達 | {host[:12]}#{os.getpid()}")
        )
    except Exception as e:
        logger.warning(f"無法更新 presence: {e}")

    # 清理過期去重紀錄
    try:
        await bot.db.execute("DELETE FROM cmd_dedupe WHERE claimed_at < datetime('now', '-2 days')")
        await bot.db.commit()
    except Exception as e:
        logger.warning(f"清理 cmd_dedupe 失敗: {e}")


@bot.command(name="reload", hidden=True)
@commands.is_owner()
async def reload_cog(ctx, extension: str):
    """(開發者專用) 重新載入特定的模組，不用重開機器人！"""
    try:
        await bot.reload_extension(f"cogs.{extension}")
        await ctx.send(f"✅ 模組 `cogs.{extension}` 重新載入成功！")
    except Exception as e:
        await ctx.send(f"❌ 重新載入失敗：\n```py\n{e}\n```")


if __name__ == '__main__':
    if not TOKEN:
        print("❌ 未設定 DISCORD_TOKEN！請檢查 .env 檔案。")
    else:
        acquire_singleton_lock()
        print(f"🔒 單例鎖已取得 (PID {os.getpid()} @ {socket.gethostname()})")
        print("⚠️ 若指令仍回兩次：代表另一台機器/雲端也在跑同一個 Token，請關閉其中一邊，或到 Discord Developer Portal 重設 Token。")
        bot.run(TOKEN)
