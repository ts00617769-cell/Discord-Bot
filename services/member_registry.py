"""成員登錄查詢（Discord 無關）。"""
from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)


async def get_member_tag(db, name: str) -> str:
    """回傳 `({original_identity})` 或空字串。"""
    try:
        async with db.execute(
            "SELECT original_identity FROM member_registry WHERE player_name = ?",
            (name,),
        ) as cursor:
            result = await cursor.fetchone()
            return f"({result[0]})" if result else ""
    except sqlite3.DatabaseError as e:
        logger.error(f"DB error fetching member info for '{name}': {e}")
        return ""
