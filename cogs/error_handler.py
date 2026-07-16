"""
例外处理和日志工具模块
用于统一处理异常和日志输出
"""
import logging
import os
import traceback
import discord

logger = logging.getLogger(__name__)


def parse_env_channel_ids(env_name: str = None, env_value: str = None) -> list:
    """解析逗號分隔的頻道 ID；空白或非數字一律略過，避免 int('') 崩潰。"""
    raw = env_value if env_value is not None else os.getenv(env_name or "", "")
    return [int(x.strip()) for x in (raw or "").split(",") if x.strip().isdigit()]


def parse_env_channel_id(env_name: str, default: int = 0) -> int:
    """讀取單一頻道 ID；未設定或無效時回傳 default。"""
    ids = parse_env_channel_ids(env_name=env_name)
    return ids[0] if ids else default


def is_allowed_command_channel(channel_id: int, allowed_channel_ids: list) -> bool:
    """allowlist 空白時不限制（尚未設定）；有設定時僅允許列表內頻道。"""
    if not allowed_channel_ids:
        return True
    return channel_id in allowed_channel_ids


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
    """处理 API 调用错误"""
    try:
        await ctx.send(f"❌ {error_msg}\n若问题持续，请联系机器人维护者。")
        logger.error(f"API Error: {error_msg} | Detail: {detail}")
    except Exception as e:
        logger.error(f"Failed to send error message: {e}")

async def handle_db_error(ctx, error_msg: str, exception: Exception):
    """处理数据库错误"""
    try:
        await ctx.send(f"❌ 数据库错误: {error_msg}")
        logger.error(f"DB Error: {error_msg} | Exception: {str(exception)}")
    except Exception as e:
        logger.error(f"Failed to handle DB error: {e}")

def log_command_error(ctx, command_name: str, exception: Exception):
    """记录命令执行错误"""
    logger.error(
        f"Command '{command_name}' failed for user {ctx.author.id}: "
        f"{type(exception).__name__}: {str(exception)}\n"
        f"Traceback:\n{traceback.format_exc()}"
    )

async def safe_database_operation(operation_name: str, operation_func, *args, **kwargs):
    """安全的数据库操作包装器
    
    Args:
        operation_name: 操作名称（用于日志）
        operation_func: 异步函数
        *args, **kwargs: 传递给函数的参数
        
    Returns:
        操作结果，失败返回 None
    """
    try:
        return await operation_func(*args, **kwargs)
    except Exception as e:
        logger.error(f"Database operation '{operation_name}' failed: {type(e).__name__}: {str(e)}")
        return None
