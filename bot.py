from __future__ import annotations

import atexit
import logging
import os
import socket
import sqlite3
import sys
from collections import Counter
from typing import Any, cast

import aiohttp
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

from db import apply_migrations, connect_db, connect_db_ro
from db.connection import DatabaseIntegrityError, resolve_db_path
from db.instance_lock import (
    make_holder_id,
    refresh_instance_lock,
    release_instance_lock,
    try_acquire_instance_lock,
)
from db.schema import (
    StartupReadinessError,
    ensure_startup_db_readiness,
)
from services.ranking_api import get_ranking_client
from services.timeutil import now_naive_taipei, taipei_cutoff_str

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
CRITICAL_EXTENSIONS = frozenset(
    {
        "cogs.exp_commands",
        "cogs.exp_tracker",
        "cogs.player_search",
        "cogs.rank",
        "cogs.war_room",
    }
)

LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bot.lock")
_lock_fp = None


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            # windll 僅存在於 Windows；CI（Linux）mypy 會報 attr-defined
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if handle:
                kernel32.CloseHandle(handle)
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
        # 無法取得獨占鎖時直接拒絕啟動，避免僅靠 PID 檢查造成雙開
        print(f"❌ 無法取得本機單例鎖，拒絕啟動: {e}")
        print("   請確認 .bot.lock 可寫入，並結束其他 python 實例後重試。")
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
        self.instance_holder_id = make_holder_id()
        self._heartbeat_fail_count = 0

    HEARTBEAT_FAIL_LIMIT = 3

    @tasks.loop(seconds=30)
    async def instance_heartbeat(self):
        try:
            if not await refresh_instance_lock(
                self.instance_db, self.instance_holder_id
            ):
                logger.critical("共享 DB 實例鎖已失效，立即關閉避免重複執行")
                await self.close()
                return
            self._heartbeat_fail_count = 0
        except sqlite3.DatabaseError as e:
            self._heartbeat_fail_count += 1
            logger.error(
                "更新共享 DB 實例 heartbeat 失敗 (%s/%s): %s",
                self._heartbeat_fail_count,
                self.HEARTBEAT_FAIL_LIMIT,
                e,
            )
            if self._heartbeat_fail_count >= self.HEARTBEAT_FAIL_LIMIT:
                logger.critical(
                    "共享 DB heartbeat 連續失敗，立即關閉避免 split-brain"
                )
                await self.close()

    async def setup_hook(self):
        try:
            logger.info("⏳ setup_hook 開始（登入後初始化）…")
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
            logger.info("⏳ 正在開啟資料庫 %s …", db_path)
            try:
                self.db = await connect_db(db_path, check_integrity=True)
            except DatabaseIntegrityError as e:
                logger.error(f"❌ 資料庫完整性檢查失敗，拒絕啟動: {e}")
                raise
            schema_ver = await apply_migrations(self.db)
            logger.info(f"✅ schema 版本: v{schema_ver}")
            # heartbeat 使用獨立連線，避免其 commit 切斷快照的原子交易
            self.instance_db = await connect_db(db_path, check_integrity=False)
            self.snapshot_db = await connect_db(db_path, check_integrity=False)
            if not await try_acquire_instance_lock(
                self.instance_db, self.instance_holder_id
            ):
                raise RuntimeError(
                    "共享 DB 已有另一個 bot 實例持有鎖；請關閉舊實例或等待鎖逾時"
                )
            self.instance_heartbeat.start()

            try:
                self.db_ro = await connect_db_ro(db_path)
            except (OSError, FileNotFoundError) as e:
                logger.warning(f"唯讀連線開啟失敗，重查詢將共用寫入連線: {e}")
                self.db_ro = self.db

            try:
                await ensure_startup_db_readiness(self.db)
                logger.info("✅ 搜尋索引與 player_profile denorm 已就緒")
            except StartupReadinessError as e:
                logger.critical("❌ 資料庫搜尋條件未就緒，拒絕啟動: %s", e)
                raise

            if not os.getenv("ALLOWED_COMMAND_CHANNELS", "").strip():
                logger.error(
                    "❌ ALLOWED_COMMAND_CHANNELS 未設定：機密指令已改為 fail-closed，"
                    "全部機密指令將無法使用。請在 .env 填入戰情室頻道 ID。"
                )
            if not os.getenv("EXP_ALERT_CHANNEL_ID", "").strip():
                logger.warning(
                    "⚠️ EXP_ALERT_CHANNEL_ID 未設定：超速警報將不會發送。"
                )
            if not os.getenv("TRANSFER_ALERT_CHANNEL_ID", "").strip():
                logger.warning(
                    "⚠️ TRANSFER_ALERT_CHANNEL_ID 未設定：轉服／改名警報將不會發送。"
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
                except Exception as e:
                    if ext_name in CRITICAL_EXTENSIONS:
                        logger.critical(
                            "❌ 必要模組 %s 掛載失敗，拒絕半殘啟動",
                            filename,
                            exc_info=True,
                        )
                        raise RuntimeError(
                            f"critical extension failed: {ext_name}"
                        ) from e
                    logger.warning(
                        "⏭️ 非必要模組 %s 掛載失敗，繼續啟動: %s",
                        filename,
                        e,
                        exc_info=True,
                    )
            logger.info(f"✅ setup_hook 完成，已掛載 {len(self.extensions)} 個模組")
        except Exception as e:
            logger.error(f"❌ 初始化失敗: {e}", exc_info=True)
            raise

    async def close(self):
        if self.instance_heartbeat.is_running():
            self.instance_heartbeat.cancel()
        if hasattr(self, "session"):
            await self.session.close()
        db_ro = getattr(self, "db_ro", None)
        db = getattr(self, "db", None)
        snapshot_db = getattr(self, "snapshot_db", None)
        if db_ro is not None and db_ro is not db:
            await db_ro.close()
            logger.info("✅ 唯讀資料庫已關閉")
        if db is not None:
            instance_db = getattr(self, "instance_db", None)
            try:
                if instance_db is not None:
                    await release_instance_lock(
                        instance_db, self.instance_holder_id
                    )
            except sqlite3.DatabaseError as e:
                logger.warning("釋放共享 DB 實例鎖失敗: %s", e)
            if instance_db is not None:
                await instance_db.close()
            if snapshot_db is not None:
                await snapshot_db.close()
            await db.close()
            logger.info("✅ 資料庫已安全關閉")
        await super().close()


bot = PrasiaBot()
_app_commands_synced = False


def invoke_dedupe_id(ctx: commands.Context) -> int | None:
    """prefix 用 message snowflake，slash/hybrid 用 interaction snowflake。"""
    interaction_id = getattr(ctx.interaction, "id", None)
    if interaction_id is not None:
        return int(interaction_id)
    message_id = getattr(ctx.message, "id", None)
    return int(message_id) if message_id is not None else None


@bot.before_invoke
async def claim_command_once(ctx: commands.Context):
    """同一則 Discord 訊息只允許一個實例執行指令，避免雙重回覆。"""
    ctx._cmd_dedupe_claimed = False  # type: ignore[attr-defined]
    if not hasattr(ctx.bot, "db"):
        return
    invoke_id = invoke_dedupe_id(ctx)
    if invoke_id is None:
        logger.warning("指令無 message/interaction ID，無法執行去重")
        return
    host = socket.gethostname()
    try:
        claimed_at = now_naive_taipei().strftime("%Y-%m-%d %H:%M:%S")
        await ctx.bot.db.execute(
            "INSERT INTO cmd_dedupe (message_id, claimed_at, pid, host) VALUES (?, ?, ?, ?)",
            (invoke_id, claimed_at, os.getpid(), host),
        )
        await ctx.bot.db.commit()
        ctx._cmd_dedupe_claimed = True  # type: ignore[attr-defined]
        ctx._cmd_dedupe_id = invoke_id  # type: ignore[attr-defined]
    except sqlite3.IntegrityError:
        logger.warning(
            f"⚠️ 略過重複指令: invoke={invoke_id} cmd={getattr(ctx.command, 'name', '?')} "
            f"pid={os.getpid()} host={host}"
        )
        raise commands.CheckFailure("duplicate_invoke") from None


@bot.after_invoke
async def release_command_claim_on_failure(ctx: commands.Context):
    """指令失敗時釋放 claim，允許同則訊息重試（暫態錯誤）。"""
    if not getattr(ctx, "_cmd_dedupe_claimed", False):
        return
    if not getattr(ctx, "command_failed", False):
        return
    invoke_id = getattr(ctx, "_cmd_dedupe_id", None)
    if invoke_id is None or not hasattr(ctx.bot, "db"):
        return
    try:
        await ctx.bot.db.execute(
            "DELETE FROM cmd_dedupe WHERE message_id = ?", (invoke_id,)
        )
        await ctx.bot.db.commit()
    except sqlite3.DatabaseError as e:
        logger.warning("釋放 cmd_dedupe claim 失敗: %s", e)


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
@cast(Any, commands.is_owner())
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
