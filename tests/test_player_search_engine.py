"""尋人引擎純函式與 retention 線上清理 SQL。"""
from __future__ import annotations

import datetime

from services.player_search_engine import causal_transfer_pairs, parse_track_target
from services.retention_windows import (
    build_search_keep_ranges,
    exp_history_outside_keep_sql,
)


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
