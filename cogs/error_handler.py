"""
例外处理和日志工具模块
用于统一处理异常和日志输出
"""
import logging
import traceback
import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

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
