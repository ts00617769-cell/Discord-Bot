import atexit
import logging
import os
import socket
import sqlite3
import sys
from collections import Counter

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

from db import apply_migrations, connect_db
from db.connection import resolve_db_path
from services.ranking_api import get_ranking_client
from services.timeutil import now_naive_taipei, taipei_cutoff_str

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bot.lock")
_lock_fp = None


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
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
        except OSError:
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
    os.makedirs(os.path.dirname(LOCK_PATH) or ".", exist_ok=True)
    _lock_fp = open(LOCK_PATH, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt
            _lock_fp.seek(0)
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
    except OSError as e:
        logger.warning(f"檔案鎖失敗，改用 PID 檢查: {e}")
        _lock_fp.seek(0)
        raw = (_lock_fp.read() or "").strip()
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
                if os.name == "nt":
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
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            }
            self.session = aiohttp.ClientSession(headers=headers)
            # 經 get_ranking_client 套用 RANKING_CACHE_TTL 等環境設定
            self.ranking_client = get_ranking_client(self)

            db_path = resolve_db_path(os.path.dirname(os.path.abspath(__file__)))
            self.db = await connect_db(db_path)
            schema_ver = await apply_migrations(self.db)
            logger.info(f"✅ schema 版本: v{schema_ver}")

            if not os.getenv("ALLOWED_COMMAND_CHANNELS", "").strip():
                logger.error(
                    "❌ ALLOWED_COMMAND_CHANNELS 未設定：機密指令已改為 fail-closed，"
                    "全部機密指令將無法使用。請在 .env 填入戰情室頻道 ID。"
                )

            cogs_dir = os.path.join(os.path.dirname(__file__), "cogs")
            cog_files = sorted(
                f for f in os.listdir(cogs_dir)
                if f.endswith(".py") and not f.startswith("__")
            )
            for filename in cog_files:
                ext_name = f"cogs.{filename[:-3]}"
                if ext_name in self.extensions:
                    logger.warning(f"⚠️ 模組 {filename} 已掛載，略過重複載入")
                    continue
                try:
                    logger.info(f"⏳ 正在掛載模組 {filename}...")
                    await self.load_extension(ext_name)
                    logger.info(f"✅ 模組 {filename} 已成功掛載！")
                except commands.ExtensionFailed as e:
                    logger.error(f"❌ 模組 {filename} 掛載失敗: {e}", exc_info=True)
                except (commands.NoEntryPointError, commands.ExtensionError) as e:
                    logger.warning(f"⏭️ 略過模組 {filename}: {e}")
                except Exception as e:
                    logger.error(f"❌ 模組 {filename} 掛載失敗: {e}", exc_info=True)
            logger.info(f"✅ setup_hook 完成，已掛載 {len(self.extensions)} 個模組")
        except Exception as e:
            logger.error(f"❌ 初始化失敗: {e}", exc_info=True)
            raise

    async def close(self):
        if hasattr(self, "session"):
            await self.session.close()
        if hasattr(self, "db"):
            await self.db.close()
            logger.info("✅ 資料庫已安全關閉")
        await super().close()


bot = PrasiaBot()
_app_commands_synced = False


@bot.before_invoke
async def claim_command_once(ctx: commands.Context):
    """同一則 Discord 訊息只允許一個實例執行指令，避免雙重回覆。"""
    if not hasattr(ctx.bot, "db") or ctx.message is None:
        return
    host = socket.gethostname()
    try:
        claimed_at = now_naive_taipei().strftime("%Y-%m-%d %H:%M:%S")
        await ctx.bot.db.execute(
            "INSERT INTO cmd_dedupe (message_id, claimed_at, pid, host) VALUES (?, ?, ?, ?)",
            (ctx.message.id, claimed_at, os.getpid(), host),
        )
        await ctx.bot.db.commit()
    except sqlite3.IntegrityError:
        logger.warning(
            f"⚠️ 略過重複指令: msg={ctx.message.id} cmd={getattr(ctx.command, 'name', '?')} "
            f"pid={os.getpid()} host={host}"
        )
        raise commands.CheckFailure("duplicate_invoke") from None


