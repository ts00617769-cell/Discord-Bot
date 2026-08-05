"""共享 SQLite bot 實例 lease。"""
from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from bot import PrasiaBot
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


@pytest.mark.asyncio
async def test_heartbeat_closes_after_consecutive_db_errors():
    bot = PrasiaBot.__new__(PrasiaBot)
    bot.instance_db = object()
    bot.instance_holder_id = "host:1"
    bot._heartbeat_fail_count = 0
    bot.close = AsyncMock()

    with patch(
        "bot.refresh_instance_lock",
        new_callable=AsyncMock,
        side_effect=sqlite3.DatabaseError("database is locked"),
    ):
        for _ in range(PrasiaBot.HEARTBEAT_FAIL_LIMIT):
            await PrasiaBot.instance_heartbeat.coro(bot)

    assert bot._heartbeat_fail_count >= PrasiaBot.HEARTBEAT_FAIL_LIMIT
    bot.close.assert_awaited()


@pytest.mark.asyncio
async def test_heartbeat_closes_immediately_when_lease_is_lost():
    bot = PrasiaBot.__new__(PrasiaBot)
    bot.instance_db = object()
    bot.instance_holder_id = "host:1"
    bot._heartbeat_fail_count = 0
    bot.close = AsyncMock()

    with patch(
        "bot.refresh_instance_lock", new_callable=AsyncMock, return_value=False
    ):
        await PrasiaBot.instance_heartbeat.coro(bot)

    bot.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_heartbeat_unexpected_error_fails_closed():
    bot = PrasiaBot.__new__(PrasiaBot)
    bot.instance_db = object()
    bot.instance_holder_id = "host:1"
    bot._heartbeat_fail_count = 0
    bot.close = AsyncMock()

    with patch(
        "bot.refresh_instance_lock",
        new_callable=AsyncMock,
        side_effect=RuntimeError("unexpected"),
    ):
        await PrasiaBot.instance_heartbeat.coro(bot)

    bot.close.assert_awaited_once()
