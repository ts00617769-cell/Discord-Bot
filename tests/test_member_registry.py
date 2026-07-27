"""成員雙向別名。"""
from __future__ import annotations

import pytest

from db.schema import apply_migrations
from services.member_registry import clear_member_identity, upsert_alias_links


@pytest.mark.asyncio
async def test_upsert_alias_links_bidirectional(tmp_path):
    import aiosqlite

    db_path = tmp_path / "reg.db"
    db = await aiosqlite.connect(str(db_path))
    try:
        await apply_migrations(db)
        identity = await upsert_alias_links(db, "現名", ["前身A", "前身B"])
        assert "前身A" in identity
        assert "前身B" in identity

        async with db.execute(
            "SELECT original_identity FROM member_registry WHERE player_name = ?",
            ("前身A",),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert "現名" in row[0]

        await clear_member_identity(db, "現名")
        async with db.execute(
            "SELECT 1 FROM member_registry WHERE player_name = ?",
            ("現名",),
        ) as cur:
            assert await cur.fetchone() is None
        async with db.execute(
            "SELECT original_identity FROM member_registry WHERE player_name = ?",
            ("前身A",),
        ) as cur:
            row = await cur.fetchone()
        if row:
            assert "現名" not in (row[0] or "")
    finally:
        await db.close()
