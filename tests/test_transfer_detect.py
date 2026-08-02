"""轉服偵測純邏輯。"""
from __future__ import annotations

from services.transfer_detect import (
    candidate_score,
    format_exp_diff,
    pick_unique_pairs,
    rank_transfer_candidates,
    rename_allowed,
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
    new_guild="",
    old_guild="",
    old_last_seen="2026-07-30 12:00:00",
):
    return (
        new_exp,
        new_name,
        new_server,
        new_lvl,
        new_cls,
        new_guild,
        old_name,
        old_server,
        old_lvl,
        old_cls,
        old_exp,
        old_guild,
        new_sub,
        old_sub,
        old_last_seen,
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
    ranked = rank_transfer_candidates(
        [rename, same],
        appear_time="2026-07-30 12:10:00",
        in_active_period=True,
    )
    assert ranked[0][1] == "A"


def test_rank_prefers_same_guild():
    same_g = _row(
        2e12,
        "新1",
        "B",
        60,
        "太陽監視者",
        "舊1",
        "A",
        60,
        "太陽監視者",
        2e12,
        new_guild="狼團",
        old_guild="狼團",
    )
    diff_g = _row(
        2e12,
        "新2",
        "B",
        60,
        "太陽監視者",
        "舊2",
        "A",
        60,
        "太陽監視者",
        2e12,
        new_guild="X",
        old_guild="Y",
    )
    ranked = rank_transfer_candidates(
        [diff_g, same_g],
        appear_time="2026-07-30 12:10:00",
        in_active_period=True,
    )
    assert ranked[0][1] == "新1"


def test_outside_window_only_same_name():
    same = _row(2e12, "A", "B", 60, "太陽監視者", "A", "C", 60, "太陽監視者", 2e12)
    rename = _row(2e12, "新", "B", 60, "太陽監視者", "舊", "C", 60, "太陽監視者", 2e12)
    ranked = rank_transfer_candidates(
        [rename, same],
        appear_time="2026-06-15 12:00:00",
        in_active_period=False,
    )
    assert len(ranked) == 1
    assert ranked[0][1] == "A"


def test_rename_without_guild_needs_strict_margin():
    loose = _row(
        2e12 + 50 * 100_000_000,
        "新",
        "B",
        60,
        "太陽監視者",
        "舊",
        "A",
        60,
        "太陽監視者",
        2e12,
    )
    assert rename_allowed(loose, cohort_boost=False, in_transfer_window=True) is False
    tight = _row(
        2e12 + 5 * 100_000_000,
        "新",
        "B",
        60,
        "太陽監視者",
        "舊",
        "A",
        60,
        "太陽監視者",
        2e12,
    )
    assert rename_allowed(tight, cohort_boost=False, in_transfer_window=True) is True
    assert rename_allowed(loose, cohort_boost=True, in_transfer_window=True) is True


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


def test_ambiguous_skip_close_scores():
    # 同一新角兩個舊角，EXP 差相同 → ambiguous
    r1 = _row(2e12, "N1", "B", 60, "太陽監視者", "O1", "A", 60, "太陽監視者", 2e12)
    r2 = _row(2e12, "N1", "B", 60, "太陽監視者", "O2", "A", 60, "太陽監視者", 2e12)
    pairs = pick_unique_pairs([r1, r2], set(), ambiguous_gap=40)
    assert pairs == []


def test_guild_breaks_ambiguity():
    r1 = _row(
        2e12,
        "N1",
        "B",
        60,
        "太陽監視者",
        "O1",
        "A",
        60,
        "太陽監視者",
        2e12,
        new_guild="狼團",
        old_guild="狼團",
    )
    r2 = _row(
        2e12,
        "N1",
        "B",
        60,
        "太陽監視者",
        "O2",
        "A",
        60,
        "太陽監視者",
        2e12,
        new_guild="狼團",
        old_guild="別團",
    )
    assert candidate_score(r1) > candidate_score(r2)
    pairs = pick_unique_pairs([r1, r2], set(), ambiguous_gap=40)
    assert len(pairs) == 1
    assert pairs[0]["old_name"] == "O1"
    assert pairs[0]["old_guild"] == "狼團"


def test_format_and_status():
    assert "完美吻合" in format_exp_diff(0)
    assert "億" in format_exp_diff(5e10)
    assert transfer_status("A", "A") == "跨服轉移"
    assert transfer_status("B", "A") == "跨服轉移並改名"


def test_transfer_sort_key_same_name_beats_rename():
    same = _row(2e12, "同", "B", 60, "太陽監視者", "同", "A", 60, "太陽監視者", 2e12)
    rename = _row(2e12, "新", "B", 60, "太陽監視者", "舊", "A", 60, "太陽監視者", 2e12)
    assert transfer_sort_key(same) < transfer_sort_key(rename)


def test_cohort_boost_in_rank():
    rows = []
    for i in range(3):
        rows.append(
            _row(
                2e12 + i,
                f"N{i}",
                "B",
                60,
                "太陽監視者",
                f"O{i}",
                "A",
                60,
                "太陽監視者",
                2e12 + i,
                new_guild=f"G{i}",
                old_guild="狼團",
            )
        )
    # 非 cohort 單人
    alone = _row(
        2e12,
        "NX",
        "B",
        60,
        "太陽監視者",
        "OX",
        "A",
        60,
        "太陽監視者",
        2e12,
        new_guild="",
        old_guild="獨行",
    )
    ranked = rank_transfer_candidates(
        rows + [alone],
        appear_time="2026-07-30 12:10:00",
        in_active_period=True,
    )
    # cohort 成員應排在獨行異名之前（此處皆異名）
    assert ranked[-1][6] == "OX"
