"""選用寫入鎖的共用包裝（bot.db_write_lock 在測試中常為 None）。"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")


async def run_locked(
    lock: Any | None,
    func: Callable[..., Awaitable[T]],
    *args: Any,
    **kwargs: Any,
) -> T:
    """有鎖就在鎖內執行，沒有就直接執行。"""
    if lock is None:
        return await func(*args, **kwargs)
    async with lock:
        return await func(*args, **kwargs)
