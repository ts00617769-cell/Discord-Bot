"""超速警報去重：新表 alert_dedupe，相容舊 bot_settings overspeed:*。"""
from __future__ import annotations

KIND_OVERSPEED = "overspeed"

UPSERT_SQL = """
INSERT INTO alert_dedupe (kind, dedupe_key, created_at)
VALUES (?, ?, ?)
ON CONFLICT(kind, dedupe_key) DO UPDATE SET created_at=excluded.created_at
"""


async def overspeed_already_sent(db, key: str) -> bool:
    """先查 alert_dedupe，再回退 bot_settings。"""
    async with db.execute(
        "SELECT 1 FROM alert_dedupe WHERE kind = ? AND dedupe_key = ?",
        (KIND_OVERSPEED, key),
    ) as cursor:
        if await cursor.fetchone():
            return True
    async with db.execute(
        "SELECT 1 FROM bot_settings WHERE key = ?", (key,)
    ) as cursor:
        return await cursor.fetchone() is not None


async def mark_overspeed_sent(db, key: str, *, created_at: str) -> None:
    await db.execute(UPSERT_SQL, (KIND_OVERSPEED, key, created_at))
    await db.commit()
