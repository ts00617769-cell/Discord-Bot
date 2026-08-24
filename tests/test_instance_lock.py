"""本機單例鎖、清庫拒絕、fatal shutdown。"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from bot import PrasiaBot
from db.singleton_lock import is_process_lock_held
from services.sqlite_busy import is_sqlite_busy


def test_is_process_lock_held_missing_file(tmp_path):
    assert is_process_lock_held(tmp_path / "missing.lock") is False


def test_refuse_if_bot_running_blocks_local_without_force(monkeypatch):
    import cleanup_db

    monkeypatch.setattr(cleanup_db, "_bot_appears_running", lambda: True)
    assert cleanup_db._refuse_if_bot_running(force=False) == 1
    assert cleanup_db._refuse_if_bot_running(force=True) is None


def test_refuse_if_bot_running_allows_when_idle(monkeypatch):
    import cleanup_db

    monkeypatch.setattr(cleanup_db, "_bot_appears_running", lambda: False)
    assert cleanup_db._refuse_if_bot_running(force=False) is None


def test_is_sqlite_busy():
    import sqlite3

    assert is_sqlite_busy(sqlite3.OperationalError("database is locked"))
    assert is_sqlite_busy(sqlite3.OperationalError("database is busy"))
    assert not is_sqlite_busy(sqlite3.OperationalError("no such table"))
    assert not is_sqlite_busy(ValueError("locked"))


@pytest.mark.asyncio
async def test_fatal_shutdown_hard_exits_once():
    bot = PrasiaBot.__new__(PrasiaBot)
    bot._fatal_shutdown_started = False

    with (
        patch("bot.logging.shutdown") as shutdown,
        patch("bot.os._exit") as hard_exit,
    ):
        await bot.fatal_shutdown("session closed")
        await bot.fatal_shutdown("duplicate")

    shutdown.assert_called_once()
    hard_exit.assert_called_once_with(1)
