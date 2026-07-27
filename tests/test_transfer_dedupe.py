"""轉服 dedupe 針對性查詢。"""
from __future__ import annotations

import pytest

from db.schema import apply_migrations
from services.transfer_alert_flow import (
    filter_viable_ranked,
    lookup_alerted_pairs,
    pair_key_from_row,
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
        # row layout matches transfer SQL select
        row = (
            2e12,
            "New",
            "S2",
            60,
            "戰士",
            "Old",
            "S1",
            60,
            "戰士",
            2e12,
            1,
            1,
        )
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
