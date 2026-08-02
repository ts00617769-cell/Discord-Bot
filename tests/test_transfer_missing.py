"""轉服消失佇列。"""
from __future__ import annotations

import pytest

from db.schema import apply_migrations
from services.game_event_windows import (
    active_transfer_label,
    disappear_in_transfer_window,
    is_transfer_active_period,
)
from services.transfer_missing import (
    build_missing_queue_rows,
    bump_still_missing,
    fetch_open_missing,
    match_newcomer_to_missing,
    resolve_reappeared,
    upsert_disappeared,
)


def test_transfer_active_helpers():
    assert is_transfer_active_period("2026-07-30 12:00:00") is True
    assert active_transfer_label("2026-07-30 12:00:00") == "第28次領域轉移"
    assert is_transfer_active_period("2026-06-15 12:00:00") is False
    assert disappear_in_transfer_window("2026-07-30 12:00:00") == "第28次領域轉移"
    assert disappear_in_transfer_window("2026-06-15 12:00:00") is None


def test_match_newcomer_same_name():
    missing = {
        "player_name": "Hero",
        "server_name": "S1",
        "last_seen": "2026-07-30 12:00:00",
        "last_exp": 2e12,
        "level": 60,
        "class_name": "太陽監視者",
        "subjugation_grade": 10,
        "guild_name": "狼團",
    }
    neu = {
        "player_name": "Hero",
        "server_name": "S2",
        "level": 60,
        "exp": 2e12,
        "class_name": "太陽監視者",
        "subjugation_grade": 10,
        "guild_name": "狼團",
    }
    row = match_newcomer_to_missing(
        neu, missing, appear_time="2026-07-31 12:00:00"
    )
    assert row is not None
    assert row[1] == "Hero"
    assert row[6] == "Hero"
    assert row[5] == "狼團"


def test_match_newcomer_rejects_same_server():
    missing = {
        "player_name": "Hero",
        "server_name": "S1",
        "last_seen": "2026-07-30 12:00:00",
        "last_exp": 2e12,
        "level": 60,
        "class_name": "太陽監視者",
        "subjugation_grade": 10,
        "guild_name": "",
    }
    neu = {**missing, "server_name": "S1", "exp": 2e12}
    assert (
        match_newcomer_to_missing(neu, missing, appear_time="2026-07-31 12:00:00")
        is None
    )


@pytest.mark.asyncio
async def test_missing_queue_miss_count_and_match(tmp_path):
    import aiosqlite

    db = await aiosqlite.connect(str(tmp_path / "miss.db"))
    try:
        await apply_migrations(db)
        t0 = "2026-07-30 12:00:00"
        t1 = "2026-07-30 12:10:00"
        t2 = "2026-07-30 12:20:00"
        t3 = "2026-07-31 12:00:00"

        await db.execute(
            """
            INSERT INTO exp_history
            (record_time, server_name, player_name, level, exp, class_name,
             subjugation_grade, guild_name)
            VALUES (?, 'S1', 'Hero', 60, 2e12, '太陽監視者', 10, '狼團')
            """,
            (t0,),
        )
        # t1: Hero 消失；其他服有無關玩家維持完整感
        await db.execute(
            """
            INSERT INTO exp_history
            (record_time, server_name, player_name, level, exp, class_name,
             subjugation_grade, guild_name)
            VALUES (?, 'S2', 'Other', 60, 3e12, '太陽監視者', 10, '')
            """,
            (t1,),
        )
        await db.commit()

        await upsert_disappeared(
            db, time_now=t1, time_prev=t0, created_at=t1
        )
        await bump_still_missing(db, time_now=t1)
        open1 = await fetch_open_missing(db)
        assert open1 == []  # miss_count=1 < 2

        # t2: 仍缺席
        await db.execute(
            """
            INSERT INTO exp_history
            (record_time, server_name, player_name, level, exp, class_name,
             subjugation_grade, guild_name)
            VALUES (?, 'S2', 'Other', 60, 3e12, '太陽監視者', 10, '')
            """,
            (t2,),
        )
        await db.commit()
        await upsert_disappeared(
            db, time_now=t2, time_prev=t1, created_at=t2
        )
        await bump_still_missing(db, time_now=t2)
        open2 = await fetch_open_missing(db)
        assert len(open2) == 1
        assert open2[0]["player_name"] == "Hero"

        # 新服出現
        newcomers = [
            {
                "player_name": "Hero",
                "server_name": "S2",
                "level": 60,
                "exp": 2e12,
                "class_name": "太陽監視者",
                "subjugation_grade": 10,
                "guild_name": "狼團",
            }
        ]
        rows = build_missing_queue_rows(
            newcomers, open2, appear_time=t3
        )
        assert len(rows) == 1
        assert rows[0][2] == "S2"
        assert rows[0][7] == "S1"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_resolve_reappeared(tmp_path):
    import aiosqlite

    db = await aiosqlite.connect(str(tmp_path / "reapp.db"))
    try:
        await apply_migrations(db)
        t0 = "2026-07-30 12:00:00"
        t1 = "2026-07-30 12:10:00"
        await db.execute(
            """
            INSERT INTO exp_history
            (record_time, server_name, player_name, level, exp, class_name,
             subjugation_grade, guild_name)
            VALUES (?, 'S1', 'Hero', 60, 2e12, '太陽監視者', 10, '')
            """,
            (t0,),
        )
        await db.commit()
        await upsert_disappeared(
            db, time_now=t1, time_prev=t0, created_at=t1
        )
        await bump_still_missing(db, time_now=t1)

        # 又出現在原服
        await db.execute(
            """
            INSERT INTO exp_history
            (record_time, server_name, player_name, level, exp, class_name,
             subjugation_grade, guild_name)
            VALUES (?, 'S1', 'Hero', 60, 2e12, '太陽監視者', 10, '')
            """,
            (t1,),
        )
        await db.commit()
        n = await resolve_reappeared(db, time_now=t1)
        assert n == 1
        open_rows = await fetch_open_missing(db, min_miss_count=1)
        assert open_rows == []
    finally:
        await db.close()
