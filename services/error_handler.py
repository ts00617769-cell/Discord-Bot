"""
例外處理、頻道權限與共用工具。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import traceback
from typing import Optional

import discord
from discord.ext import commands

from game_data import SERVER_MAP

logger = logging.getLogger(__name__)

CHANNEL_DENIED = "channel_denied"


def parse_env_channel_ids(
    env_name: Optional[str] = None, env_value: Optional[str] = None
) -> list[int]:
    """解析逗號分隔的頻道 ID；空白或非數字一律略過，避免 int('') 崩潰。"""
    raw = env_value if env_value is not None else os.getenv(env_name or "", "")
    return [int(x.strip()) for x in (raw or "").split(",") if x.strip().isdigit()]


def parse_env_channel_id(env_name: str, default: int = 0) -> int:
    """讀取單一頻道 ID；未設定或無效時回傳 default。"""
    ids = parse_env_channel_ids(env_name=env_name)
    return ids[0] if ids else default


def get_allowed_command_channels() -> list[int]:
    """每次從環境變數熱讀白名單（改 .env 後不必重載 cog）。"""
    return parse_env_channel_ids(env_name="ALLOWED_COMMAND_CHANNELS")


def resolve_command_channel_ids(channel) -> list[int]:
    """回傳要檢查的頻道 ID（含討論串 / 論壇貼文的 parent）。"""
    ids = [getattr(channel, "id", None)]
    parent_id = getattr(channel, "parent_id", None)
    if parent_id:
        ids.append(parent_id)
    # 少數情況：thread 的 parent 仍是 forum，再往上一層
    parent = getattr(channel, "parent", None)
    if parent is not None:
        ids.append(getattr(parent, "id", None))
        grand = getattr(parent, "parent_id", None)
        if grand:
            ids.append(grand)
    return [i for i in ids if isinstance(i, int)]


def is_allowed_command_channel(
    channel_id: int, allowed_channel_ids: Optional[list[int]] = None
) -> bool:
    """fail-closed：未設定白名單時拒絕機密指令；有設定時僅允許列表內頻道。"""
    if allowed_channel_ids is None:
        allowed_channel_ids = get_allowed_command_channels()
    if not allowed_channel_ids:
        return False
    return channel_id in allowed_channel_ids


async def _send_channel_deny(ctx) -> None:
    allowed = get_allowed_command_channels()
    try:
        if not allowed:
            await ctx.send(
                "🔒 此為戰情室機密指令，但尚未設定 `ALLOWED_COMMAND_CHANNELS`。"
                "請管理員在 `.env` 填入頻道 ID 後重啟機器人。"
            )
        else:
            await ctx.send(
                "🔒 此指令僅限戰情室指定頻道使用。\n"
                f"（目前頻道 ID：`{ctx.channel.id}`"
                + (
                    f"，父頻道：`{getattr(ctx.channel, 'parent_id', None)}`"
                    if getattr(ctx.channel, "parent_id", None)
                    else ""
                )
                + "）"
            )
    except discord.HTTPException as e:
        logger.warning(f"Failed to send channel-deny message: {e}")


async def require_allowed_channel(ctx) -> bool:
    """機密指令頻道檢查；拒絕時回覆提示。True = 允許繼續。"""
    allowed = get_allowed_command_channels()
    candidates = resolve_command_channel_ids(ctx.channel)
    if any(is_allowed_command_channel(cid, allowed) for cid in candidates):
        return True
    await _send_channel_deny(ctx)
    return False


def allowed_channel():
    """機密指令 decorator；拒絕時已送提示並拋 CheckFailure（WarRoom 靜默略過）。"""

    async def predicate(ctx: commands.Context) -> bool:
        if await require_allowed_channel(ctx):
            return True
        raise commands.CheckFailure(CHANNEL_DENIED)

    return commands.check(predicate)


def min_complete_snapshot_servers() -> int:
    """判定「全服快照已完成」所需的最少伺服器數（預設全部 SERVER_MAP）。

    可用環境變數 SNAPSHOT_MIN_SERVERS 覆寫；未設或無效時要求全服到齊。
    """
    n = len(SERVER_MAP)
    raw = (os.getenv("SNAPSHOT_MIN_SERVERS", "") or "").strip()
    if raw.isdigit():
        return max(2, min(int(raw), n))
    return max(2, n)


def min_snapshot_players() -> int:
    """單一伺服器算入完整快照所需的最少玩家數（預設 30）。

    總榜失敗或合併人數過低的服仍可寫入，但不計入 SNAPSHOT_MIN_SERVERS。
    """
    raw = (os.getenv("SNAPSHOT_MIN_PLAYERS", "") or "").strip()
    if raw.isdigit():
        return max(1, int(raw))
    return 30


def parse_env_float(env_name: str, default: float) -> float:
    """安全讀取浮點環境變數。"""
    raw = (os.getenv(env_name, "") or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"Invalid {env_name}={raw!r}, using default {default}")
        return default


async def handle_api_error(ctx, error_msg: str, detail: str = ""):
    """處理 API 呼叫錯誤"""
    try:
        await ctx.send(f"❌ {error_msg}\n若問題持續，請聯絡機器人維護者。")
        logger.error(f"API Error: {error_msg} | Detail: {detail}")
    except discord.HTTPException as e:
        logger.error(f"Failed to send error message: {e}")


async def handle_db_error(ctx, error_msg: str, exception: Exception):
    """處理資料庫錯誤"""
    try:
        await ctx.send(f"❌ 資料庫錯誤: {error_msg}")
        logger.error(f"DB Error: {error_msg} | Exception: {exception}")
    except discord.HTTPException as e:
        logger.error(f"Failed to handle DB error: {e}")


def log_command_error(ctx, command_name: str, exception: Exception):
    """記錄指令執行錯誤"""
    logger.error(
        f"Command '{command_name}' failed for user {ctx.author.id}: "
        f"{type(exception).__name__}: {exception}\n"
        f"Traceback:\n{traceback.format_exc()}"
    )


async def safe_database_operation(operation_name: str, operation_func, *args, **kwargs):
    """安全的資料庫操作包裝器；僅吞資料庫錯誤，其餘向上拋。"""
    try:
        return await operation_func(*args, **kwargs)
    except sqlite3.DatabaseError as e:
        logger.error(
            f"Database operation '{operation_name}' failed: "
            f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        )
        return None
