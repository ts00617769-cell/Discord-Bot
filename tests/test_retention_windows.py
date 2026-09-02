"""尋人導向保留區間。"""
import sqlite3
from datetime import datetime

from services.retention_windows import (
    build_bridge_thin_ranges,
    build_search_keep_ranges,
    build_transfer_thin_ranges,
    exp_history_outside_keep_batch_sql,
    exp_history_outside_keep_sql,
    exp_history_transfer_middle_sql,
    exp_history_transfer_middle_statements,
    merge_ranges,
    search_retention_cutoff,
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


def test_defaults_keep_contiguous_and_thin_bridge():
    """轉移窗至 recent 之間連續保留，橋接區另行稀疏化。"""
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
        ("2026-07-01 12:00:00", "2026-07-21 04:00:00"),
    ]
    assert build_bridge_thin_ranges(
        recent_days=3,
        pad_days=3,
        max_transfer_windows=3,
        now=datetime(2026, 7, 21, 4, 0, 0),
        transfer_windows=windows,
    ) == [
        ("2026-07-09 00:00:00", "2026-07-18 03:59:59"),
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
        ("2026-05-06 12:00:00", "2026-07-21 04:00:00"),
    ]


def test_select_skips_future_windows():
    """尚未開始的預估窗不佔最近 K 次名額。"""
    windows = [
        ("2026-05-06 12:00:00", "2026-05-10 23:59:59", "第25次"),
        ("2026-06-03 12:00:00", "2026-06-07 23:59:59", "第26次"),
        ("2026-07-01 12:00:00", "2026-07-05 23:59:59", "第27次"),
        ("2026-07-29 12:00:00", "2026-08-02 23:59:59", "第28次尚未開始"),
    ]
    selected = select_recent_transfer_windows(
        windows, max_windows=3, now=datetime(2026, 7, 28, 9, 0, 0)
    )
    assert [w[2] for w in selected] == ["第27次", "第26次", "第25次"]


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


def test_outside_keep_batch_sql_has_limit():
    ranges = [("2026-05-01 00:00:00", "2026-05-20 00:00:00")]
    sql, params = exp_history_outside_keep_batch_sql(ranges, batch_size=1000)
    assert "LIMIT ?" in sql
    assert params[-1] == 1000
    assert params[:2] == ("2026-05-01 00:00:00", "2026-05-20 00:00:00")


def test_thin_ranges_include_pad():
    windows = [
        ("2026-06-03 12:00:00", "2026-06-07 23:59:59", "第26次領域轉移"),
        ("2026-07-01 12:00:00", "2026-07-05 23:59:59", "第27次領域轉移"),
    ]
    thin = build_transfer_thin_ranges(
        max_transfer_windows=3,
        pad_days=3,
        now=datetime(2026, 7, 21, 4, 0, 0),
        transfer_windows=windows,
    )
    assert thin == [
        ("2026-06-03 12:00:00", "2026-06-10 23:59:59"),
        ("2026-07-01 12:00:00", "2026-07-08 23:59:59"),
    ]
    keep = build_search_keep_ranges(
        recent_days=0,
        pad_days=3,
        max_transfer_windows=3,
        now=datetime(2026, 7, 21, 4, 0, 0),
        transfer_windows=windows,
    )
    # keep 是連續 envelope；thin 仍個別依窗+pad
    assert keep == [("2026-06-03 12:00:00", "2026-07-21 04:00:00")]


def test_transfer_middle_sql_params_match_window():
    sql, params = exp_history_transfer_middle_sql(
        "2026-07-01 12:00:00",
        "2026-07-05 23:59:59",
        for_delete=True,
    )
    assert sql.strip().startswith("DELETE FROM exp_history")
    assert params == (
        "2026-07-01 12:00:00",
        "2026-07-05 23:59:59",
        "2026-07-01 12:00:00",
        "2026-07-05 23:59:59",
    )
    assert exp_history_transfer_middle_statements([]) == []


