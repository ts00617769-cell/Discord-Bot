"""Discord send 重試、轉服 per-channel claim、警報編排整合。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import discord
import pytest

from db.schema import apply_migrations
from services.alert_dedupe import (
    KIND_TRANSFER,
    alert_already_sent,
    transfer_channel_dedupe_key,
)
from services.discord_send import send_to_channels
from services.overspeed_alerts import run_overspeed_patrol
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
