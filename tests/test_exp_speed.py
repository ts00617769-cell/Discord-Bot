"""測速／超速警報純邏輯。"""
from __future__ import annotations

from services.exp_speed import (
    collect_overspeed,
    collect_speed_ranking,
    hourly_speed,
    pick_interval_baseline,
    speed_in_yi,
)


def test_hourly_speed_basic():
    # 10 分鐘漲 100 億 → 時速 600 億（原始單位）
    yi = 100_000_000
    hourly = hourly_speed(100 * yi, 0, 10)
    assert speed_in_yi(hourly) == 600.0


def test_hourly_speed_zero_or_negative():
    assert hourly_speed(100, 200, 10) == 0.0
    assert hourly_speed(100, 50, 0) == 0.0


def test_collect_overspeed_filters_and_sorts():
    yi = 100_000_000
    # 10 分鐘漲幅 → 時速(億) = (diff/yi)/10*60 = diff_yi * 6
    records = [
        ("慢", "A", 50, 10 * yi, 0),  # 時速 60 億
        ("快", "B", 60, 1000 * yi, 0),  # 時速 6000 億
        ("中", "C", 55, 100 * yi, 0),  # 時速 600 億
        ("零", "D", 40, 5 * yi, 5 * yi),  # 無成長
    ]
    alerts = collect_overspeed(records, 10, speed_limit_yi=500)
    assert [a["name"] for a in alerts] == ["快", "中"]
    assert alerts[0]["speed"] > alerts[1]["speed"]


def test_collect_speed_ranking():
    yi = 100_000_000
    records = [
        ("A", "S1", 10, 20 * yi, 0),
        ("B", "S1", 10, 10 * yi, 0),
    ]
    ranked = collect_speed_ranking(records, 10)
    assert ranked[0]["name"] == "A"
    assert ranked[1]["name"] == "B"


def test_pick_interval_baseline_nearest_30min():
    times = [
        ("2026-07-19 12:00:00",),
        ("2026-07-19 11:50:00",),  # 10 min
        ("2026-07-19 11:30:00",),  # 30 min — best for 30
        ("2026-07-19 11:00:00",),  # 60 min
    ]
    prev, minutes = pick_interval_baseline(times, 30)
    assert prev == "2026-07-19 11:30:00"
    assert minutes == 30.0


def test_pick_interval_baseline_insufficient():
    prev, minutes = pick_interval_baseline([("2026-07-19 12:00:00",)], 30)
    assert prev is None
    assert minutes == 0.0
