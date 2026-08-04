"""cleanup_db CLI 參數與 --for-search dry-run。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import cleanup_db
from db.schema import SCHEMA_VERSION


def _init_minimal_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (7);
            CREATE TABLE exp_history (
                record_time TEXT NOT NULL,
                player_name TEXT NOT NULL,
                server_name TEXT NOT NULL,
                level INTEGER,
                exp REAL,
                class_name TEXT,
                subjugation_grade INTEGER
            );
            CREATE TABLE transfer_alerts_log (
                old_name TEXT, old_server TEXT,
                new_name TEXT, new_server TEXT,
                alert_time TEXT
            );
            CREATE TABLE bot_settings (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        # 舊資料（應被 for-search 刪）與近期資料
        conn.execute(
            "INSERT INTO exp_history VALUES (?,?,?,?,?,?,?)",
            ("2025-01-01 12:00:00", "Old", "S1", 60, 2e12, "戰士", 1),
        )
        conn.execute(
            "INSERT INTO exp_history VALUES (?,?,?,?,?,?,?)",
            ("2026-07-26 12:00:00", "New", "S1", 60, 2e12, "戰士", 1),
        )
        conn.commit()
    finally:
        conn.close()
    assert SCHEMA_VERSION == 9


def test_validate_cleanup_args_rejects_wipe_with_for_search():
    parser = cleanup_db.build_cleanup_parser()
    args = parser.parse_args(["--wipe-history", "--for-search"])
    err = cleanup_db.validate_cleanup_args(args, ["--wipe-history", "--for-search"])
    assert err is not None
    assert "不可同時" in err


def test_validate_cleanup_args_for_search_negative_days():
    parser = cleanup_db.build_cleanup_parser()
    args = parser.parse_args(["--for-search", "--recent-days", "-1"])
    err = cleanup_db.validate_cleanup_args(args, ["--for-search", "--recent-days", "-1"])
    assert err is not None
    assert "recent-days" in err


def test_for_search_dry_run(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "search.db"
    _init_minimal_db(db_path)
    monkeypatch.setattr(cleanup_db, "_bot_appears_running", lambda: False)
    monkeypatch.setattr(
        "sys.argv",
        [
            "cleanup_db.py",
            "--db",
            str(db_path),
            "--for-search",
            "--dry-run",
            "--recent-days",
            "3",
            "--max-transfer-windows",
            "0",
        ],
    )
    code = cleanup_db.main()
    assert code == 0
    out = capsys.readouterr().out
    assert "尋人導向" in out
    assert "dry-run" in out
    # 不應刪除
    conn = sqlite3.connect(str(db_path))
    try:
        n = conn.execute("SELECT COUNT(*) FROM exp_history").fetchone()[0]
        assert n == 2
    finally:
        conn.close()


def test_for_search_deletes_outside_keep(tmp_path, monkeypatch):
    db_path = tmp_path / "search_del.db"
    _init_minimal_db(db_path)
    monkeypatch.setattr(cleanup_db, "_bot_appears_running", lambda: False)
    # 固定「現在」：New@07-26 落在 recent-days=3 內，Old@2025 被刪
    monkeypatch.setattr(
        "services.retention_windows.now_naive_taipei",
        lambda: __import__("datetime").datetime(2026, 7, 28, 12, 0, 0),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "cleanup_db.py",
            "--db",
            str(db_path),
            "--for-search",
            "--recent-days",
            "3",
            "--max-transfer-windows",
            "0",
            "--no-vacuum",
        ],
    )
    code = cleanup_db.main()
    assert code == 0
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT player_name FROM exp_history ORDER BY player_name"
        ).fetchall()
        assert rows == [("New",)]
    finally:
        conn.close()


def test_for_search_thins_transfer_window_middles(tmp_path, monkeypatch):
    db_path = tmp_path / "thin_del.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (7);
            CREATE TABLE exp_history (
                record_time TEXT NOT NULL,
                player_name TEXT NOT NULL,
                server_name TEXT NOT NULL,
                level INTEGER,
                exp REAL,
                class_name TEXT,
                subjugation_grade INTEGER
            );
            CREATE TABLE transfer_alerts_log (
                old_name TEXT, old_server TEXT,
                new_name TEXT, new_server TEXT,
                alert_time TEXT
            );
            CREATE TABLE bot_settings (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        # 第27次轉移窗 2026-07-01～07-05：同角同服多筆中間應刪
        for t, exp in [
            ("2026-07-01 13:00:00", 1.0),
            ("2026-07-02 13:00:00", 2.0),
            ("2026-07-03 13:00:00", 3.0),
            ("2026-07-05 13:00:00", 4.0),
        ]:
            conn.execute(
                "INSERT INTO exp_history VALUES (?,?,?,?,?,?,?)",
                (t, "Hero", "S1", 60, exp, "戰士", 1),
            )
        # 近三日（相對 mocked now）保留且不在 thin 窗內
        conn.execute(
            "INSERT INTO exp_history VALUES (?,?,?,?,?,?,?)",
            ("2026-07-26 12:00:00", "Hero", "S1", 60, 9.0, "戰士", 1),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(cleanup_db, "_bot_appears_running", lambda: False)
    # 固定「現在」讓 recent=3 不會蓋住 07-01～05 窗
    monkeypatch.setattr(
        "services.retention_windows.now_naive_taipei",
        lambda: __import__("datetime").datetime(2026, 7, 28, 12, 0, 0),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "cleanup_db.py",
            "--db",
            str(db_path),
            "--for-search",
            "--recent-days",
            "3",
            "--max-transfer-windows",
            "1",
            "--transfer-pad-days",
            "0",
            "--no-vacuum",
        ],
    )
    assert cleanup_db.main() == 0

    conn = sqlite3.connect(str(db_path))
    try:
        times = [
            r[0]
            for r in conn.execute(
                "SELECT record_time FROM exp_history ORDER BY record_time"
            ).fetchall()
        ]
        assert times == [
            "2026-07-01 13:00:00",
            "2026-07-05 13:00:00",
            "2026-07-26 12:00:00",
        ]
    finally:
        conn.close()
