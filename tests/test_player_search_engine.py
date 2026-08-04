"""尋人引擎純函式與 retention 線上清理 SQL。"""
from __future__ import annotations

import datetime
import sqlite3

import aiosqlite
import pytest

from db.schema import apply_migrations, rebuild_player_profiles_sync
from services.player_search_db import PlayerSearchStore, normalize_profile_rows
from services.player_search_engine import (
    build_causal_scan_sections,
    causal_transfer_pairs,
    group_scan_records_by_exp,
    parse_track_target,
    run_track_search,
)
from services.retention_windows import (
    build_search_keep_ranges,
    exp_history_outside_keep_sql,
    search_retention_cutoff,
)


def test_normalize_profile_rows_single_tuple():
    row = ("A", "S1", 10, "2026-07-01 10:00:00", "2026-07-02 10:00:00", 1.0, 2.0, "戰士", 1)
    assert normalize_profile_rows(None) == []
    assert normalize_profile_rows(row) == [row]
    assert normalize_profile_rows([row]) == [row]


def test_parse_track_target_with_server():
    servers = {"萊涅01", "困特03"}
    name, server = parse_track_target("小碎冰 萊涅01", servers)
    assert name == "小碎冰"
    assert server == "萊涅01"


def test_parse_track_target_without_server():
    name, server = parse_track_target("小碎冰", {"萊涅01"})
    assert name == "小碎冰"
    assert server is None


def test_causal_transfer_pairs():
    players = [
        {
            "name": "A",
            "server": "S1",
            "first": "2026-07-01 10:00:00",
            "last": "2026-07-02 10:00:00",
            "cls": "戰士",
        },
        {
            "name": "B",
            "server": "S2",
            "first": "2026-07-02 12:00:00",
            "last": "2026-07-03 10:00:00",
            "cls": "戰士",
        },
        {
            "name": "C",
            "server": "S3",
            "first": "2026-07-01 11:00:00",
            "last": "2026-07-03 11:00:00",
            "cls": "戰士",
        },
    ]
    pairs = causal_transfer_pairs(players)
    assert len(pairs) == 1
    earlier, later, gap = pairs[0]
    assert earlier["name"] == "A"
    assert later["name"] == "B"
    assert gap >= 0


def test_group_and_build_causal_scan_sections():
    exp = 2_000_000_000_000
    records = [
        (exp, "A", "S1", "2026-07-01 10:00:00", "2026-07-02 10:00:00", 60, "戰士", 1),
        (exp, "B", "S2", "2026-07-02 12:00:00", "2026-07-03 10:00:00", 60, "戰士", 1),
    ]
    grouped = group_scan_records_by_exp(records)
    sections = build_causal_scan_sections(grouped)
    assert len(sections) == 1
    assert sections[0]["exp"] == exp
    assert len(sections[0]["pairs"]) == 1


def test_online_cleanup_delete_sql_matches_for_search():
    now = datetime.datetime(2026, 7, 27, 12, 0, 0)
    ranges = build_search_keep_ranges(
        recent_days=3,
        pad_days=3,
        max_transfer_windows=0,
        now=now,
        transfer_windows=[],
    )
    sql, params = exp_history_outside_keep_sql(ranges, for_delete=True)
    assert sql.startswith("DELETE FROM exp_history")
    assert "NOT (" in sql
    assert len(params) == 2


def test_search_retention_cutoff_matches_recent_days_without_transfer_windows():
    now = datetime.datetime(2026, 7, 27, 12, 0, 0)
    cutoff = search_retention_cutoff(
        recent_days=3, max_transfer_windows=0, now=now
    )
    assert cutoff == "2026-07-24 12:00:00"


@pytest.mark.asyncio
async def test_run_track_search_with_server_filter(tmp_path):
    db_path = tmp_path / "track.db"
    db = await aiosqlite.connect(str(db_path))
    try:
        await apply_migrations(db)
        await db.executemany(
            """
            INSERT INTO exp_history
            (record_time, server_name, player_name, level, exp, class_name, subjugation_grade)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("2026-07-01 10:00:00", "S1", "Target", 50, 1e12, "戰士", 5),
                ("2026-07-02 10:00:00", "S1", "Target", 51, 1.01e12, "戰士", 5),
                ("2026-07-01 10:00:00", "S2", "Target", 50, 1e12, "戰士", 5),
            ],
        )
        await db.commit()
    finally:
        await db.close()

    conn = sqlite3.connect(str(db_path))
    try:
        rebuild_player_profiles_sync(conn)
    finally:
        conn.close()

    db = await aiosqlite.connect(str(db_path))
    try:
        store = PlayerSearchStore(db)
        result = await run_track_search(store, db, "Target", server_name="S1")
        assert result.kind in ("linked", "no_link", "soft")
        seeds = [
            e for e in result.unique_entries if e["match_type"] == "🎯 查詢目標"
        ]
        assert len(seeds) == 1
        assert seeds[0]["server"] == "S1"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_run_track_search_server_filter_limits_seed(tmp_path):
    """指定伺服器時，seed 只含該服的目標履歷。"""
    db_path = tmp_path / "server_seed.db"
    db = await aiosqlite.connect(str(db_path))
    try:
        await apply_migrations(db)
        await db.executemany(
            """
            INSERT INTO exp_history
            (record_time, server_name, player_name, level, exp, class_name, subjugation_grade)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("2026-07-01 10:00:00", "S1", "Target", 50, 1e12, "戰士", 5),
                ("2026-07-01 10:00:00", "S2", "Target", 50, 2e12, "戰士", 5),
                ("2026-07-01 11:00:00", "S1", "Other", 50, 1.5e12, "戰士", 5),
            ],
        )
        await db.commit()
    finally:
        await db.close()

    conn = sqlite3.connect(str(db_path))
    try:
        rebuild_player_profiles_sync(conn)
    finally:
        conn.close()

    db = await aiosqlite.connect(str(db_path))
    try:
        store = PlayerSearchStore(db)
        result = await run_track_search(store, db, "Target", server_name="S1")
        seeds = [
            (e["name"], e["server"])
            for e in result.unique_entries
            if e["match_type"] == "🎯 查詢目標"
        ]
        assert ("Target", "S1") in seeds
        assert ("Target", "S2") not in seeds
    finally:
        await db.close()
