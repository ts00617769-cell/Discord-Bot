"""Discord send 重試、轉服 per-channel claim、警報編排整合。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import discord
import pytest

from db.connection import connect_db, connect_db_ro
from db.schema import apply_migrations
from game_data import GAP_BOSS_SCHEDULE
from services.alert_dedupe import (
    KIND_OVERSPEED,
    KIND_TRANSFER,
    alert_already_sent,
    transfer_channel_dedupe_key,
)
from services.boss_reminder import find_pending_boss_hour
from services.discord_send import (
    chunk_embeds,
    send_embeds_to_channels,
    send_to_channels,
)
from services.overspeed_alerts import run_overspeed_patrol
from services.retention_cleanup import RetentionPlan, prune_secondary_async
from services.transfer_alert_runner import run_transfer_check


@pytest.mark.asyncio
async def test_discord_send_retries_retryable_http():
    bot = MagicMock()
    channel = MagicMock()
    response = MagicMock(status=503)
    calls = {"n": 0}

    async def flaky(_ch):
        calls["n"] += 1
        if calls["n"] < 3:
            raise discord.HTTPException(response, "fail")

    bot.get_channel.return_value = channel
    with patch("services.discord_send.asyncio.sleep", new_callable=AsyncMock):
        sent = await send_to_channels(
            bot, [1], send_fn=flaky, label="test", max_retries=3
        )
    assert sent == {1}
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_discord_send_does_not_retry_client_errors():
    bot = MagicMock()
    channel = MagicMock()
    response = MagicMock(status=400)
    calls = {"n": 0}

    async def bad(_ch):
        calls["n"] += 1
        raise discord.HTTPException(response, "bad request")

    bot.get_channel.return_value = channel
    sent = await send_to_channels(bot, [1], send_fn=bad, label="test", max_retries=3)
    assert sent == set()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_transfer_partial_channel_failure_retries_failed_only(tmp_path):
    db = await aiosqlite.connect(str(tmp_path / "xfer_partial.db"))
    try:
        await apply_migrations(db)
        pair = {
            "pair_key": ("Old", "S1", "New", "S2"),
            "new_name": "New",
            "new_server": "S2",
            "old_name": "Old",
            "old_server": "S1",
            "new_lvl": 60,
            "new_cls": "戰士",
            "new_sub_grade": 1,
            "status": "疑似改名",
            "exp_diff": 0,
            "old_guild": "",
            "new_guild": "",
        }
        row = (
            2e12,
            "New",
            "S2",
            60,
            "戰士",
            "",
            "Old",
            "S1",
            60,
            "戰士",
            2e12,
            "",
            1,
            1,
            "2026-08-05 11:50:00",
        )
        send = AsyncMock(return_value={1})  # channel 2 fails

        with patch(
            "services.transfer_alert_runner.is_transfer_active_period",
            return_value=False,
        ), patch(
            "services.transfer_alert_runner.rank_transfer_candidates",
            return_value=[row],
        ), patch(
            "services.transfer_alert_runner.lookup_alerted_pairs",
            new_callable=AsyncMock,
            return_value=set(),
        ), patch(
            "services.transfer_alert_runner.filter_viable_ranked",
            new_callable=AsyncMock,
            return_value=[row],
        ), patch(
            "services.transfer_alert_runner.pick_unique_pairs", return_value=[pair]
        ), patch(
            "services.transfer_alert_runner.prune_stale_missing", new_callable=AsyncMock
        ), patch(
            "services.transfer_alert_runner.fetch_potential_transfers",
            new_callable=AsyncMock,
            return_value=[row],
        ):
            await run_transfer_check(
                write_db=db,
                read_db=db,
                time_now="2026-08-05 12:00:00",
                time_prev="2026-08-05 11:50:00",
                channel_ids=[1, 2],
                send_alert=send,
            )

        k1 = transfer_channel_dedupe_key(("Old", "S1", "New", "S2"), 1)
        k2 = transfer_channel_dedupe_key(("Old", "S1", "New", "S2"), 2)
        assert await alert_already_sent(
            db, KIND_TRANSFER, k1, check_legacy_settings=False
        )
        assert not await alert_already_sent(
            db, KIND_TRANSFER, k2, check_legacy_settings=False
        )
        async with db.execute(
            "SELECT 1 FROM transfer_alerts_log WHERE old_name='Old'"
        ) as cur:
            assert await cur.fetchone() is None

        # 第二次：只應再 claim 失敗的頻道 2
        send2 = AsyncMock(return_value={2})
        with patch(
            "services.transfer_alert_runner.is_transfer_active_period",
            return_value=False,
        ), patch(
            "services.transfer_alert_runner.rank_transfer_candidates",
            return_value=[row],
        ), patch(
            "services.transfer_alert_runner.lookup_alerted_pairs",
            new_callable=AsyncMock,
            return_value=set(),
        ), patch(
            "services.transfer_alert_runner.filter_viable_ranked",
            new_callable=AsyncMock,
            return_value=[row],
        ), patch(
            "services.transfer_alert_runner.pick_unique_pairs", return_value=[pair]
        ), patch(
            "services.transfer_alert_runner.prune_stale_missing", new_callable=AsyncMock
        ), patch(
            "services.transfer_alert_runner.fetch_potential_transfers",
            new_callable=AsyncMock,
            return_value=[row],
        ):
            await run_transfer_check(
                write_db=db,
                read_db=db,
                time_now="2026-08-05 12:00:00",
                time_prev="2026-08-05 11:50:00",
                channel_ids=[1, 2],
                send_alert=send2,
            )

        assert send2.await_args.kwargs["channel_ids"] == [2]
        assert await alert_already_sent(
            db, KIND_TRANSFER, k2, check_legacy_settings=False
        )
        async with db.execute(
            "SELECT 1 FROM transfer_alerts_log WHERE old_name='Old'"
        ) as cur:
            assert await cur.fetchone() is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_overspeed_patrol_happy_path_with_sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("EXP_ALERT_SEND_CLEAR", "1")
    db = await aiosqlite.connect(str(tmp_path / "os_int.db"))
    try:
        await apply_migrations(db)
        t_now, t_prev = "2026-08-05 12:10:00", "2026-08-05 12:00:00"
        await db.executemany(
            """
            INSERT INTO exp_history
            (record_time, player_name, server_name, level, exp, class_name,
             subjugation_grade, guild_name)
            VALUES (?, ?, ?, ?, ?, '戰士', 1, ?)
            """,
            [
                (t_now, "Fast", "萊涅01", 90, 5e12, "狼團"),
                (t_prev, "Fast", "萊涅01", 90, 1e12, "狼團"),
            ],
        )
        await db.commit()
        bot = MagicMock()
        settings = SimpleNamespace(
            alerts_enabled=True,
            alert_server="萊涅01",
            alert_guild="狼團",
            alert_count=30,
            alert_interval_minutes=10,
            alert_speed_window_minutes=10,
            SPEED_LIMIT=2000.0,
            ALERT_CHANNEL_IDS=[42],
        )
        import datetime

        with patch(
            "services.overspeed_alerts.send_embeds_to_channels",
            new_callable=AsyncMock,
            return_value={42},
        ) as send:
            await run_overspeed_patrol(
                bot,
                settings,
                read_db=db,
                write_db=db,
                times=[(t_now,), (t_prev,)],
                current_time=datetime.datetime(2026, 8, 5, 12, 10, 0),
            )
        send.assert_awaited_once()
        embeds = send.await_args.args[2]
        assert embeds and "超速" in embeds[0].title
    finally:
        await db.close()


def test_chunk_embeds_packs_into_single_message():
    embeds = [discord.Embed(title="t", description="x" * 100) for _ in range(3)]
    assert len(chunk_embeds(embeds)) == 1


def test_chunk_embeds_splits_on_count_and_length():
    small = [discord.Embed(title=str(i)) for i in range(23)]
    assert [len(b) for b in chunk_embeds(small)] == [10, 10, 3]

    big = [discord.Embed(title="t", description="x" * 3000) for _ in range(3)]
    assert [len(b) for b in chunk_embeds(big)] == [1, 1, 1]


@pytest.mark.asyncio
async def test_send_embeds_partial_batches_not_marked_failed():
    """前批已送達、後批失敗時不得釋放 claim，否則重試會重複貼文。"""
    bot = MagicMock()
    channel = MagicMock()
    response = MagicMock(status=500)
    calls = {"n": 0}

    async def send(**_kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise discord.HTTPException(response, "fail")

    channel.send = AsyncMock(side_effect=send)
    bot.get_channel.return_value = channel
    embeds = [discord.Embed(title=str(i)) for i in range(11)]

    with patch("services.discord_send.asyncio.sleep", new_callable=AsyncMock):
        delivered = await send_embeds_to_channels(bot, [1], embeds, label="t")

    assert delivered == {1}


@pytest.mark.asyncio
async def test_send_embeds_first_batch_failure_marks_channel_failed():
    bot = MagicMock()
    channel = MagicMock()
    response = MagicMock(status=500)
    channel.send = AsyncMock(side_effect=discord.HTTPException(response, "fail"))
    bot.get_channel.return_value = channel

    with patch("services.discord_send.asyncio.sleep", new_callable=AsyncMock):
        delivered = await send_embeds_to_channels(
            bot, [1], [discord.Embed(title="t")], label="t"
        )

    assert delivered == set()


@pytest.mark.asyncio
async def test_overspeed_no_records_dedupes_hourly(tmp_path, monkeypatch):
    monkeypatch.delenv("EXP_ALERT_SEND_CLEAR", raising=False)
    db = await aiosqlite.connect(str(tmp_path / "os_none.db"))
    try:
        await apply_migrations(db)
        bot = MagicMock()
        settings = SimpleNamespace(
            alerts_enabled=True,
            alert_server="萊涅01",
            alert_guild="狼團",
            alert_count=30,
            alert_interval_minutes=10,
            alert_speed_window_minutes=10,
            SPEED_LIMIT=2000.0,
            ALERT_CHANNEL_IDS=[42],
        )
        import datetime

        with patch(
            "services.overspeed_alerts.send_embeds_to_channels",
            new_callable=AsyncMock,
            return_value={42},
        ) as send:
            await run_overspeed_patrol(
                bot,
                settings,
                read_db=db,
                write_db=db,
                times=[("2026-08-05 12:10:00",), ("2026-08-05 12:00:00",)],
                current_time=datetime.datetime(2026, 8, 5, 12, 10, 0),
            )
            # 同一小時的下一輪不應再送
            await run_overspeed_patrol(
                bot,
                settings,
                read_db=db,
                write_db=db,
                times=[("2026-08-05 12:20:00",), ("2026-08-05 12:10:00",)],
                current_time=datetime.datetime(2026, 8, 5, 12, 20, 0),
            )
        assert send.await_count == 1

        key = "overspeed_norecords:2026-08-05 12|萊涅01|狼團|channel:42"
        assert await alert_already_sent(db, KIND_OVERSPEED, key) is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_overspeed_claim_visible_via_readonly_connection(tmp_path, monkeypatch):
    """write_lock 下寫入的 claim，須能被獨立唯讀連線讀到。"""
    monkeypatch.setenv("EXP_ALERT_SEND_CLEAR", "1")
    db_path = str(tmp_path / "dual.db")
    write_db = await connect_db(db_path, check_integrity=False)
    read_conn = None
    try:
        await apply_migrations(write_db)
        t_now, t_prev = "2026-08-05 12:10:00", "2026-08-05 12:00:00"
        await write_db.executemany(
            """
            INSERT INTO exp_history
            (record_time, player_name, server_name, level, exp, class_name,
             subjugation_grade, guild_name)
            VALUES (?, ?, ?, ?, ?, '戰士', 1, ?)
            """,
            [
                (t_now, "Alice", "萊涅01", 90, 2e12, "狼團"),
                (t_prev, "Alice", "萊涅01", 90, 1e12, "狼團"),
            ],
        )
        await write_db.commit()
        read_conn = await connect_db_ro(db_path)

        lock = asyncio.Lock()
        bot = MagicMock()
        settings = SimpleNamespace(
            alerts_enabled=True,
            alert_server="萊涅01",
            alert_guild="狼團",
            alert_count=30,
            alert_interval_minutes=10,
            alert_speed_window_minutes=10,
            SPEED_LIMIT=2000.0,
            ALERT_CHANNEL_IDS=[42],
        )
        import datetime

        with patch(
            "services.overspeed_alerts.collect_overspeed", return_value=[]
        ), patch(
            "services.overspeed_alerts.send_embeds_to_channels",
            new_callable=AsyncMock,
            return_value={42},
        ):
            await run_overspeed_patrol(
                bot,
                settings,
                read_db=read_conn,
                write_db=write_db,
                times=[(t_now,), (t_prev,)],
                current_time=datetime.datetime(2026, 8, 5, 12, 10, 0),
                write_lock=lock,
            )

        key = (
            f"overspeed:{t_now}|{t_prev}|萊涅01|狼團|30|channel:42"
        )
        assert await alert_already_sent(read_conn, KIND_OVERSPEED, key) is True
        assert not lock.locked()
    finally:
        if read_conn is not None:
            await read_conn.close()
        await write_db.close()


def test_find_pending_boss_hour_supports_late_catch_up():
    import datetime

    from services.timeutil import TAIPEI

    weekday = next(iter(GAP_BOSS_SCHEDULE))
    hour = GAP_BOSS_SCHEDULE[weekday][0]
    base = datetime.datetime(2026, 8, 3, hour, 0, tzinfo=TAIPEI)
    target = base + datetime.timedelta(days=(weekday - base.weekday()) % 7)

    on_time = find_pending_boss_hour(target - datetime.timedelta(minutes=10))
    assert on_time is not None and on_time[1] == 10

    # 錯過整 10 分鐘那一輪，3 分鐘前仍應補送
    late = find_pending_boss_hour(target - datetime.timedelta(minutes=3))
    assert late is not None and late[1] == 3

    assert find_pending_boss_hour(target - datetime.timedelta(minutes=30)) is None


def test_search_cache_evicts_oldest_beyond_limit():
    from services import search_cache

    search_cache.invalidate_player_search_cache()
    limit = search_cache._SEARCH_CACHE_MAX_ENTRIES
    for i in range(limit + 10):
        search_cache.set_cached_search(f"k{i}", {"kind": "text", "content": str(i)})

    assert search_cache.cached_search_size() == limit
    assert search_cache.get_cached_search("k0") is None
    assert search_cache.get_cached_search(f"k{limit + 9}") is not None
    search_cache.invalidate_player_search_cache()


@pytest.mark.asyncio
async def test_transfer_dedupe_kept_until_transfer_retention(tmp_path):
    db = await aiosqlite.connect(str(tmp_path / "prune.db"))
    try:
        await apply_migrations(db)
        await db.executemany(
            "INSERT INTO alert_dedupe (kind, dedupe_key, created_at) VALUES (?, ?, ?)",
            [
                ("overspeed", "old-overspeed", "2026-07-01 00:00:00"),
                ("transfer", "recent-transfer", "2026-07-01 00:00:00"),
                ("transfer", "ancient-transfer", "2025-01-01 00:00:00"),
            ],
        )
        await db.commit()

        plan = RetentionPlan(
            keep_ranges=[],
            thin_ranges=[],
            retention_cutoff="2026-08-01 00:00:00",
            transfer_cutoff="2026-01-01 00:00:00",
        )
        await prune_secondary_async(db, plan)

        async with db.execute(
            "SELECT dedupe_key FROM alert_dedupe ORDER BY dedupe_key"
        ) as cur:
            remaining = [r[0] for r in await cur.fetchall()]
        assert remaining == ["recent-transfer"]
    finally:
        await db.close()
