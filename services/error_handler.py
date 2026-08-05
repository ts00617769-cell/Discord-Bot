"""
例外處理、頻道權限與共用工具。
"""
from __future__ import annotations

import logging
import os
import traceback
from typing import Optional

import discord
from discord.ext import commands

from game_data import SERVER_MAP

logger = logging.getLogger(__name__)

CHANNEL_DENIED = "channel_denied"


async def resolve_bot_channel(bot, channel_id: int, *, label: str = "channel"):
    """先查 Discord cache，未命中時再走 REST；解析失敗回傳 None。"""
    if not channel_id:
        return None
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(channel_id)
    except discord.HTTPException as e:
        logger.error("Failed to resolve %s %s: %s", label, channel_id, e)
        return None


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


async def require_allowed_channel(
    ctx, extra_channel_ids: Optional[list[int]] = None
) -> bool:
    """機密指令頻道檢查；拒絕時回覆提示。True = 允許繼續。

    extra_channel_ids：額外允許的頻道（例如轉移警報目標頻道也可跑測試指令）。
    """
    allowed = get_allowed_command_channels()
    if extra_channel_ids:
        allowed = list(dict.fromkeys([*allowed, *extra_channel_ids]))
    candidates = resolve_command_channel_ids(ctx.channel)
    if any(is_allowed_command_channel(cid, allowed) for cid in candidates):
        return True
    await _send_channel_deny(ctx)
    return False


def allowed_channel(*extra_env_names: str):
    """機密指令 decorator；拒絕時已送提示並拋 CheckFailure（WarRoom 靜默略過）。

    可傳入額外環境變數名稱（如 TRANSFER_ALERT_CHANNEL_ID），那些頻道也視為允許。
    """

    async def predicate(ctx: commands.Context) -> bool:
        extras: list[int] = []
        for name in extra_env_names:
            extras.extend(parse_env_channel_ids(env_name=name))
        if await require_allowed_channel(ctx, extra_channel_ids=extras or None):
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


def log_command_error(ctx, command_name: str, exception: Exception):
    """記錄指令執行錯誤"""
    logger.error(
        f"Command '{command_name}' failed for user {ctx.author.id}: "
        f"{type(exception).__name__}: {exception}\n"
        f"Traceback:\n{traceback.format_exc()}"
    )


async def handle_command_error(
    ctx,
    error,
    *,
    log_channel_id: int | None = None,
    bot=None,
) -> bool:
    """統一處理指令錯誤（WarRoom 與 bot 後備共用）。回傳 True 表示已處理。"""
    if isinstance(error, commands.CheckFailure) and str(error) in (
        "duplicate_invoke",
        "channel_denied",
    ):
        return True

    if isinstance(error, commands.CommandOnCooldown):
        try:
            await ctx.send(
                f"⏳ 指令冷卻中，請再等 **{error.retry_after:.0f}** 秒。",
                delete_after=10,
            )
        except discord.HTTPException:
            pass
        return True
    if isinstance(error, commands.MaxConcurrencyReached):
        try:
            await ctx.send(
                "⏳ 相同指令尚在執行中，請稍候完成後再試。", delete_after=10
            )
        except discord.HTTPException:
            pass
        return True
    if isinstance(error, commands.UserInputError):
        try:
            usage = getattr(ctx.command, "help", None) or "請檢查指令參數。"
            await ctx.send(f"❌ 參數錯誤。{usage}", delete_after=15)
        except discord.HTTPException:
            pass
        return True

    if isinstance(
        error,
        (commands.CommandNotFound, commands.NotOwner, commands.CheckFailure),
    ):
        return True

    logger.error(
        f"Command error in {getattr(ctx.command, 'name', '?')}: {error}\n"
        f"{''.join(traceback.format_exception(type(error), error, error.__traceback__))}"
    )

    try:
        await ctx.send("❌ 指令執行失敗，已記錄。", delete_after=15)
    except discord.HTTPException:
        pass

    if not log_channel_id or bot is None:
        return True

    channel = await resolve_bot_channel(
        bot, log_channel_id, label="war room log channel"
    )
    if channel:
        error_msg = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        cmd_name = getattr(ctx.command, "qualified_name", "unknown")
        try:
            await channel.send(
                f"🔴 **【系統報錯】**\n出錯頻道：<#{ctx.channel.id}>\n出錯指令：`!{cmd_name}`\n"
                f"```python\n{error_msg[:1900]}\n```"
            )
        except discord.HTTPException as e:
            logger.error(f"Failed to send war room error report: {e}")
    return True
