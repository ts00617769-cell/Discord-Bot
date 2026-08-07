"""共享 SQLite bot 實例 lease。"""
from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import PrasiaBot
from db.instance_lock import (
    get_live_instance_holder,
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


def test_get_live_instance_holder_sync(tmp_path):
    path = tmp_path / "live.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE bot_instance_lock (
                lock_name TEXT PRIMARY KEY,
                holder_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL
            );
            INSERT INTO bot_instance_lock
            VALUES ('primary', 'nas:42', '2026-08-06 08:00:00',
                    datetime('now', 'localtime'));
            """
        )
        conn.commit()
        live = get_live_instance_holder(conn, ttl_seconds=120)
        assert live is not None
        assert live[0] == "nas:42"

        conn.execute(
            "UPDATE bot_instance_lock SET heartbeat_at='2000-01-01 00:00:00'"
        )
        conn.commit()
        assert get_live_instance_holder(conn, ttl_seconds=120) is None
    finally:
        conn.close()


def test_get_live_instance_holder_treats_invalid_timestamp_as_stale(tmp_path):
    path = tmp_path / "invalid-heartbeat.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE bot_instance_lock (
                lock_name TEXT PRIMARY KEY,
                holder_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL
            );
            INSERT INTO bot_instance_lock
            VALUES ('primary', 'broken:1', '2026-08-06 08:00:00', 'not-a-time');
            """
        )
        conn.commit()
        assert get_live_instance_holder(conn, ttl_seconds=120) is None
    finally:
        conn.close()


def test_refuse_if_bot_running_blocks_remote_without_force(tmp_path, monkeypatch):
    import cleanup_db

    monkeypatch.setattr(cleanup_db, "_bot_appears_running", lambda: False)
    path = tmp_path / "remote.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE bot_instance_lock (
                lock_name TEXT PRIMARY KEY,
                holder_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL
            );
            INSERT INTO bot_instance_lock
            VALUES ('primary', 'other-host:9', '2026-08-06 08:00:00',
                    datetime('now', 'localtime'));
            """
        )
        conn.commit()
        assert cleanup_db._refuse_if_bot_running(conn, force=False) == 1
        assert cleanup_db._refuse_if_bot_running(conn, force=True) is None
    finally:
        conn.close()


def _heartbeat_bot(*, last_ok: float = 1000.0) -> PrasiaBot:
    bot = PrasiaBot.__new__(PrasiaBot)
    bot.db = object()
    bot.instance_holder_id = "host:1"
    bot.db_write_lock = asyncio.Lock()
    bot._last_heartbeat_ok = last_ok
    bot.fatal_shutdown = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_heartbeat_skips_when_write_lock_held():
    bot = _heartbeat_bot()
    await bot.db_write_lock.acquire()
    try:
        with patch(
            "bot.refresh_instance_lock", new_callable=AsyncMock
        ) as refresh:
            await PrasiaBot.instance_heartbeat.coro(bot)
        refresh.assert_not_awaited()
        bot.fatal_shutdown.assert_not_awaited()
    finally:
        bot.db_write_lock.release()


@pytest.mark.asyncio
async def test_heartbeat_closes_after_grace_on_db_errors():
    bot = _heartbeat_bot(last_ok=1000.0)
    bot.LEASE_GRACE_SECONDS = 600

    class _Loop:
        def __init__(self) -> None:
            self._t = 1000.0

        def time(self) -> float:
            return self._t

    loop = _Loop()

    with (
        patch(
            "bot.refresh_instance_lock",
            new_callable=AsyncMock,
            side_effect=sqlite3.DatabaseError("database is locked"),
        ),
        patch("bot.asyncio.get_running_loop", return_value=loop),
    ):
        await PrasiaBot.instance_heartbeat.coro(bot)
        bot.fatal_shutdown.assert_not_awaited()

        loop._t = 1000.0 + 599.0
        await PrasiaBot.instance_heartbeat.coro(bot)
        bot.fatal_shutdown.assert_not_awaited()

        loop._t = 1000.0 + 600.0
        await PrasiaBot.instance_heartbeat.coro(bot)
        bot.fatal_shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_heartbeat_recovers_after_transient_db_errors():
    bot = _heartbeat_bot(last_ok=1000.0)
    bot.LEASE_GRACE_SECONDS = 600

    class _Loop:
        def __init__(self) -> None:
            self._t = 1000.0

        def time(self) -> float:
            return self._t

    loop = _Loop()

    with (
        patch(
            "bot.refresh_instance_lock",
            new_callable=AsyncMock,
            side_effect=[
                sqlite3.DatabaseError("database is locked"),
                sqlite3.DatabaseError("database is locked"),
                True,
            ],
        ),
        patch("bot.asyncio.get_running_loop", return_value=loop),
    ):
        loop._t = 1100.0
        await PrasiaBot.instance_heartbeat.coro(bot)
        loop._t = 1200.0
        await PrasiaBot.instance_heartbeat.coro(bot)
        loop._t = 1300.0
        await PrasiaBot.instance_heartbeat.coro(bot)

    assert bot._last_heartbeat_ok == 1300.0
    bot.fatal_shutdown.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_instance_lock_retries_busy_then_succeeds():
    db = AsyncMock()
    cursor = MagicMock()
    cursor.rowcount = 1
    db.execute = AsyncMock(
        side_effect=[
            sqlite3.OperationalError("database is locked"),
            sqlite3.OperationalError("database is locked"),
            cursor,
        ]
    )
    db.commit = AsyncMock()

    with patch("db.instance_lock.asyncio.sleep", new_callable=AsyncMock):
        assert await refresh_instance_lock(db, "host:1") is True

    assert db.execute.await_count == 3
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_heartbeat_closes_immediately_when_lease_is_lost():
    bot = _heartbeat_bot()

    with patch(
        "bot.refresh_instance_lock", new_callable=AsyncMock, return_value=False
    ):
        await PrasiaBot.instance_heartbeat.coro(bot)

    bot.fatal_shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_heartbeat_unexpected_error_fails_closed():
    bot = _heartbeat_bot()

    with patch(
        "bot.refresh_instance_lock",
        new_callable=AsyncMock,
        side_effect=RuntimeError("unexpected"),
    ):
        await PrasiaBot.instance_heartbeat.coro(bot)

    bot.fatal_shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_fatal_shutdown_releases_lease_and_hard_exits_once():
    bot = PrasiaBot.__new__(PrasiaBot)
    bot._fatal_shutdown_started = False
    bot.db = object()
    bot.instance_holder_id = "host:1"

    with (
        patch(
            "bot.release_instance_lock", new_callable=AsyncMock
        ) as release_instance_lock,
        patch("bot.logging.shutdown") as shutdown,
        patch("bot.os._exit") as hard_exit,
    ):
        await bot.fatal_shutdown("lease lost")
        await bot.fatal_shutdown("duplicate")

    release_instance_lock.assert_awaited_once_with(bot.db, "host:1")
    shutdown.assert_called_once()
    hard_exit.assert_called_once_with(1)
