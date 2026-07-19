"""經驗測速／超速警報純邏輯。"""
from __future__ import annotations

import datetime
from typing import Any, Iterable, Optional, Sequence


def hourly_speed(exp_now: float, exp_prev: float, minutes_diff: float) -> float:
    """回傳每小時經驗增速（原始 EXP 單位）。"""
    if minutes_diff <= 0:
        return 0.0
    diff = exp_now - exp_prev
    if diff <= 0:
        return 0.0
    return (diff / minutes_diff) * 60


def speed_in_yi(hourly: float) -> float:
    return hourly / 100_000_000


def collect_overspeed(
    records: Iterable[tuple],
    minutes_diff: float,
    speed_limit_yi: float,
) -> list[dict[str, Any]]:
    """records: (name, server, level, exp_now, exp_prev)。"""
    alert_list: list[dict[str, Any]] = []
    for name, server, level, exp_now, exp_prev in records:
        hourly = hourly_speed(exp_now, exp_prev, minutes_diff)
        speed_yi = speed_in_yi(hourly)
        if speed_yi >= speed_limit_yi:
            alert_list.append(
                {"name": name, "server": server, "level": level, "speed": speed_yi}
            )
    alert_list.sort(key=lambda x: x["speed"], reverse=True)
    return alert_list


def collect_speed_ranking(
    records: Iterable[tuple],
    minutes_diff: float,
) -> list[dict[str, Any]]:
    """records: (name, server, level, exp_now, exp_prev)。"""
    speed_data: list[dict[str, Any]] = []
    safe_minutes = minutes_diff if minutes_diff > 0 else 10.0
    for name, server, level, exp_now, exp_prev in records:
        hourly = hourly_speed(exp_now, exp_prev, safe_minutes)
        if hourly > 0:
            speed_data.append(
                {"name": name, "server": server, "level": level, "speed": hourly}
            )
    speed_data.sort(key=lambda x: x["speed"], reverse=True)
    return speed_data


def pick_interval_baseline(
    times: Sequence[tuple],
    target_minutes: float,
    *,
    fmt: str = "%Y-%m-%d %H:%M:%S",
) -> tuple[Optional[str], float]:
    """從完整快照時間列表挑最接近 target_minutes 的前一個時間點。

    times[0] 為最新；回傳 (time_prev, minutes_diff)。不足兩筆時 (None, 0)。
    """
    if len(times) < 2:
        return None, 0.0
    time_now = times[0][0]
    t1 = datetime.datetime.strptime(time_now, fmt)
    time_prev = times[1][0]
    minutes_diff = (
        t1 - datetime.datetime.strptime(time_prev, fmt)
    ).total_seconds() / 60
    best_gap = abs(minutes_diff - target_minutes)
    for (rt,) in times[1:]:
        t2 = datetime.datetime.strptime(rt, fmt)
        gap = (t1 - t2).total_seconds() / 60
        if gap <= 0:
            continue
        score = abs(gap - target_minutes)
        if score < best_gap:
            best_gap = score
            time_prev = rt
            minutes_diff = gap
    return time_prev, minutes_diff
