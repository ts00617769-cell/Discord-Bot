"""WarRoom 線上清庫：retention plan + secondary prune。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from cogs.war_room import WarRoom
from db.schema import apply_migrations
from services.retention_cleanup import build_retention_plan, prune_secondary_async
from services.search_cache import invalidate_player_search_cache


def test_invalidate_player_search_cache_clears_entries():
    from services import search_cache

    search_cache.set_cached_search("k", {"kind": "text", "content": "x"})
    assert search_cache.get_cached_search("k") is not None
    search_cache.invalidate_player_search_cache()
    assert search_cache.get_cached_search("k") is None


@pytest.mark.asyncio
async def test_prune_secondary_async_removes_old_rows(tmp_path):
    db = await aiosqlite.connect(str(tmp_path / "ret.db"))
    try:
        await apply_migrations(db)
        await db.execute(
            """
            INSERT INTO transfer_alerts_log
            (old_name, old_server, new_name, new_server, alert_time)
            VALUES ('a','s1','b','s2','2020-01-01 00:00:00')
            """
        )
        await db.execute(
            """
            INSERT INTO alert_dedupe (kind, dedupe_key, created_at)
            VALUES ('overspeed', 'old-key', '2020-01-01 00:00:00')
            """
        )
        await db.execute(
            "INSERT INTO cmd_dedupe (message_id, claimed_at, pid, host) VALUES (1, '2020-01-01 00:00:00', 1, 'h')"
        )
        await db.commit()

        plan = build_retention_plan()
        stats = await prune_secondary_async(db, plan)
        assert stats.deleted_transfer >= 1
        assert stats.deleted_alert_dedupe >= 1
        assert stats.deleted_cmd_dedupe >= 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_war_room_cleanup_uses_retention_plan():
    bot = MagicMock()
    bot.db = MagicMock()
    bot.db_write_lock = asyncio.Lock()
    bot.db.execute = AsyncMock()
    bot.db.commit = AsyncMock()
    bot.db.rollback = AsyncMock()

    cog = WarRoom(bot)
    cog._delete_exp_history_in_batches = AsyncMock(return_value=0)

    plan = build_retention_plan()
    secondary = SimpleNamespace(
        deleted_transfer=0,
        deleted_settings=0,
        deleted_alert_dedupe=0,
        deleted_cmd_dedupe=0,
    )
    with patch(
        "cogs.war_room.build_retention_plan", return_value=plan
    ), patch(
        "cogs.war_room.outside_keep_batch", return_value=("DELETE 1", ())
    ), patch(
        "cogs.war_room.thin_range_batches", return_value=[]
    ), patch(
        "cogs.war_room.prune_secondary_async",
        new_callable=AsyncMock,
        return_value=secondary,
    ) as prune, patch(
        "cogs.war_room.invalidate_player_search_cache"
    ), patch(
        "cogs.war_room.parse_env_channel_id", return_value=0
    ):
        await WarRoom.db_cleanup_task.coro(cog)

    prune.assert_awaited_once()
    cog._delete_exp_history_in_batches.assert_awaited()
