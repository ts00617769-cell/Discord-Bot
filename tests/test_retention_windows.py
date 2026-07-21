"""尋人導向保留區間。"""
from datetime import datetime

from services.retention_windows import (
    build_search_keep_ranges,
    exp_history_outside_keep_sql,
    merge_ranges,
    select_recent_transfer_windows,
)


def test_merge_ranges_overlaps_and_adjacent():
    merged = merge_ranges(
        [
            ("2026-06-01 00:00:00", "2026-06-10 00:00:00"),
            ("2026-06-10 00:00:00", "2026-06-15 00:00:00"),  # 相接
            ("2026-06-20 00:00:00", "2026-06-25 00:00:00"),
            ("2026-06-22 00:00:00", "2026-06-28 00:00:00"),  # 重疊
        ]
    )
    assert merged == [
        ("2026-06-01 00:00:00", "2026-06-15 00:00:00"),
        ("2026-06-20 00:00:00", "2026-06-28 00:00:00"),
    ]


def test_transfer_pad_after_only():
    """只留窗開始～結束後 pad，不含轉移前（延遲登入緩衝）。"""
    windows = [
        ("2026-06-03 12:00:00", "2026-06-07 23:59:59", "第26次領域轉移"),
    ]
    ranges = build_search_keep_ranges(
        recent_days=0,
        pad_days=3,
        max_transfer_windows=3,
        now=datetime(2026, 6, 7, 23, 59, 59),
        transfer_windows=windows,
    )
    assert ranges == [
        ("2026-06-03 12:00:00", "2026-06-10 23:59:59"),
    ]


def test_defaults_transfer_and_recent_may_gap():
    """預設 轉移後+3 / 近3：第27次與最近區間中間可能留空洞。"""
    windows = [
        ("2026-07-01 12:00:00", "2026-07-05 23:59:59", "第27次領域轉移"),
    ]
    # transfer: 07-01 12:00 ~ 07-08 23:59；recent3 from 07-21 04:00 → 07-18 04:00
    ranges = build_search_keep_ranges(
        recent_days=3,
        pad_days=3,
        max_transfer_windows=3,
        now=datetime(2026, 7, 21, 4, 0, 0),
        transfer_windows=windows,
    )
    assert ranges == [
        ("2026-07-01 12:00:00", "2026-07-08 23:59:59"),
        ("2026-07-18 04:00:00", "2026-07-21 04:00:00"),
    ]


def test_recent_and_transfer_merge_when_overlap():
    """結束後 pad 與 recent 重疊時應合併。"""
    windows = [
        ("2026-07-01 12:00:00", "2026-07-05 23:59:59", "第27次領域轉移"),
    ]
    ranges = build_search_keep_ranges(
        recent_days=14,
        pad_days=10,
        max_transfer_windows=3,
        now=datetime(2026, 7, 21, 4, 0, 0),
        transfer_windows=windows,
    )
    assert len(ranges) == 1
    assert ranges[0][0] == "2026-07-01 12:00:00"
    assert ranges[0][1] == "2026-07-21 04:00:00"


def test_only_last_n_transfer_windows():
    windows = [
        ("2026-05-06 12:00:00", "2026-05-10 23:59:59", "第25次"),
        ("2026-06-03 12:00:00", "2026-06-07 23:59:59", "第26次"),
        ("2026-07-01 12:00:00", "2026-07-05 23:59:59", "第27次"),
        ("2026-04-01 12:00:00", "2026-04-05 23:59:59", "第24次"),
    ]
    selected = select_recent_transfer_windows(
        windows, max_windows=3, now=datetime(2026, 7, 21, 4, 0, 0)
    )
    labels = [w[2] for w in selected]
    assert labels == ["第27次", "第26次", "第25次"]

    ranges = build_search_keep_ranges(
        recent_days=0,
        pad_days=0,
        max_transfer_windows=3,
        now=datetime(2026, 7, 21, 4, 0, 0),
        transfer_windows=windows,
    )
    assert ranges == [
        ("2026-05-06 12:00:00", "2026-05-10 23:59:59"),
        ("2026-06-03 12:00:00", "2026-06-07 23:59:59"),
        ("2026-07-01 12:00:00", "2026-07-05 23:59:59"),
    ]


def test_outside_keep_sql_params_match_ranges():
    ranges = [
        ("2026-05-01 00:00:00", "2026-05-20 00:00:00"),
        ("2026-06-01 00:00:00", "2026-06-20 00:00:00"),
    ]
    count_sql, count_params = exp_history_outside_keep_sql(ranges)
    delete_sql, delete_params = exp_history_outside_keep_sql(ranges, for_delete=True)

    assert count_sql.startswith("SELECT COUNT(*) FROM exp_history WHERE NOT (")
    assert delete_sql.startswith("DELETE FROM exp_history WHERE NOT (")
    assert count_params == delete_params
    assert len(count_params) == 4
    assert count_params == (
        "2026-05-01 00:00:00",
        "2026-05-20 00:00:00",
        "2026-06-01 00:00:00",
        "2026-06-20 00:00:00",
    )


def test_empty_ranges_safe_no_delete():
    sql, params = exp_history_outside_keep_sql([], for_delete=True)
    assert sql == "DELETE FROM exp_history WHERE 0"
    assert params == ()
