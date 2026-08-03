"""EXP snapshot helpers."""
from __future__ import annotations

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
