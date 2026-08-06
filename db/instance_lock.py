"""以共享 SQLite heartbeat 阻擋跨主機重複 bot 實例。"""
from __future__ import annotations

import datetime
import os
import socket
import sqlite3

from services.timeutil import FMT_SQL, now_naive_taipei

LOCK_NAME = "primary"


def make_holder_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def try_acquire_instance_lock(
    db,
    holder_id: str,
    *,
    ttl_seconds: int = 120,
) -> bool:
    now = now_naive_taipei()
    now_s = now.strftime(FMT_SQL)
    stale_s = (now - datetime.timedelta(seconds=ttl_seconds)).strftime(FMT_SQL)
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
    except Exception:
        await db.rollback()
        raise


async def refresh_instance_lock(db, holder_id: str) -> bool:
    cursor = await db.execute(
        """
        UPDATE bot_instance_lock SET heartbeat_at = ?
        WHERE lock_name = ? AND holder_id = ?
        """,
        (now_naive_taipei().strftime(FMT_SQL), LOCK_NAME, holder_id),
    )
    await db.commit()
    return cursor.rowcount == 1


async def release_instance_lock(db, holder_id: str) -> None:
    await db.execute(
        "DELETE FROM bot_instance_lock WHERE lock_name = ? AND holder_id = ?",
        (LOCK_NAME, holder_id),
    )
    await db.commit()


def get_live_instance_holder(
    conn,
    *,
    ttl_seconds: int = 120,
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
        return (str(holder_id), str(heartbeat_at))
    now = now_naive_taipei()
    if hb.tzinfo is not None:
        hb = hb.replace(tzinfo=None)
    if (now - hb).total_seconds() > ttl_seconds:
        return None
    return (str(holder_id), str(heartbeat_at))
