"""以共享 SQLite heartbeat 阻擋跨主機重複 bot 實例。"""
from __future__ import annotations

import asyncio
import datetime
import logging
import os
import socket
import sqlite3

from services.timeutil import FMT_SQL, now_naive_taipei

logger = logging.getLogger(__name__)

LOCK_NAME = "primary"
# 與 bot LEASE_GRACE 對齊：大快照／分批 DELETE 期間允許 heartbeat 延遲，避免誤判過期
INSTANCE_LOCK_TTL_SECONDS = 600
BUSY_RETRY_ATTEMPTS = 8
BUSY_RETRY_BASE_DELAY = 0.25


def make_holder_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _is_busy_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


async def _sleep_busy_backoff(attempt: int) -> None:
    await asyncio.sleep(min(BUSY_RETRY_BASE_DELAY * (2**attempt), 10.0))


async def try_acquire_instance_lock(
    db,
    holder_id: str,
    *,
    ttl_seconds: int = INSTANCE_LOCK_TTL_SECONDS,
) -> bool:
    now = now_naive_taipei()
    now_s = now.strftime(FMT_SQL)
    stale_s = (now - datetime.timedelta(seconds=ttl_seconds)).strftime(FMT_SQL)
    last: BaseException | None = None
    for attempt in range(BUSY_RETRY_ATTEMPTS):
        try:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                INSERT OR IGNORE INTO bot_instance_lock
                (lock_name, holder_id, acquired_at, heartbeat_at)
                VALUES (?, ?, ?, ?)
                """,
                (LOCK_NAME, holder_id, now_s, now_s),
            )
            await db.execute(
                """
                UPDATE bot_instance_lock
                SET holder_id = ?,
                    acquired_at = CASE WHEN holder_id = ? THEN acquired_at ELSE ? END,
                    heartbeat_at = ?
                WHERE lock_name = ?
                  AND (holder_id = ? OR heartbeat_at < ?)
                """,
                (
                    holder_id,
                    holder_id,
                    now_s,
                    now_s,
                    LOCK_NAME,
                    holder_id,
                    stale_s,
                ),
            )
            async with db.execute(
                "SELECT holder_id FROM bot_instance_lock WHERE lock_name = ?",
                (LOCK_NAME,),
            ) as cursor:
                row = await cursor.fetchone()
            await db.commit()
            return bool(row and row[0] == holder_id)
        except Exception as e:
            last = e
            try:
                await db.rollback()
            except Exception:
                pass
            if _is_busy_error(e) and attempt < BUSY_RETRY_ATTEMPTS - 1:
                logger.warning(
                    "取得實例鎖忙碌（%s），重試 %s/%s…",
                    e,
                    attempt + 1,
                    BUSY_RETRY_ATTEMPTS,
                )
                await _sleep_busy_backoff(attempt)
                continue
            raise
    assert last is not None
    raise last


async def refresh_instance_lock(db, holder_id: str) -> bool:
    last: BaseException | None = None
    for attempt in range(BUSY_RETRY_ATTEMPTS):
        try:
            cursor = await db.execute(
                """
                UPDATE bot_instance_lock SET heartbeat_at = ?
                WHERE lock_name = ? AND holder_id = ?
                """,
                (now_naive_taipei().strftime(FMT_SQL), LOCK_NAME, holder_id),
            )
            await db.commit()
            return cursor.rowcount == 1
        except sqlite3.DatabaseError as e:
            last = e
            if not _is_busy_error(e) or attempt == BUSY_RETRY_ATTEMPTS - 1:
                raise
            logger.warning(
                "更新實例 heartbeat 忙碌（%s），重試 %s/%s…",
                e,
                attempt + 1,
                BUSY_RETRY_ATTEMPTS,
            )
            await _sleep_busy_backoff(attempt)
    assert last is not None
    raise last


async def release_instance_lock(db, holder_id: str) -> None:
    last: BaseException | None = None
    for attempt in range(BUSY_RETRY_ATTEMPTS):
        try:
            await db.execute(
                "DELETE FROM bot_instance_lock WHERE lock_name = ? AND holder_id = ?",
                (LOCK_NAME, holder_id),
            )
            await db.commit()
            return
        except sqlite3.DatabaseError as e:
            last = e
            if not _is_busy_error(e) or attempt == BUSY_RETRY_ATTEMPTS - 1:
                raise
            logger.warning(
                "釋放實例鎖忙碌（%s），重試 %s/%s…",
                e,
                attempt + 1,
                BUSY_RETRY_ATTEMPTS,
            )
            await _sleep_busy_backoff(attempt)
    assert last is not None
    raise last


def get_live_instance_holder(
    conn,
    *,
    ttl_seconds: int = INSTANCE_LOCK_TTL_SECONDS,
) -> tuple[str, str] | None:
    """同步查詢：若有未過期的 bot_instance_lock，回傳 (holder_id, heartbeat_at)。

    供 cleanup_db 等離線工具阻擋「他機 bot 仍在寫同一 DB」的互撞。
    """
    try:
        row = conn.execute(
            """
            SELECT holder_id, heartbeat_at
            FROM bot_instance_lock
            WHERE lock_name = ?
            """,
            (LOCK_NAME,),
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    if not row:
        return None
    holder_id, heartbeat_at = row[0], row[1]
    if not holder_id or not heartbeat_at:
        return None
    try:
        hb = datetime.datetime.strptime(str(heartbeat_at), FMT_SQL)
    except ValueError:
        # 無法解析的時間戳不能永久佔住 lease；視為損壞且已過期。
        return None
    now = now_naive_taipei()
    if hb.tzinfo is not None:
        hb = hb.replace(tzinfo=None)
    if (now - hb).total_seconds() > ttl_seconds:
        return None
    return (str(holder_id), str(heartbeat_at))
