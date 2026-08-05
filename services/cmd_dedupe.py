"""指令去重：claim / release / prune（供 bot 與線上清庫共用）。"""
from __future__ import annotations

import logging
import os
import socket
import sqlite3
from typing import Any

from services.timeutil import now_naive_taipei, taipei_cutoff_str

logger = logging.getLogger(__name__)


def invoke_dedupe_id(ctx: Any) -> int | None:
    """prefix 用 message snowflake，slash/hybrid 用 interaction snowflake。"""
    interaction_id = getattr(getattr(ctx, "interaction", None), "id", None)
    if interaction_id is not None:
        return int(interaction_id)
    message = getattr(ctx, "message", None)
    message_id = getattr(message, "id", None)
    return int(message_id) if message_id is not None else None


async def try_claim_command(
    db,
    invoke_id: int,
    *,
    claimed_at: str | None = None,
    pid: int | None = None,
    host: str | None = None,
) -> bool:
    """INSERT cmd_dedupe；成功取得 claim 回 True，重複回 False。"""
    claimed_at = claimed_at or now_naive_taipei().strftime("%Y-%m-%d %H:%M:%S")
    pid = os.getpid() if pid is None else pid
    host = socket.gethostname() if host is None else host
    try:
        await db.execute(
            "INSERT INTO cmd_dedupe (message_id, claimed_at, pid, host) "
            "VALUES (?, ?, ?, ?)",
            (invoke_id, claimed_at, pid, host),
        )
        await db.commit()
        return True
    except sqlite3.IntegrityError:
        return False


async def release_command_claim(db, invoke_id: int) -> None:
    await db.execute("DELETE FROM cmd_dedupe WHERE message_id = ?", (invoke_id,))
    await db.commit()


async def prune_command_dedupe(db, *, days: int = 2) -> int:
    cutoff = taipei_cutoff_str(days)
    cursor = await db.execute(
        "DELETE FROM cmd_dedupe WHERE claimed_at < ?",
        (cutoff,),
    )
    await db.commit()
    return int(cursor.rowcount or 0)


def prune_command_dedupe_sync(conn, *, days: int = 2) -> int:
    cutoff = taipei_cutoff_str(days)
    cursor = conn.execute(
        "DELETE FROM cmd_dedupe WHERE claimed_at < ?",
        (cutoff,),
    )
    return int(cursor.rowcount or 0)
