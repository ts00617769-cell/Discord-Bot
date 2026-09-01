"""轉服 dedupe 針對性查詢。"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from db.schema import apply_migrations
from services.transfer_alert_flow import (
    filter_viable_ranked,
    lookup_alerted_pairs,
    pair_key_from_row,
)
from services.transfer_alert_runner import run_transfer_check


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
async def test_filter_viable_same_tick_only_requires_current_snapshot(
    tmp_path,
):
    """上一輪舊角還在、這一輪才消失 → 仍應視為可報（即時轉服）。"""
    import aiosqlite

    db = await aiosqlite.connect(str(tmp_path / "same_tick.db"))
    try:
        await apply_migrations(db)
        row = _xfer_row()
        t_prev = "2026-08-28 14:00:00"
        t_now = "2026-08-28 14:10:00"
        await db.execute(
            """
            INSERT INTO exp_history
            (record_time, player_name, server_name, level, exp, class_name,
             subjugation_grade)
            VALUES (?, 'Old', 'S1', 60, 2e12, '戰士', 1)
            """,
            (t_prev,),
        )
        await db.commit()
        # 舊邏輯若把上一輪也列入 miss_times，會把即時轉服濾掉
        assert await filter_viable_ranked(db, [row], set(), [t_prev, t_now]) == []
        viable = await filter_viable_ranked(db, [row], set(), [t_now])
        assert viable == [row]
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
        send = AsyncMock(return_value=set())

        with patch(
            "services.transfer_alert_runner.is_transfer_active_period", return_value=False
        ), patch(
            "services.transfer_alert_runner.rank_transfer_candidates", return_value=[row]
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
                channel_ids=[7],
                send_alert=send,
            )

        async with db.execute(
            """
            SELECT 1 FROM transfer_alerts_log
            WHERE old_name='Old' AND old_server='S1'
              AND new_name='New' AND new_server='S2'
            """
        ) as cursor:
            assert await cursor.fetchone() is None
        from services.alert_dedupe import (
            KIND_TRANSFER,
            alert_already_sent,
            transfer_channel_dedupe_key,
        )

        key = transfer_channel_dedupe_key(("Old", "S1", "New", "S2"), 7)
        assert await alert_already_sent(db, KIND_TRANSFER, key) is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_same_tick_rename_reverse_alerts_despite_outbound_log(tmp_path):
    """去程已報過後，同一輪快照改名轉回原 ID 仍應再報一則。"""
    import aiosqlite

    db = await aiosqlite.connect(str(tmp_path / "reverse.db"))
    try:
        await apply_migrations(db)
        t_prev = "2026-08-28 14:00:00"
        t_now = "2026-08-28 14:10:00"
        orig = "一杯敬朝陽一杯敬月光"
        dest = "環保局"
        orig_sv = "伊奈司01"
        dest_sv = "困特03"
        cls = "香射手"
        await db.executemany(
            """
            INSERT INTO exp_history (
                record_time, server_name, player_name, level, exp,
                class_name, subjugation_grade, guild_name
            ) VALUES (?, ?, ?, 92, 2e12, ?, 25, ?)
            """,
            [
                (t_prev, dest_sv, dest, cls, "TTKK"),
                (t_now, orig_sv, orig, cls, "白月如火"),
            ],
        )
        await db.execute(
            """
            INSERT INTO transfer_alerts_log
            (old_name, old_server, new_name, new_server, alert_time)
            VALUES (?, ?, ?, ?, ?)
            """,
            (orig, orig_sv, dest, dest_sv, t_prev),
        )
        await db.commit()

        sent_pairs: list[tuple[str, str, str, str]] = []

        async def send_alert(
            time_now,
            new_name,
            new_server,
            old_name,
            old_server,
            new_lvl,
            new_cls,
            new_sub_grade,
            status,
            exp_diff,
            old_guild="",
            new_guild="",
            channel_ids=None,
        ):
            sent_pairs.append((old_name, old_server, new_name, new_server))
            return set(channel_ids or [])

        await run_transfer_check(
            write_db=db,
            read_db=db,
            time_now=t_now,
            time_prev=t_prev,
            channel_ids=[7],
            send_alert=send_alert,
        )
        assert sent_pairs == [(dest, dest_sv, orig, orig_sv)]
    finally:
        await db.close()
