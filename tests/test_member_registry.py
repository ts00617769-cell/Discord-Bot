"""成員雙向別名。"""
from __future__ import annotations

import aiosqlite
import pytest

from db.schema import apply_migrations
from services.member_registry import clear_member_identity, upsert_alias_links


async def _identity_of(db, name: str) -> set[str]:
    async with db.execute(
        "SELECT original_identity FROM member_registry WHERE player_name = ?",
        (name,),
    ) as cur:
        row = await cur.fetchone()
    if not row or not row[0]:
        return set()
    return {x.strip() for x in row[0].split(",") if x.strip()}


@pytest.mark.asyncio
async def test_upsert_alias_links_bidirectional(tmp_path):
    db_path = tmp_path / "reg.db"
    db = await aiosqlite.connect(str(db_path))
    try:
        await apply_migrations(db)
        identity = await upsert_alias_links(db, "現名", ["前身A", "前身B"])
        assert "前身A" in identity
        assert "前身B" in identity

        assert "現名" in await _identity_of(db, "前身A")
        assert "前身B" in await _identity_of(db, "前身A")
        assert "前身A" in await _identity_of(db, "前身B")

        await clear_member_identity(db, "現名")
        async with db.execute(
            "SELECT 1 FROM member_registry WHERE player_name = ?",
            ("現名",),
        ) as cur:
            assert await cur.fetchone() is None
        assert "前身A" in await _identity_of(db, "前身B")
        assert "前身B" in await _identity_of(db, "前身A")
        assert "現名" not in await _identity_of(db, "前身A")
    finally:
        await db.close()
