"""官方公告時程：領域轉移／變更職業視窗。

來源：https://warsofprasia.beanfun.com/News
細節 API：POST /api/EventAD/GetEventAD?eventAdId=…
銜接點落在視窗內時，尋人可放寬「職業不符」並標註疑似轉職／轉服。

領域轉移特別注意：玩家可能在窗內（例如 23:50）完成轉移後數日才登入，
榜上會「消失 → 隔 N 天才在新服出現」，觀測間隔遠大於實際轉移時刻。
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
    ("2026-07-29 12:00:00", "2026-08-02 23:59:59", "第28次領域轉移"),
]

# 提醒維護者核對公告用；未證實日期絕不參與轉服／改名放寬邏輯。
# 以下依「週三 12:00～週日 23:59、約每 28 天」自 #28 推算。
PROJECTED_REALM_TRANSFER_WINDOWS: list[tuple[str, str, str]] = [
    ("2026-08-26 12:00:00", "2026-08-30 23:59:59", "第29次領域轉移（預估）"),
    ("2026-09-23 12:00:00", "2026-09-27 23:59:59", "第30次領域轉移（預估）"),
    ("2026-10-21 12:00:00", "2026-10-25 23:59:59", "第31次領域轉移（預估）"),
    ("2026-11-18 12:00:00", "2026-11-22 23:59:59", "第32次領域轉移（預估）"),
    ("2026-12-16 12:00:00", "2026-12-20 23:59:59", "第33次領域轉移（預估）"),
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
    # 下一波待官網公告後再加入（勿放未證實預估，以免誤放寬轉職配對）
]

# 轉移後延遲登入寬限：窗結束後仍可能隔 N 天才首次出現在新服榜
TRANSFER_LOGIN_GRACE_DAYS = 3
# 舊服最後上榜可略早於窗開始（申請前仍短暫留在榜上）
TRANSFER_DISAPPEAR_PRE_HOURS = 24

_FMT = "%Y-%m-%d %H:%M:%S"


def _parse(s: Optional[str]) -> Optional[datetime.datetime]:
    if not s:
        return None
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


def match_realm_transfer(
    a_last: Optional[str],
    b_first: Optional[str],
    *,
    grace_days: int = TRANSFER_LOGIN_GRACE_DAYS,
) -> Optional[str]:
    """判定是否符合「轉移窗內消失、之後才登入新服」的銜接。

    - 舊服最後觀測：窗開始前 PRE_HOURS ～ 窗結束
    - 新服首次觀測：不早於舊服最後觀測，且不晚於窗結束 + grace_days
      （含窗內立刻上榜，或隔 2～5 天才登入）
    """
    a = _parse(a_last)
    b = _parse(b_first)
    if a is None or b is None:
        return None
    earlier, later = (a, b) if a <= b else (b, a)
    pre = datetime.timedelta(hours=TRANSFER_DISAPPEAR_PRE_HOURS)
    grace = datetime.timedelta(days=int(grace_days))
    for start_s, end_s, label in REALM_TRANSFER_WINDOWS:
        start, end = _parse(start_s), _parse(end_s)
        if start is None or end is None:
            continue
        disappear_ok = (start - pre) <= earlier <= end
        appear_ok = earlier <= later <= (end + grace)
        if disappear_ok and appear_ok:
            return label
    return None


def realm_transfer_label(a_last: Optional[str], b_first: Optional[str]) -> Optional[str]:
    """優先用延遲登入規則；否則退回中點是否落在窗內。"""
    return match_realm_transfer(a_last, b_first) or _gap_crosses_window(
        a_last, b_first, REALM_TRANSFER_WINDOWS
    )


def class_change_label(a_last: Optional[str], b_first: Optional[str]) -> Optional[str]:
    return _gap_crosses_window(a_last, b_first, CLASS_CHANGE_WINDOWS)


def allow_delayed_transfer_high(
    a_last: Optional[str],
    b_first: Optional[str],
    *,
    obs_gap_hours: float,
    exact_exp: bool,
) -> Optional[str]:
    """EXP 一致／極近時，若符合領域轉移延遲登入，回傳場次 label。"""
    if not exact_exp:
        return None
    # 過長空窗（遠超 grace）不放行；match 內已用窗結束+grace 卡住
    if obs_gap_hours > (14 + TRANSFER_LOGIN_GRACE_DAYS) * 24:
        return None
    return match_realm_transfer(a_last, b_first)


def allow_class_mismatch_high(
    a_last: Optional[str],
    b_first: Optional[str],
    *,
    obs_gap_hours: float,
    exact_exp: bool,
) -> bool:
    """EXP 完全一致且觀測銜接緊密 → 允許職業不符進主軌（疑似轉職）。

    落在官方自由職業變更視窗、或領域轉移延遲登入時，可再放寬。
    """
    if not exact_exp:
        return False
    if obs_gap_hours <= 72:
        return True
    if class_change_label(a_last, b_first) and obs_gap_hours <= 7 * 24:
        return True
    if allow_delayed_transfer_high(
        a_last, b_first, obs_gap_hours=obs_gap_hours, exact_exp=True
    ):
        return True
    return False


def is_transfer_active_period(
    when: Optional[str],
    *,
    grace_days: int = TRANSFER_LOGIN_GRACE_DAYS,
) -> bool:
    """時間點是否落在任一領域轉移活躍期（窗開始前 PRE_HOURS ～ 窗結束+grace）。"""
    dt = _parse(when)
    if dt is None:
        return False
    pre = datetime.timedelta(hours=TRANSFER_DISAPPEAR_PRE_HOURS)
    grace = datetime.timedelta(days=int(grace_days))
    for start_s, end_s, _label in REALM_TRANSFER_WINDOWS:
        start, end = _parse(start_s), _parse(end_s)
        if start is None or end is None:
            continue
        if (start - pre) <= dt <= (end + grace):
            return True
    return False


def active_transfer_label(
    when: Optional[str],
    *,
    grace_days: int = TRANSFER_LOGIN_GRACE_DAYS,
) -> Optional[str]:
    """回傳 `when` 所屬轉移場次 label；不在活躍期則 None。"""
    dt = _parse(when)
    if dt is None:
        return None
    pre = datetime.timedelta(hours=TRANSFER_DISAPPEAR_PRE_HOURS)
    grace = datetime.timedelta(days=int(grace_days))
    for start_s, end_s, label in REALM_TRANSFER_WINDOWS:
        start, end = _parse(start_s), _parse(end_s)
        if start is None or end is None:
            continue
        if (start - pre) <= dt <= (end + grace):
            return label
    return None


def disappear_in_transfer_window(
    last_seen: Optional[str],
    *,
    grace_days: int = TRANSFER_LOGIN_GRACE_DAYS,
) -> Optional[str]:
    """舊角最後上榜是否落在可計入消失的轉移窗區間；回傳 label。"""
    dt = _parse(last_seen)
    if dt is None:
        return None
    pre = datetime.timedelta(hours=TRANSFER_DISAPPEAR_PRE_HOURS)
    for start_s, end_s, label in REALM_TRANSFER_WINDOWS:
        start, end = _parse(start_s), _parse(end_s)
        if start is None or end is None:
            continue
        if (start - pre) <= dt <= end:
            return label
    # 窗結束後 grace 內仍可能補登消失（延遲偵測）
    grace = datetime.timedelta(days=int(grace_days))
    for start_s, end_s, label in REALM_TRANSFER_WINDOWS:
        start, end = _parse(start_s), _parse(end_s)
        if start is None or end is None:
            continue
        if end < dt <= (end + grace):
            return label
    return None


def transfer_calendar_health_notes(
    when: Optional[str] = None,
    *,
    grace_days: int = TRANSFER_LOGIN_GRACE_DAYS,
) -> list[str]:
    """健康摘要用：無官方後續窗時提示最近的預估日期。"""
    dt: datetime.datetime | None
    if when is None:
        from services.timeutil import now_naive_taipei

        dt = now_naive_taipei()
    else:
        dt = _parse(when)
    if dt is None:
        return []
    grace = datetime.timedelta(days=int(grace_days))
    future_or_active: list[str] = []
    for start_s, end_s, label in REALM_TRANSFER_WINDOWS:
        start, end = _parse(start_s), _parse(end_s)
        if start is None or end is None:
            continue
        if dt <= (end + grace):
            future_or_active.append(label)
    if not future_or_active:
        projected = []
        for start_s, _end_s, label in PROJECTED_REALM_TRANSFER_WINDOWS:
            start = _parse(start_s)
            if start is not None and start >= dt:
                projected.append((start_s, label))
        projection_note = (
            f" 最近推算為 {projected[0][1]}（{projected[0][0]}），但未經官方證實。"
            if projected
            else ""
        )
        return [
            "⚠️ 領域轉移日曆已無後續場次；請至官網確認後更新 "
            f"`game_event_windows.REALM_TRANSFER_WINDOWS`。{projection_note}"
        ]
    return []
