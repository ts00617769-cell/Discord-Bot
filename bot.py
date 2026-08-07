from __future__ import annotations

import asyncio
import logging
import os
import socket
import sqlite3
from collections import Counter

import aiohttp
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

from db import apply_migrations, connect_db, connect_db_ro
from db.connection import INSTANCE_BUSY_TIMEOUT_MS, DatabaseIntegrityError
from db.instance_lock import (
    make_holder_id,
    refresh_instance_lock,
    release_instance_lock,
    try_acquire_instance_lock,
)
from db.paths import resolve_db_path
from db.schema import (
    StartupReadinessError,
    ensure_startup_db_readiness,
)
from db.singleton_lock import acquire_process_lock
from services.cmd_dedupe import (
    invoke_dedupe_id,
    prune_command_dedupe,
    release_command_claim,
    try_claim_command,
)
from services.db_lock import run_locked
from services.error_handler import (
    CANNOT_SEND,
    bot_can_send_in_channel,
    handle_command_error,
    parse_env_channel_id,
)
from services.ranking_api import get_ranking_client

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


class PrasiaBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.instance_holder_id = make_holder_id()
        self._heartbeat_fail_count = 0
        self._fatal_shutdown_started = False
        self._closing = False
        # 僅協調本程序內的大型寫入；heartbeat 保持獨立，避免 lease 被清庫拖住。
        self.db_write_lock = asyncio.Lock()

    HEARTBEAT_FAIL_LIMIT = 5

    async def fatal_shutdown(self, reason: str) -> None:
        """立即結束失去安全執行條件的程序，交由容器重啟。

        不呼叫 close()：此方法可能由 heartbeat task 本身觸發，而 close()
        會取消 heartbeat，造成清理在只關閉 HTTP session 後中斷，留下殭屍程序。
        """
        if getattr(self, "_fatal_shutdown_started", False):
            return
        self._fatal_shutdown_started = True
        logger.critical("%s；立即終止程序，等待容器重啟", reason)
        instance_db = getattr(self, "instance_db", None)
        if instance_db is not None:
            try:
                await asyncio.wait_for(
                    release_instance_lock(instance_db, self.instance_holder_id),
                    timeout=2,
                )
            except (asyncio.TimeoutError, sqlite3.DatabaseError) as e:
                logger.warning("終止前釋放共享 DB 實例鎖失敗: %s", e)
            except Exception:
                logger.warning("終止前釋放共享 DB 實例鎖發生非預期錯誤", exc_info=True)
        logging.shutdown()
        os._exit(1)

    @tasks.loop(seconds=30)
    async def instance_heartbeat(self):
        try:
            if not await refresh_instance_lock(
                self.instance_db, self.instance_holder_id
            ):
                await self.fatal_shutdown("共享 DB 實例鎖已失效，避免重複執行")
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
                await self.fatal_shutdown(
                    "共享 DB heartbeat 連續失敗，避免 split-brain"
                )
                return
        except Exception:
            logger.critical(
                "共享 DB heartbeat 發生非預期錯誤",
                exc_info=True,
            )
            await self.fatal_shutdown(
                "共享 DB heartbeat 發生非預期錯誤，避免 split-brain"
            )

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
            self.instance_db = await connect_db(
                db_path,
                check_integrity=False,
                busy_timeout_ms=INSTANCE_BUSY_TIMEOUT_MS,
            )
            self.snapshot_db = await connect_db(db_path, check_integrity=False)
            if not await try_acquire_instance_lock(
                self.instance_db, self.instance_holder_id
            ):
                raise RuntimeError(
                    "共享 DB 已有另一個 bot 實例持有鎖；請關閉舊實例或等待鎖逾時"
                )

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
            self.instance_heartbeat.start()

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
        if getattr(self, "_closing", False):
            return
        self._closing = True
        if self.instance_heartbeat.is_running():
            self.instance_heartbeat.cancel()
        # 先卸載 cogs，讓其 tasks.loop / 背景工作停止，再關閉共用資源。
        for ext_name in reversed(list(self.extensions)):
            try:
                await self.unload_extension(ext_name)
            except Exception:
                logger.warning("關閉時卸載模組 %s 失敗", ext_name, exc_info=True)
        await asyncio.sleep(0)
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


@bot.check
async def ensure_bot_can_reply(ctx: commands.Context) -> bool:
    """沒有發言權限就別執行：避免指令改完狀態卻回不了訊息（403 50001）。"""
    if bot_can_send_in_channel(ctx.channel, getattr(ctx, "me", None)):
        return True
    raise commands.CheckFailure(CANNOT_SEND)


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
    if not await try_claim_command(
        ctx.bot.db,
        invoke_id,
        host=host,
        write_lock=getattr(ctx.bot, "db_write_lock", None),
    ):
        logger.warning(
            f"⚠️ 略過重複指令: invoke={invoke_id} cmd={getattr(ctx.command, 'name', '?')} "
            f"pid={os.getpid()} host={host}"
        )
        raise commands.CheckFailure("duplicate_invoke") from None
    ctx._cmd_dedupe_claimed = True  # type: ignore[attr-defined]
    ctx._cmd_dedupe_id = invoke_id  # type: ignore[attr-defined]


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
        await run_locked(
            getattr(ctx.bot, "db_write_lock", None),
            release_command_claim,
            ctx.bot.db,
            invoke_id,
        )
    except sqlite3.DatabaseError as e:
        logger.warning("釋放 cmd_dedupe claim 失敗: %s", e)


@bot.event
async def on_command_error(ctx, error):
    """WarRoom 在場時由其處理；否則在此回覆使用者。"""
    if bot.get_cog("WarRoom") is not None:
        return
    await handle_command_error(
        ctx,
        error,
        log_channel_id=parse_env_channel_id("WAR_ROOM_CHANNEL_ID") or None,
        bot=bot,
    )


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
        await run_locked(bot.db_write_lock, prune_command_dedupe, bot.db)
    except sqlite3.DatabaseError as e:
        logger.warning(f"清理 cmd_dedupe 失敗: {e}")

if __name__ == "__main__":
    if not TOKEN:
        print("❌ 未設定 DISCORD_TOKEN！請檢查 .env 檔案。")
    else:
        acquire_process_lock()
        print(f"🔒 單例鎖已取得 (PID {os.getpid()} @ {socket.gethostname()})")
        print(
            "⚠️ 若指令仍回兩次：代表另一台機器/雲端也在跑同一個 Token，"
            "請關閉其中一邊，或到 Discord Developer Portal 重設 Token。"
        )
        bot.run(TOKEN)
