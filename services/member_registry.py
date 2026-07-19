"""成員登錄查詢（Discord 無關）。"""
from __future__ import annotations

from services.error_handler import safe_database_operation


async def get_member_tag(db, name: str) -> str:
    """回傳 `({original_identity})` 或空字串。"""

    async def _query():
        async with db.execute(
            "SELECT original_identity FROM member_registry WHERE player_name = ?",
            (name,),
        ) as cursor:
            result = await cursor.fetchone()
            return f"({result[0]})" if result else ""

    tag = await safe_database_operation(f"member_tag:{name}", _query)
    return tag or ""
