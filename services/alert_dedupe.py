"""警報去重：通用 claim / already / release；相容舊 bot_settings。"""
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

UPSERT_SQL = """
INSERT INTO alert_dedupe (kind, dedupe_key, created_at)
VALUES (?, ?, ?)
ON CONFLICT(kind, dedupe_key) DO UPDATE SET created_at=excluded.created_at
"""

RELEASE_SQL = "DELETE FROM alert_dedupe WHERE kind = ? AND dedupe_key = ?"


async def alert_already_sent(
    db,
    kind: str,
    key: str,
    *,
    check_legacy_settings: bool = True,
) -> bool:
    """先查 alert_dedupe，可選回退 bot_settings。"""
    async with db.execute(
        "SELECT 1 FROM alert_dedupe WHERE kind = ? AND dedupe_key = ?",
        (kind, key),
    ) as cursor:
        if await cursor.fetchone():
            return True
    if not check_legacy_settings:
        return False
    async with db.execute(
        "SELECT 1 FROM bot_settings WHERE key = ?", (key,)
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


async def overspeed_already_sent(db, key: str) -> bool:
    """相容舊呼叫：等同 alert_already_sent(overspeed)。"""
    return await alert_already_sent(db, KIND_OVERSPEED, key)


async def mark_overspeed_sent(db, key: str, *, created_at: str) -> None:
    """相容舊呼叫（UPSERT）；新路徑請用 try_claim_alert。"""
    await db.execute(UPSERT_SQL, (KIND_OVERSPEED, key, created_at))
    await db.commit()
