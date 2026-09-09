"""SQLite locked/busy 判斷與重試（NAS 常見）。"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)

_NESTED_TXN = "cannot start a transaction within a transaction"


async def begin_immediate(db: Any) -> None:
    """開 IMMEDIATE 交易；若連線已有隱式／殘留交易則先 rollback 再重試。

    Python sqlite3 預設 isolation_level 會在 DML 時暗中 BEGIN。
    長跑共用寫入連線若沒 commit／rollback，下一輪 BEGIN IMMEDIATE 會炸巢狀交易。
    """
    try:
        await db.execute("BEGIN IMMEDIATE")
        return
    except sqlite3.OperationalError as e:
        if _NESTED_TXN not in str(e).lower():
            raise
        logger.warning("SQLite 連線已有未結束交易，rollback 後重開 BEGIN IMMEDIATE")
    await db.rollback()
    await db.execute("BEGIN IMMEDIATE")


def is_sqlite_busy(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.Error):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def execute_with_busy_retry(
    action: Callable[[], T],
    *,
    attempts: int,
    sleep_sec: float,
    on_retry: Callable[[BaseException, int], None] | None = None,
) -> T:
    """同步重試；用盡仍忙碌則拋最後一次例外。"""
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except sqlite3.OperationalError as e:
            last = e
            if not is_sqlite_busy(e) or attempt == attempts:
                raise
            if on_retry is not None:
                on_retry(e, attempt)
            time.sleep(sleep_sec)
    assert last is not None
    raise last


async def await_with_busy_retry(
    action: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    delay_for_attempt: Callable[[int], float],
    on_retry: Callable[[BaseException, int], None] | None = None,
) -> T:
    """非同步重試；attempt 從 1 起算。"""
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await action()
        except sqlite3.OperationalError as e:
            last = e
            if not is_sqlite_busy(e) or attempt == attempts:
                raise
            if on_retry is not None:
                on_retry(e, attempt)
            await asyncio.sleep(delay_for_attempt(attempt))
    assert last is not None
    raise last