def test_transfer_middle_keeps_first_last_per_name_server(tmp_path):
    db = tmp_path / "thin.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """
            CREATE TABLE exp_history (
                record_time TEXT NOT NULL,
                player_name TEXT NOT NULL,
                server_name TEXT NOT NULL,
                level INTEGER,
                exp REAL
            )
            """
        )
        rows = [
            # A@S1：五筆 → 只留首尾
            ("2026-07-01 13:00:00", "A", "S1", 60, 1.0),
            ("2026-07-02 13:00:00", "A", "S1", 60, 2.0),
            ("2026-07-03 13:00:00", "A", "S1", 60, 3.0),
            ("2026-07-04 13:00:00", "A", "S1", 60, 4.0),
            ("2026-07-05 13:00:00", "A", "S1", 60, 5.0),
            # A@S2：兩筆都留
            ("2026-07-02 13:00:00", "A", "S2", 60, 10.0),
            ("2026-07-04 13:00:00", "A", "S2", 60, 11.0),
            # B@S1：單筆不變
            ("2026-07-03 13:00:00", "B", "S1", 60, 7.0),
            # 窗外：不應被 sparse 刪
            ("2026-07-10 13:00:00", "A", "S1", 60, 99.0),
        ]
        conn.executemany(
            "INSERT INTO exp_history VALUES (?,?,?,?,?)",
            rows,
        )
        conn.commit()

        start, end = "2026-07-01 12:00:00", "2026-07-05 23:59:59"
        count_sql, count_params = exp_history_transfer_middle_sql(
            start, end, for_delete=False
        )
        assert conn.execute(count_sql, count_params).fetchone()[0] == 3

        del_sql, del_params = exp_history_transfer_middle_sql(
            start, end, for_delete=True
        )
        deleted = conn.execute(del_sql, del_params).rowcount
        conn.commit()
        assert deleted == 3

        kept = conn.execute(
            """
            SELECT record_time, player_name, server_name, exp
            FROM exp_history
            ORDER BY player_name, server_name, record_time
            """
        ).fetchall()
        assert kept == [
            ("2026-07-01 13:00:00", "A", "S1", 1.0),
            ("2026-07-05 13:00:00", "A", "S1", 5.0),
            ("2026-07-10 13:00:00", "A", "S1", 99.0),
            ("2026-07-02 13:00:00", "A", "S2", 10.0),
            ("2026-07-04 13:00:00", "A", "S2", 11.0),
            ("2026-07-03 13:00:00", "B", "S1", 7.0),
        ]
    finally:
        conn.close()


def test_default_retention_is_three_days_and_pad_three():
    from services.retention_windows import (
        DEFAULT_RECENT_DAYS,
        DEFAULT_TRANSFER_PAD_DAYS,
    )

    assert DEFAULT_RECENT_DAYS == 3
    assert DEFAULT_TRANSFER_PAD_DAYS == 3


def test_transfer_calendar_health_notes_has_remaining():
    from services.game_event_windows import transfer_calendar_health_notes

    # #29～#33 已進正式日曆；08-10 仍在 #29 有效期內，不應警告
    assert transfer_calendar_health_notes("2026-08-10 12:00:00") == []


def test_transfer_calendar_health_notes_exhausted():
    from services.game_event_windows import transfer_calendar_health_notes

    notes = transfer_calendar_health_notes("2027-01-15 12:00:00")
    assert notes
    assert any("已無後續" in n for n in notes)
    assert any("2026-12-31" in n for n in notes)
    assert any("無需再更新" in n for n in notes)
    assert not any("請至官網" in n for n in notes)


def test_transfer_calendar_health_notes_warns_before_expiry():
    from services.game_event_windows import transfer_calendar_health_notes

    # 第33次結束 2026-12-20 + grace 3 日 ≈ 12-23；在 12-12 應提示最後一場／關服
    notes = transfer_calendar_health_notes(
        "2026-12-12 12:00:00", warn_within_days=14
    )
    assert notes
    assert any("最後一場" in n for n in notes)
    assert any("第33次" in n for n in notes)
    assert any("2026-12-31" in n for n in notes)
    assert any("無需再補日曆" in n for n in notes)
    assert not any("請至官網" in n for n in notes)
