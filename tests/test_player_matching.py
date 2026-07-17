"""尋人匹配純邏輯。"""
from __future__ import annotations

from services.player_matching import (
    class_compatible,
    confidence,
    gap_hours,
    is_unknown_class,
    observation_gap_hours,
    pick_soft_candidates,
    score,
)


def test_class_compatible_unknown_both():
    assert class_compatible("未知", None) is True
    assert class_compatible("太陽監視者", "未知") is False
    assert class_compatible("太陽監視者", "太陽監視者") is True


def test_gap_hours_and_overlap():
    assert gap_hours("2026-01-01 10:00:00", "2026-01-01 12:00:00") == 2.0
    assert (
        observation_gap_hours(
            "2026-01-01 10:00:00",
            "2026-01-01 12:00:00",
            "2026-01-01 11:00:00",
            "2026-01-01 13:00:00",
        )
        == 0.0
    )
    assert (
        observation_gap_hours(
            "2026-01-01 10:00:00",
            "2026-01-01 11:00:00",
            "2026-01-01 13:00:00",
            "2026-01-01 14:00:00",
        )
        == 2.0
    )


def test_confidence_high_near_identical():
    assert (
        confidence("太陽監視者", 10, "太陽監視者", 1e7, 10, 1.0, False)
        == "high"
    )


def test_confidence_medium_cross_server_large_gap():
    assert (
        confidence("太陽監視者", 10, "太陽監視者", 1e10, 10, 100.0, False)
        == "medium"
    )


def test_score_prefers_same_class():
    same = score("A", 5, 60, 1e8, 1.0, "A", 5, 60, False, forward=True)
    other = score("A", 5, 60, 1e8, 1.0, "B", 5, 60, False, forward=True)
    assert same < other


def test_pick_soft_candidates_limits_per_direction():
    cands = [
        {"name": f"n{i}", "server": "s", "direction": "forward", "score": i, "exp_diff": i}
        for i in range(5)
    ] + [
        {
            "name": f"b{i}",
            "server": "s",
            "direction": "backward",
            "score": i,
            "exp_diff": i,
        }
        for i in range(5)
    ]
    picked = pick_soft_candidates(cands, exclude_keys=[], per_direction=2)
    assert len([c for c in picked if c["direction"] == "forward"]) == 2
    assert len([c for c in picked if c["direction"] == "backward"]) == 2


def test_is_unknown_class():
    assert is_unknown_class("未知")
    assert not is_unknown_class("MirageBlade")
