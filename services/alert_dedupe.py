"""警報去重：通用 claim / already / release。"""
from __future__ import annotations

KIND_OVERSPEED = "overspeed"
KIND_BOSS_REMINDER = "boss_reminder"
KIND_TRANSFER = "transfer"


def transfer_channel_dedupe_key(
    pair_key: tuple[str, str, str, str], channel_id: int
) -> str:
    old_name, old_server, new_name, new_server = pair_key
    return (
        f"transfer:{old_name}|{old_server}|{new_name}|{new_server}"
        f"|channel:{channel_id}"
    )

CLAIM_SQL = """
INSERT OR IGNORE INTO alert_dedupe (kind, dedupe_key, created_at)
VALUES (?, ?, ?)
"""

RELEASE_SQL = "DELETE FROM alert_dedupe WHERE kind = ? AND dedupe_key = ?"


async def alert_already_sent(db, kind: str, key: str) -> bool:
    async with db.execute(
        "SELECT 1 FROM alert_dedupe WHERE kind = ? AND dedupe_key = ?",
        (kind, key),
    ) as cursor:
        return await cursor.fetchone() is not None


async def try_claim_alert(db, kind: str, key: str, *, created_at: str) -> bool:
    """INSERT OR IGNORE；成功取得 claim 才回 True。"""
    cursor = await db.execute(CLAIM_SQL, (kind, key, created_at))
    await db.commit()
    return (cursor.rowcount or 0) == 1


async def release_alert_claim(db, kind: str, key: str) -> None:
    await db.execute(RELEASE_SQL, (kind, key))
    await db.commit()
