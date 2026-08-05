"""轉服 dedupe 針對性查詢。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from cogs.exp_tracker import ExpTracker
from db.schema import apply_migrations
from services.transfer_alert_flow import (
    filter_viable_ranked,
    lookup_alerted_pairs,
    pair_key_from_row,
)


def _xfer_row(
    new_exp=2e12,
    new_name="New",
    new_server="S2",
    new_lvl=60,
    new_cls="戰士",
    new_guild="",
    old_name="Old",
    old_server="S1",
    old_lvl=60,
    old_cls="戰士",
    old_exp=2e12,
    old_guild="",
    new_sub=1,
    old_sub=1,
    old_last_seen="2026-07-27 11:00:00",
):
    return (
        new_exp,
        new_name,
        new_server,
        new_lvl,
        new_cls,
        new_guild,
        old_name,
        old_server,
        old_lvl,
        old_cls,
        old_exp,
        old_guild,
        new_sub,
        old_sub,
        old_last_seen,
    )


@pytest.mark.asyncio
async def test_transfer_pair_lookup_targeted(tmp_path):
    import aiosqlite

    db_path = tmp_path / "xfer.db"
    db = await aiosqlite.connect(str(db_path))
    try:
        await apply_migrations(db)
        await db.execute(
            """
            INSERT INTO transfer_alerts_log
            (old_name, old_server, new_name, new_server, alert_time)
            VALUES
            ('A', 'S1', 'B', 'S2', '2026-07-01 00:00:00'),
            ('C', 'S1', 'D', 'S2', '2026-07-02 00:00:00')
            """
        )
        await db.commit()

        candidates = [("A", "S1", "B", "S2"), ("X", "S1", "Y", "S2")]
        found = await lookup_alerted_pairs(db, candidates)
        assert ("A", "S1", "B", "S2") in found
        assert ("X", "S1", "Y", "S2") not in found
        assert ("C", "S1", "D", "S2") not in found
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_filter_viable_ranked_requires_old_missing(tmp_path):
    import aiosqlite

    db = await aiosqlite.connect(str(tmp_path / "viable.db"))
    try:
        await apply_migrations(db)
        row = _xfer_row()
        assert pair_key_from_row(row) == ("Old", "S1", "New", "S2")
        t_now = "2026-07-27 12:00:00"
        await db.execute(
            """
            INSERT INTO exp_history
            (record_time, player_name, server_name, level, exp, class_name, subjugation_grade)
            VALUES (?, 'Old', 'S1', 60, 2e12, '戰士', 1)
            """,
            (t_now,),
        )
        await db.commit()
        # 舊角仍在榜 → 不 viable
        viable = await filter_viable_ranked(db, [row], set(), [t_now])
        assert viable == []
        # 舊角缺席 → viable
        viable2 = await filter_viable_ranked(db, [row], set(), ["2026-07-27 11:00:00"])
        assert viable2 == [row]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_players_present_at_times_batches(tmp_path):
    import aiosqlite

    from services.transfer_alert_flow import players_present_at_times

    db = await aiosqlite.connect(str(tmp_path / "batch.db"))
    try:
        await apply_migrations(db)
        t1, t2 = "2026-07-28 10:00:00", "2026-07-28 10:10:00"
        await db.executemany(
            """
            INSERT INTO exp_history
            (record_time, player_name, server_name, level, exp, class_name, subjugation_grade)
            VALUES (?, ?, ?, 60, 2e12, '戰士', 1)
            """,
            [
                (t1, "A", "S1"),
                (t1, "B", "S2"),
                (t2, "A", "S1"),
            ],
        )
        await db.commit()
        present = await players_present_at_times(
            db, [t1, t2], [("A", "S1"), ("B", "S2"), ("C", "S3")]
        )
        assert (t1, "A", "S1") in present
        assert (t1, "B", "S2") in present
        assert (t2, "A", "S1") in present
        assert (t2, "B", "S2") not in present
        assert (t1, "C", "S3") not in present
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_transfer_alert_send_failure_releases_claim(tmp_path):
    import aiosqlite

    db = await aiosqlite.connect(str(tmp_path / "rollback.db"))
    try:
        await apply_migrations(db)
        bot = SimpleNamespace(db=db, db_ro=db)
        cog = ExpTracker.__new__(ExpTracker)
        cog.bot = bot
        row = _xfer_row(old_last_seen="2026-08-05 11:50:00")
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
        cog._get_potential_transfers = AsyncMock(return_value=[row])
        cog._send_transfer_alert = AsyncMock(return_value=0)

        with patch(
            "cogs.exp_tracker.is_transfer_active_period", return_value=False
        ), patch(
            "cogs.exp_tracker.rank_transfer_candidates", return_value=[row]
        ), patch(
            "cogs.exp_tracker.lookup_alerted_pairs",
            new_callable=AsyncMock,
            return_value=set(),
        ), patch(
            "cogs.exp_tracker.filter_viable_ranked",
            new_callable=AsyncMock,
            return_value=[row],
        ), patch(
            "cogs.exp_tracker.pick_unique_pairs", return_value=[pair]
        ), patch(
            "cogs.exp_tracker.prune_stale_missing", new_callable=AsyncMock
        ):
            await cog.check_for_transfers(
                "2026-08-05 12:00:00", "2026-08-05 11:50:00"
            )

        async with db.execute(
            """
            SELECT 1 FROM transfer_alerts_log
            WHERE old_name='Old' AND old_server='S1'
              AND new_name='New' AND new_server='S2'
            """
        ) as cursor:
            assert await cursor.fetchone() is None
    finally:
        await db.close()
