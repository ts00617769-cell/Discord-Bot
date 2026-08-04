"""共享 SQLite bot 實例 lease。"""
from __future__ import annotations

import pytest

from db.instance_lock import (
    refresh_instance_lock,
    release_instance_lock,
    try_acquire_instance_lock,
)
from db.schema import apply_migrations


@pytest.mark.asyncio
async def test_instance_lock_blocks_second_holder_and_releases(tmp_path):
    import aiosqlite

    path = tmp_path / "lock.db"
    db1 = await aiosqlite.connect(path)
    db2 = await aiosqlite.connect(path)
    try:
        await apply_migrations(db1)
        assert await try_acquire_instance_lock(db1, "host-a:1") is True
        assert await try_acquire_instance_lock(db2, "host-b:2") is False
        assert await refresh_instance_lock(db1, "host-a:1") is True
        assert await refresh_instance_lock(db2, "host-b:2") is False
        await release_instance_lock(db1, "host-a:1")
        assert await try_acquire_instance_lock(db2, "host-b:2") is True
    finally:
        await db1.close()
        await db2.close()


@pytest.mark.asyncio
async def test_instance_lock_allows_stale_takeover(tmp_path):
    import aiosqlite

    db = await aiosqlite.connect(tmp_path / "stale.db")
    try:
        await apply_migrations(db)
        assert await try_acquire_instance_lock(db, "old") is True
        await db.execute(
            "UPDATE bot_instance_lock SET heartbeat_at='2000-01-01 00:00:00'"
        )
        await db.commit()
        assert await try_acquire_instance_lock(
            db, "new", ttl_seconds=1
        ) is True
    finally:
        await db.close()
