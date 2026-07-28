"""尋人導向保留區間：最近 N 天 ∪ 最近 K 次領域轉移窗（結束後 pad 天）。"""
from __future__ import annotations

import datetime
from typing import Iterable, Optional, Sequence

from services.game_event_windows import REALM_TRANSFER_WINDOWS
from services.timeutil import FMT_SQL, now_naive_taipei

DEFAULT_RECENT_DAYS = 3
# 轉移窗結束後再留 N 天：窗尾（如 23:50）轉移後，可能隔幾天才上新服榜
DEFAULT_TRANSFER_PAD_DAYS = 3
DEFAULT_MAX_TRANSFER_WINDOWS = 3


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


def select_recent_transfer_windows(
    windows: Sequence[tuple[str, str, str]],
    *,
    max_windows: int,
    now: Optional[datetime.datetime] = None,
) -> list[tuple[str, str, str]]:
    """依窗結束時間取最近 max_windows 次（略過尚未開始的未來窗）。"""
    if max_windows < 0:
        raise ValueError("max_windows must be >= 0")
    if max_windows == 0:
        return []

    now_dt = now if now is not None else now_naive_taipei()
    if now_dt.tzinfo is not None:
        now_dt = now_dt.replace(tzinfo=None)

    scored: list[tuple[datetime.datetime, tuple[str, str, str]]] = []
    for item in windows:
        start_s, end_s, _label = item
        start, end = _parse(start_s), _parse(end_s)
        if start is None or end is None:
            continue
        # 預估／未開跑的未來窗不佔「最近 K 次」名額，避免擠掉真實歷史窗
        if start > now_dt:
            continue
        scored.append((end, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:max_windows]]


def build_search_keep_ranges(
    *,
    recent_days: int = DEFAULT_RECENT_DAYS,
    pad_days: int = DEFAULT_TRANSFER_PAD_DAYS,
    max_transfer_windows: int = DEFAULT_MAX_TRANSFER_WINDOWS,
    now: Optional[datetime.datetime] = None,
    transfer_windows: Sequence[tuple[str, str, str]] | None = None,
) -> list[tuple[str, str]]:
    """回傳合併後的保留區間 (start, end) SQL 字串列表。

    領域轉移：只保留最近 max_transfer_windows 次，
    區間為 [窗開始, 窗結束 + pad_days]（轉移前不另留）。
    """
    if recent_days < 0:
        raise ValueError("recent_days must be >= 0")
    if pad_days < 0:
        raise ValueError("pad_days must be >= 0")
    if max_transfer_windows < 0:
        raise ValueError("max_transfer_windows must be >= 0")

    now_dt = now if now is not None else now_naive_taipei()
    if now_dt.tzinfo is not None:
        now_dt = now_dt.replace(tzinfo=None)

    pad = datetime.timedelta(days=int(pad_days))
    raw: list[tuple[str, str]] = []

    windows = transfer_windows if transfer_windows is not None else REALM_TRANSFER_WINDOWS
    selected = select_recent_transfer_windows(
        windows, max_windows=max_transfer_windows, now=now_dt
    )
    for start_s, end_s, _label in selected:
        start, end = _parse(start_s), _parse(end_s)
        if start is None or end is None:
            continue
        raw.append((_fmt(start), _fmt(end + pad)))

    recent_start = now_dt - datetime.timedelta(days=int(recent_days))
    if recent_days > 0:
        raw.append((_fmt(recent_start), _fmt(now_dt)))
    elif recent_days == 0 and not raw:
        # 無轉移窗且 recent=0：仍保留「當下」單點，避免 empty → 誤刪全表
        raw.append((_fmt(now_dt), _fmt(now_dt)))

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


def build_transfer_thin_ranges(
    *,
    max_transfer_windows: int = DEFAULT_MAX_TRANSFER_WINDOWS,
    now: Optional[datetime.datetime] = None,
    transfer_windows: Sequence[tuple[str, str, str]] | None = None,
) -> list[tuple[str, str]]:
    """官方領域轉移窗 [start, end]（不加 pad），供窗內稀疏化。"""
    if max_transfer_windows < 0:
        raise ValueError("max_transfer_windows must be >= 0")

    now_dt = now if now is not None else now_naive_taipei()
    if now_dt.tzinfo is not None:
        now_dt = now_dt.replace(tzinfo=None)

    windows = transfer_windows if transfer_windows is not None else REALM_TRANSFER_WINDOWS
    selected = select_recent_transfer_windows(
        windows, max_windows=max_transfer_windows, now=now_dt
    )
    ranges: list[tuple[str, str]] = []
    for start_s, end_s, _label in selected:
        start, end = _parse(start_s), _parse(end_s)
        if start is None or end is None:
            continue
        ranges.append((_fmt(start), _fmt(end)))
    # 依開始時間排序，方便 dry-run／測試穩定輸出
    ranges.sort(key=lambda x: x[0])
    return ranges


def exp_history_transfer_middle_sql(
    start: str,
    end: str,
    *,
    for_delete: bool = False,
) -> tuple[str, tuple[str, str, str, str]]:
    """單一轉移窗：刪／計同角同服除首尾外的中間快照。

    分組鍵 (player_name, server_name)；只留 MIN/MAX(record_time)。
    """
    bounds = """
        SELECT player_name, server_name,
               MIN(record_time) AS min_t,
               MAX(record_time) AS max_t
        FROM exp_history
        WHERE record_time BETWEEN ? AND ?
        GROUP BY player_name, server_name
    """
    params = (start, end, start, end)
    if for_delete:
        sql = f"""
            DELETE FROM exp_history
            WHERE rowid IN (
                SELECT e.rowid
                FROM exp_history e
                INNER JOIN ({bounds}) bounds
                  ON e.player_name = bounds.player_name
                 AND e.server_name = bounds.server_name
                WHERE e.record_time BETWEEN ? AND ?
                  AND e.record_time > bounds.min_t
                  AND e.record_time < bounds.max_t
            )
        """
    else:
        sql = f"""
            SELECT COUNT(*)
            FROM exp_history e
            INNER JOIN ({bounds}) bounds
              ON e.player_name = bounds.player_name
             AND e.server_name = bounds.server_name
            WHERE e.record_time BETWEEN ? AND ?
              AND e.record_time > bounds.min_t
              AND e.record_time < bounds.max_t
        """
    return sql, params


def exp_history_transfer_middle_statements(
    ranges: Sequence[tuple[str, str]],
    *,
    for_delete: bool = False,
) -> list[tuple[str, tuple[str, str, str, str]]]:
    """每個轉移窗各一條 COUNT/DELETE（無區間時空列表）。"""
    return [
        exp_history_transfer_middle_sql(start, end, for_delete=for_delete)
        for start, end in ranges
    ]


def search_retention_cutoff(
    *,
    recent_days: int = DEFAULT_RECENT_DAYS,
    pad_days: int = DEFAULT_TRANSFER_PAD_DAYS,
    max_transfer_windows: int = DEFAULT_MAX_TRANSFER_WINDOWS,
    now: Optional[datetime.datetime] = None,
) -> str:
    """與 cleanup_db --for-search 對齊的 transfer_alerts / alert_dedupe 截止時間。"""
    _ = (pad_days, max_transfer_windows)
    now_dt = now if now is not None else now_naive_taipei()
    if now_dt.tzinfo is not None:
        now_dt = now_dt.replace(tzinfo=None)
    cutoff = now_dt - datetime.timedelta(days=int(recent_days))
    return cutoff.strftime(FMT_SQL)
