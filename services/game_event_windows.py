"""官方公告時程：領域轉移／變更職業視窗。

來源：https://warsofprasia.beanfun.com/News
細節 API：POST /api/EventAD/GetEventAD?eventAdId=…
銜接點落在視窗內時，尋人可放寬「職業不符」並標註疑似轉職／轉服。
"""
from __future__ import annotations

import datetime
from typing import Optional

# (start, end, label) — 台北時間，含端點
# 領域轉移：公告「轉移時間」；近期場次多為週三 12:00～週日 23:59
REALM_TRANSFER_WINDOWS: list[tuple[str, str, str]] = [
    ("2025-12-22 12:00:00", "2025-12-23 12:00:00", "第20次領域轉移"),
    ("2026-01-07 12:00:00", "2026-01-11 23:59:59", "第21次領域轉移"),
    ("2026-02-04 12:00:00", "2026-02-08 23:59:59", "第22次領域轉移"),
    ("2026-03-04 12:00:00", "2026-03-08 23:59:59", "第23次領域轉移"),
    ("2026-04-01 12:00:00", "2026-04-05 23:59:59", "第24次領域轉移"),
    ("2026-05-06 12:00:00", "2026-05-10 23:59:59", "第25次領域轉移"),
    ("2026-06-03 12:00:00", "2026-06-07 23:59:59", "第26次領域轉移"),
    ("2026-07-01 12:00:00", "2026-07-05 23:59:59", "第27次領域轉移"),
]

# 可轉職時段：公告「職業變更商品」販售職業變更幣的期間，
# 以及「自由職業變更」活動進行期間（同樣提供變更幣／免費變更）。
CLASS_CHANGE_WINDOWS: list[tuple[str, str, str]] = [
    ("2024-10-02 05:00:00", "2024-10-16 05:00:00", "職業變更商品（第二波）"),
    ("2024-12-24 05:00:00", "2025-01-08 05:00:00", "職業變更商品"),
    ("2025-02-12 05:00:00", "2025-02-26 05:00:00", "特別職業變更支援禮包"),
    ("2025-04-09 05:00:00", "2025-04-23 05:00:00", "職業變更商品"),
    # 08/06 開賣；異常延長至 08/27（14320／14345）
    ("2025-08-06 05:00:00", "2025-08-27 05:00:00", "職業變更商品"),
    ("2026-02-04 05:00:00", "2026-02-18 04:59:59", "EP.6 自由職業變更"),
    ("2026-06-10 05:00:00", "2026-06-24 05:00:00", "2週年自由職業變更"),
]

_FMT = "%Y-%m-%d %H:%M:%S"


def _parse(s: str) -> Optional[datetime.datetime]:
    try:
        return datetime.datetime.strptime(s, _FMT)
    except (TypeError, ValueError):
        return None


def _in_windows(
    point: Optional[str],
    windows: list[tuple[str, str, str]],
) -> Optional[str]:
    """若時間點落在任一視窗內，回傳該視窗 label。"""
    dt = _parse(point) if point else None
    if dt is None:
        return None
    for start_s, end_s, label in windows:
        start, end = _parse(start_s), _parse(end_s)
        if start is None or end is None:
            continue
        if start <= dt <= end:
            return label
    return None


def _gap_crosses_window(
    a_last: Optional[str],
    b_first: Optional[str],
    windows: list[tuple[str, str, str]],
) -> Optional[str]:
    """兩段觀測的銜接中點（或端點）若落在視窗內，回傳 label。"""
    a = _parse(a_last)
    b = _parse(b_first)
    if a is None and b is None:
        return None
    if a is not None and b is not None:
        mid = a + (b - a) / 2 if b >= a else b + (a - b) / 2
        mid_s = mid.strftime(_FMT)
        hit = _in_windows(mid_s, windows)
        if hit:
            return hit
    return _in_windows(a_last, windows) or _in_windows(b_first, windows)


def realm_transfer_label(a_last: Optional[str], b_first: Optional[str]) -> Optional[str]:
    return _gap_crosses_window(a_last, b_first, REALM_TRANSFER_WINDOWS)


def class_change_label(a_last: Optional[str], b_first: Optional[str]) -> Optional[str]:
    return _gap_crosses_window(a_last, b_first, CLASS_CHANGE_WINDOWS)


def allow_class_mismatch_high(
    a_last: Optional[str],
    b_first: Optional[str],
    *,
    obs_gap_hours: float,
    exact_exp: bool,
) -> bool:
    """EXP 完全一致且觀測銜接緊密 → 允許職業不符進主軌（疑似轉職）。

    落在官方自由職業變更視窗時，可再放寬到 7 天內。
    """
    if not exact_exp:
        return False
    if obs_gap_hours <= 72:
        return True
    if class_change_label(a_last, b_first) and obs_gap_hours <= 7 * 24:
        return True
    return False
