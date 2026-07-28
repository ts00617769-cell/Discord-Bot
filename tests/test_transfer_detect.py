"""轉服偵測純邏輯。"""
from __future__ import annotations

from services.transfer_detect import (
    format_exp_diff,
    pick_unique_pairs,
    rank_transfer_candidates,
    should_skip_rename_mismatch,
    transfer_sort_key,
    transfer_status,
)


def _row(
    new_exp,
    new_name,
    new_server,
    new_lvl,
    new_cls,
    old_name,
    old_server,
    old_lvl,
    old_cls,
    old_exp,
    new_sub=10,
    old_sub=10,
):
    return (
        new_exp,
        new_name,
        new_server,
        new_lvl,
        new_cls,
        old_name,
        old_server,
        old_lvl,
        old_cls,
        old_exp,
        new_sub,
        old_sub,
    )


def test_skip_rename_when_subjugation_mismatch():
    row = _row(
        2e12, "新", "B", 60, "太陽監視者", "舊", "A", 60, "太陽監視者", 2e12, 10, 9
    )
    assert should_skip_rename_mismatch(row) is True
    same = _row(
        2e12, "新", "B", 60, "太陽監視者", "舊", "A", 60, "太陽監視者", 2e12, 10, 10
    )
    assert should_skip_rename_mismatch(same) is False


def test_same_name_never_skipped_for_sub():
    row = _row(2e12, "同名", "B", 60, "太陽監視者", "同名", "A", 60, "太陽監視者", 2e12, 10, 9)
    assert should_skip_rename_mismatch(row) is False


def test_rank_prefers_same_name():
    same = _row(2e12, "A", "B", 60, "太陽監視者", "A", "C", 60, "太陽監視者", 2e12)
    rename = _row(2e12, "新", "B", 60, "太陽監視者", "舊", "C", 60, "太陽監視者", 2e12)
    ranked = rank_transfer_candidates([rename, same])
    assert ranked[0][1] == "A"


def test_pick_unique_pairs_one_to_one():
    r1 = _row(2e12, "N1", "B", 60, "太陽監視者", "O1", "A", 60, "太陽監視者", 2e12)
    r2 = _row(2.1e12, "N1", "B", 60, "太陽監視者", "O2", "A", 60, "太陽監視者", 2e12)
    pairs = pick_unique_pairs([r1, r2], set())
    assert len(pairs) == 1
    assert pairs[0]["old_name"] == "O1"


def test_pick_unique_pairs_skips_already_alerted():
    r1 = _row(2e12, "N1", "B", 60, "太陽監視者", "O1", "A", 60, "太陽監視者", 2e12)
    already = {("O1", "A", "N1", "B")}
    assert pick_unique_pairs([r1], already) == []


def test_format_and_status():
    assert "完美吻合" in format_exp_diff(0)
    assert "億" in format_exp_diff(5e10)
    assert transfer_status("A", "A") == "跨服轉移"
    assert transfer_status("B", "A") == "跨服轉移並改名"


def test_transfer_sort_key_same_name_beats_rename():
    same = _row(2e12, "同", "B", 60, "太陽監視者", "同", "A", 60, "太陽監視者", 2e12)
    rename = _row(2e12, "新", "B", 60, "太陽監視者", "舊", "A", 60, "太陽監視者", 2e12)
    assert transfer_sort_key(same) < transfer_sort_key(rename)
