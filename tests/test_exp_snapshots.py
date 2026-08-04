"""EXP snapshot helpers."""
from __future__ import annotations

import pytest

from services.exp_snapshots import (
    normalize_guild,
    players_to_insert_batch,
    profiles_from_insert_batch,
)


def test_normalize_guild_placeholders():
    assert normalize_guild(None) == ""
    assert normalize_guild("未知") == ""
    assert normalize_guild("null") == ""
    assert normalize_guild(" 守護者 ") == "守護者"


def test_players_to_insert_batch_skips_nameless():
    batch = players_to_insert_batch(
        "2026-07-19 12:00:00",
        "萊涅01",
        [
            {"gc_name": None, "gc_level": 1, "gc_exp": 1},
            {
                "gc_name": "Hero",
                "gc_level": 60,
                "gc_exp": 1e12,
                "class_name": "太陽監視者",
                "guild_name": "狼團",
                "string_map": {"grade": "10"},
            },
            {
                "gc_name": "BadGrade",
                "gc_level": 50,
                "gc_exp": 1e11,
                "string_map": {"grade": "x"},
            },
        ],
    )
    assert len(batch) == 2
    assert batch[0][2] == "Hero"
    assert batch[0][6] == 10
    assert batch[0][7] == "狼團"
    assert batch[1][2] == "BadGrade"
    assert batch[1][6] == 0
    assert batch[1][7] == ""

    profiles = profiles_from_insert_batch(batch)
    by_name = {p[0]: p for p in profiles}
    assert by_name["Hero"][2] == "太陽監視者"
    assert by_name["BadGrade"][2] == "未知"
    assert len(by_name["Hero"]) == 11
    assert by_name["Hero"][4] == 1e12  # min_exp
    assert by_name["Hero"][5] == 1e12  # max_exp
    assert by_name["Hero"][10] == "狼團"


def test_pad_and_timeutil():
    from services.text_display import display_width, pad_text
    from services.timeutil import TAIPEI, now_taipei, today_taipei_str

    assert display_width("測") == 2
    assert pad_text("A", 3) == "A  "
    assert now_taipei().tzinfo == TAIPEI
    assert len(today_taipei_str()) == 10


@pytest.mark.asyncio
async def test_exp_history_upsert_fills_empty_guild(tmp_path):
    import aiosqlite

    from db.schema import apply_migrations
    from services.exp_snapshots import (
        EXP_HISTORY_INSERT_SQL,
        EXP_HISTORY_TOUCH_SQL,
        touch_params_from_insert_batch,
    )

    db = await aiosqlite.connect(str(tmp_path / "upsert.db"))
    try:
        await apply_migrations(db)
        t = "2026-08-04 12:00:00"
        first = (t, "萊涅01", "Hero", 60, 1e12, "太陽監視者", 10, "")
        second = (t, "萊涅01", "Hero", 61, 1.1e12, "太陽監視者", 11, "狼團")
        await db.execute(EXP_HISTORY_INSERT_SQL, first)
        await db.executemany(
            EXP_HISTORY_TOUCH_SQL, touch_params_from_insert_batch([first])
        )
        await db.execute(EXP_HISTORY_INSERT_SQL, second)
        await db.executemany(
            EXP_HISTORY_TOUCH_SQL, touch_params_from_insert_batch([second])
        )
        await db.commit()
        async with db.execute(
            "SELECT level, exp, guild_name, subjugation_grade FROM exp_history "
            "WHERE player_name=? AND record_time=?",
            ("Hero", t),
        ) as cur:
            row = await cur.fetchone()
        assert row == (61, 1.1e12, "狼團", 11)
    finally:
        await db.close()
