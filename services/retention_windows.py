"""尋人導向保留區間：最近 N 天 ∪ 領域轉移窗起至結束後 pad 天（不含轉移前）。"""
from __future__ import annotations

import datetime
from typing import Iterable, Optional, Sequence

from services.game_event_windows import REALM_TRANSFER_WINDOWS
from services.timeutil import FMT_SQL, now_naive_taipei

DEFAULT_RECENT_DAYS = 7
DEFAULT_TRANSFER_PAD_DAYS = 5


def _parse(s: str) -> Optional[datetime.datetime]:
    try:
        return datetime.datetime.strptime(s, FMT_SQL)
    except (TypeError, ValueError):
        return None


def _fmt(dt: datetime.datetime) -> str:
    return dt.strftime(FMT_SQL)


def merge_ranges(ranges: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """合併重疊／相接的 [start, end] 字串區間（含端點）。"""
    parsed: list[tuple[datetime.datetime, datetime.datetime]] = []
    for start_s, end_s in ranges:
        start, end = _parse(start_s), _parse(end_s)
        if start is None or end is None:
            continue
        if end < start:
            start, end = end, start
        parsed.append((start, end))
    if not parsed:
        return []
    parsed.sort(key=lambda x: x[0])
    merged: list[tuple[datetime.datetime, datetime.datetime]] = [parsed[0]]
    for start, end in parsed[1:]:
        cur_start, cur_end = merged[-1]
        if start <= cur_end + datetime.timedelta(seconds=1):
            merged[-1] = (cur_start, max(cur_end, end))
        else:
            merged.append((start, end))
    return [(_fmt(a), _fmt(b)) for a, b in merged]


def build_search_keep_ranges(
    *,
    recent_days: int = DEFAULT_RECENT_DAYS,
    pad_days: int = DEFAULT_TRANSFER_PAD_DAYS,
    now: Optional[datetime.datetime] = None,
    transfer_windows: Sequence[tuple[str, str, str]] | None = None,
) -> list[tuple[str, str]]:
    """回傳合併後的保留區間 (start, end) SQL 字串列表。

    領域轉移：保留 [窗開始, 窗結束 + pad_days]（轉移前不另留）。
    """
    if recent_days < 0:
        raise ValueError("recent_days must be >= 0")
    if pad_days < 0:
        raise ValueError("pad_days must be >= 0")

    now_dt = now if now is not None else now_naive_taipei()
    if now_dt.tzinfo is not None:
        now_dt = now_dt.replace(tzinfo=None)

    pad = datetime.timedelta(days=int(pad_days))
    raw: list[tuple[str, str]] = []

    windows = transfer_windows if transfer_windows is not None else REALM_TRANSFER_WINDOWS
    for start_s, end_s, _label in windows:
        start, end = _parse(start_s), _parse(end_s)
        if start is None or end is None:
            continue
        raw.append((_fmt(start), _fmt(end + pad)))

    recent_start = now_dt - datetime.timedelta(days=int(recent_days))
    raw.append((_fmt(recent_start), _fmt(now_dt)))

    return merge_ranges(raw)


def exp_history_outside_keep_sql(
    ranges: Sequence[tuple[str, str]],
    *,
    for_delete: bool = False,
) -> tuple[str, tuple[str, ...]]:
    """產生 COUNT/DELETE：不在任一保留區間內的 exp_history。

    無區間時回傳永不匹配的條件（安全：不刪任何列）。
    """
    verb = "DELETE FROM exp_history" if for_delete else "SELECT COUNT(*) FROM exp_history"
    if not ranges:
        return f"{verb} WHERE 0", ()

    parts: list[str] = []
    params: list[str] = []
    for start, end in ranges:
        parts.append("record_time BETWEEN ? AND ?")
        params.extend([start, end])
    where = " OR ".join(parts)
    return f"{verb} WHERE NOT ({where})", tuple(params)
