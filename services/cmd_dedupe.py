"""指令去重：claim / prune（供 bot 與線上清庫共用）。"""
from __future__ import annotations

from services.timeutil import taipei_cutoff_str


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
