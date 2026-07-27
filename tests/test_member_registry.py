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


@pytest.mark.asyncio
async def test_clear_scrubs_stale_non_closure_rows(tmp_path):
    """舊資料未互指時，clear 仍應清掉殘留 token。"""
    db_path = tmp_path / "stale.db"
    db = await aiosqlite.connect(str(db_path))
    try:
        await apply_migrations(db)
        await db.execute(
            "INSERT INTO member_registry VALUES ('主名', '別名X')"
        )
        await db.execute(
            "INSERT INTO member_registry VALUES ('外人', '主名,別人')"
        )
        await db.commit()
        await clear_member_identity(db, "主名")
        assert "主名" not in await _identity_of(db, "外人")
        async with db.execute(
            "SELECT 1 FROM member_registry WHERE player_name='主名'"
        ) as cur:
            assert await cur.fetchone() is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_related_names_no_substring_false_positive(tmp_path):
    from services.player_search_db import PlayerSearchStore

    db_path = tmp_path / "like.db"
    db = await aiosqlite.connect(str(db_path))
    try:
        await apply_migrations(db)
        await db.execute(
            "INSERT INTO member_registry VALUES ('小碎', '別人')"
        )
        await db.execute(
            "INSERT INTO member_registry VALUES ('小碎冰', '前身')"
        )
        await db.commit()
        store = PlayerSearchStore(db)
        related = await store._get_related_names("小碎")
        assert "小碎" in related
        assert "別人" in related
        assert "小碎冰" not in related
    finally:
        await db.close()
