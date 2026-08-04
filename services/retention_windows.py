"""尋人導向保留區間：最近 N 天 ∪ 最近 K 次領域轉移窗（結束後 pad 天）。"""
from __future__ import annotations

import datetime
from typing import Iterable, Optional, Sequence

from services.game_event_windows import REALM_TRANSFER_WINDOWS
from services.timeutil import FMT_SQL, now_naive_taipei

DEFAULT_RECENT_DAYS = 3
# 轉移窗結束後再留 N 天：對齊 TRANSFER_LOGIN_GRACE_DAYS（延遲登入）
# NAS：維持單次轉移窗 + 稀疏寫入／窗內瘦身，避免 exp_history 肥大
DEFAULT_TRANSFER_PAD_DAYS = 3
DEFAULT_MAX_TRANSFER_WINDOWS = 1
# 轉服警報 log 與 history 脫鉤，可留較久
DEFAULT_TRANSFER_ALERT_DAYS = 180
# 非轉移活躍期：exp_history 寫入間隔（分鐘）；活躍期仍每輪寫入
HISTORY_SPARSE_INTERVAL_MINUTES = 30
# 線上／離線分批 DELETE（NAS 記憶體較緊時偏小）
ONLINE_DELETE_BATCH_SIZE = 20_000
ONLINE_CHECKPOINT_EVERY_BATCHES = 10


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
    """回傳連續保留區間 (start, end)。

    最早保留點取「最近 K 次轉移窗開始」與「最近 N 天」較早者，直到現在。
    中間橋接區由 build_bridge_thin_ranges 稀疏化，避免時間斷層又控制 NAS 容量。
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

    windows = transfer_windows if transfer_windows is not None else REALM_TRANSFER_WINDOWS
    selected = select_recent_transfer_windows(
        windows, max_windows=max_transfer_windows, now=now_dt
    )
    starts: list[datetime.datetime] = []
    ends: list[datetime.datetime] = [now_dt]
    for start_s, end_s, _label in selected:
        start, end = _parse(start_s), _parse(end_s)
        if start is None or end is None:
            continue
        starts.append(start)
        ends.append(end + datetime.timedelta(days=int(pad_days)))

    recent_start = now_dt - datetime.timedelta(days=int(recent_days))
    if recent_days > 0:
        starts.append(recent_start)
    if not starts:
        starts.append(now_dt)

    return [(_fmt(min(starts)), _fmt(max(ends)))]


def build_bridge_thin_ranges(
    *,
    recent_days: int = DEFAULT_RECENT_DAYS,
    pad_days: int = DEFAULT_TRANSFER_PAD_DAYS,
    max_transfer_windows: int = DEFAULT_MAX_TRANSFER_WINDOWS,
    now: Optional[datetime.datetime] = None,
    transfer_windows: Sequence[tuple[str, str, str]] | None = None,
) -> list[tuple[str, str]]:
    """回傳轉移窗+pad 與 recent 區間之間的橋接區；橋接區只留首尾。"""
    now_dt = now if now is not None else now_naive_taipei()
    if now_dt.tzinfo is not None:
        now_dt = now_dt.replace(tzinfo=None)
    windows = transfer_windows if transfer_windows is not None else REALM_TRANSFER_WINDOWS
    selected = select_recent_transfer_windows(
        windows, max_windows=max_transfer_windows, now=now_dt
    )
    pad = datetime.timedelta(days=int(pad_days))
    dense_ranges: list[tuple[datetime.datetime, datetime.datetime]] = []
    for start_s, end_s, _label in selected:
        start, end = _parse(start_s), _parse(end_s)
        if start is not None and end is not None:
            dense_ranges.append((start, min(end + pad, now_dt)))
    if recent_days > 0:
        dense_ranges.append(
            (now_dt - datetime.timedelta(days=int(recent_days)), now_dt)
        )
    if len(dense_ranges) < 2:
        return []
    merged = merge_ranges([(_fmt(a), _fmt(b)) for a, b in dense_ranges])
    bridges: list[tuple[str, str]] = []
    one_second = datetime.timedelta(seconds=1)
    for (_start_a, end_a), (start_b, _end_b) in zip(
        merged, merged[1:], strict=False
    ):
        bridge_start = _parse(end_a)
        bridge_end = _parse(start_b)
        if bridge_start is None or bridge_end is None:
            continue
        bridge_start += one_second
        bridge_end -= one_second
        if bridge_start <= bridge_end:
            bridges.append((_fmt(bridge_start), _fmt(bridge_end)))
    return bridges


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


def exp_history_outside_keep_batch_sql(
    ranges: Sequence[tuple[str, str]],
    *,
    batch_size: int = 50_000,
) -> tuple[str, tuple]:
    """分批 DELETE：每次最多刪 batch_size 筆（大庫避免單交易 OOM／被殺）。"""
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    if not ranges:
        return "DELETE FROM exp_history WHERE 0", ()

    parts: list[str] = []
    params: list = []
    for start, end in ranges:
        parts.append("record_time BETWEEN ? AND ?")
        params.extend([start, end])
    where = " OR ".join(parts)
    sql = f"""
        DELETE FROM exp_history
        WHERE rowid IN (
            SELECT rowid FROM exp_history
            WHERE NOT ({where})
            LIMIT ?
        )
    """
    params.append(int(batch_size))
    return sql, tuple(params)


def build_transfer_thin_ranges(
    *,
    max_transfer_windows: int = DEFAULT_MAX_TRANSFER_WINDOWS,
    pad_days: int = DEFAULT_TRANSFER_PAD_DAYS,
    now: Optional[datetime.datetime] = None,
    transfer_windows: Sequence[tuple[str, str, str]] | None = None,
) -> list[tuple[str, str]]:
    """轉移保留區間 [start, end+pad] 稀疏化：同角同服只留首尾。

    pad 一併納入，避免窗結束後全密度快照把 NAS 庫撐大。
    """
    if max_transfer_windows < 0:
        raise ValueError("max_transfer_windows must be >= 0")
    if pad_days < 0:
        raise ValueError("pad_days must be >= 0")

    now_dt = now if now is not None else now_naive_taipei()
    if now_dt.tzinfo is not None:
        now_dt = now_dt.replace(tzinfo=None)

    pad = datetime.timedelta(days=int(pad_days))
    windows = transfer_windows if transfer_windows is not None else REALM_TRANSFER_WINDOWS
    selected = select_recent_transfer_windows(
        windows, max_windows=max_transfer_windows, now=now_dt
    )
    ranges: list[tuple[str, str]] = []
    for start_s, end_s, _label in selected:
        start, end = _parse(start_s), _parse(end_s)
        if start is None or end is None:
            continue
        ranges.append((_fmt(start), _fmt(end + pad)))
    # 依開始時間排序，方便 dry-run／測試穩定輸出
    ranges.sort(key=lambda x: x[0])
    return ranges


def should_persist_exp_history(
    when: datetime.datetime,
    *,
    sparse_interval_minutes: int = HISTORY_SPARSE_INTERVAL_MINUTES,
) -> bool:
    """是否寫入本輪 exp_history；即時警報需要每個 10 分鐘快照。

    舊資料由每日清理的轉移／橋接端點稀疏化控制容量。
    """
    if when.tzinfo is not None:
        when = when.replace(tzinfo=None)
    if sparse_interval_minutes < 1:
        raise ValueError("sparse_interval_minutes must be >= 1")
    return True


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


def exp_history_transfer_middle_batch_sql(
    start: str,
    end: str,
    *,
    batch_size: int = 50_000,
) -> tuple[str, tuple]:
    """轉移窗中間列分批 DELETE。"""
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    bounds = """
        SELECT player_name, server_name,
               MIN(record_time) AS min_t,
               MAX(record_time) AS max_t
        FROM exp_history
        WHERE record_time BETWEEN ? AND ?
        GROUP BY player_name, server_name
    """
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
            LIMIT ?
        )
    """
    params = (start, end, start, end, int(batch_size))
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
    """與 exp_history 連續保留區間對齊的 dedupe 截止時間。"""
    ranges = build_search_keep_ranges(
        recent_days=recent_days,
        pad_days=pad_days,
        max_transfer_windows=max_transfer_windows,
        now=now,
    )
    if ranges:
        return ranges[0][0]
    now_dt = now if now is not None else now_naive_taipei()
    return _fmt(now_dt)


def transfer_alert_retention_cutoff(
    *,
    alert_days: int = DEFAULT_TRANSFER_ALERT_DAYS,
    now: Optional[datetime.datetime] = None,
) -> str:
    """transfer_alerts_log 截止時間（預設 180 天）。"""
    now_dt = now if now is not None else now_naive_taipei()
    if now_dt.tzinfo is not None:
        now_dt = now_dt.replace(tzinfo=None)
    cutoff = now_dt - datetime.timedelta(days=int(alert_days))
    return cutoff.strftime(FMT_SQL)