@bot.event
async def on_command_error(ctx, error):
    """去重略過；WarRoom 在場時由其處理；否則在此回覆使用者。"""
    if isinstance(error, commands.CheckFailure) and str(error) in (
        "duplicate_invoke",
        "channel_denied",
    ):
        return
    if isinstance(error, (commands.CommandNotFound, commands.NotOwner)):
        return
    # 冷卻／併發／參數錯誤由 WarRoom.on_command_error 統一回覆，避免雙重訊息
    if isinstance(
        error,
        (
            commands.CommandOnCooldown,
            commands.MaxConcurrencyReached,
            commands.UserInputError,
        ),
    ):
        if bot.get_cog("WarRoom") is not None:
            return
        # WarRoom 未載入時仍給使用者簡短回覆
        try:
            if isinstance(error, commands.CommandOnCooldown):
                await ctx.send(
                    f"⏳ 指令冷卻中，請再等 **{error.retry_after:.0f}** 秒。",
                    delete_after=10,
                )
            elif isinstance(error, commands.MaxConcurrencyReached):
                await ctx.send(
                    "⏳ 相同指令尚在執行中，請稍候完成後再試。", delete_after=10
                )
            else:
                usage = getattr(ctx.command, "help", None) or "請檢查指令參數。"
                await ctx.send(f"❌ 參數錯誤。{usage}", delete_after=15)
        except discord.HTTPException:
            pass
        return

    if bot.get_cog("WarRoom") is not None:
        return

    logger.error(
        f"Command error in {getattr(ctx.command, 'name', '?')}: {error}",
        exc_info=error,
    )
    try:
        await ctx.send("❌ 指令執行失敗，已記錄。", delete_after=15)
    except discord.HTTPException:
        pass


@bot.event
async def on_ready():
    global _app_commands_synced
    host = socket.gethostname()
    logger.info(
        f"🤖 {bot.user} 已成功登入 Discord 並準備就緒！ (PID {os.getpid()} @ {host})"
    )
    name_counts = Counter(cmd.name for cmd in bot.commands)
    dupes = [name for name, count in name_counts.items() if count > 1]
    if dupes:
        logger.error(f"❌ 偵測到重複註冊的指令（會造成雙重回覆）: {dupes}")
    else:
        logger.info(f"✅ 指令註冊正常，共 {len(bot.commands)} 個指令")

    if not _app_commands_synced:
        try:
            synced = await bot.tree.sync()
            _app_commands_synced = True
            logger.info(f"✅ 已同步 {len(synced)} 個應用程式指令（slash / hybrid）")
        except discord.HTTPException as e:
            logger.warning(f"應用程式指令同步失敗: {e}")

    try:
        await bot.change_presence(
            activity=discord.Game(name=f"戰情雷達 | {host[:12]}#{os.getpid()}")
        )
    except discord.HTTPException as e:
        logger.warning(f"無法更新 presence: {e}")

    try:
        cutoff = taipei_cutoff_str(2)
        await bot.db.execute(
            "DELETE FROM cmd_dedupe WHERE claimed_at < ?",
            (cutoff,),
        )
        await bot.db.commit()
    except sqlite3.DatabaseError as e:
        logger.warning(f"清理 cmd_dedupe 失敗: {e}")

@bot.command(name="reload", hidden=True)
@commands.is_owner()
async def reload_cog(ctx, extension: str):
    """(開發者專用) 重新載入特定的模組，不用重開機器人！"""
    try:
        await bot.reload_extension(f"cogs.{extension}")
        await ctx.send(f"✅ 模組 `cogs.{extension}` 重新載入成功！")
    except commands.ExtensionError as e:
        await ctx.send(f"❌ 重新載入失敗：\n```py\n{e}\n```")


if __name__ == "__main__":
    if not TOKEN:
        print("❌ 未設定 DISCORD_TOKEN！請檢查 .env 檔案。")
    else:
        acquire_singleton_lock()
        print(f"🔒 單例鎖已取得 (PID {os.getpid()} @ {socket.gethostname()})")
        print(
            "⚠️ 若指令仍回兩次：代表另一台機器/雲端也在跑同一個 Token，"
            "請關閉其中一邊，或到 Discord Developer Portal 重設 Token。"
        )
        bot.run(TOKEN)
