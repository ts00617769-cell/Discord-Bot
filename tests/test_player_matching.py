"""尋人匹配純邏輯。"""
from __future__ import annotations

from services.game_event_windows import allow_class_mismatch_high, class_change_label
from services.player_matching import (
    class_compatible,
    confidence,
    gap_hours,
    is_unknown_class,
    observation_gap_hours,
    pick_soft_candidates,
    score,
)


def test_class_compatible_unknown_both():
    assert class_compatible("未知", None) is True
    assert class_compatible("太陽監視者", "未知") is False
    assert class_compatible("太陽監視者", "太陽監視者") is True


def test_gap_hours_and_overlap():
    assert gap_hours("2026-01-01 10:00:00", "2026-01-01 12:00:00") == 2.0
    assert (
        observation_gap_hours(
            "2026-01-01 10:00:00",
            "2026-01-01 12:00:00",
            "2026-01-01 11:00:00",
            "2026-01-01 13:00:00",
        )
        == 0.0
    )
    assert (
        observation_gap_hours(
            "2026-01-01 10:00:00",
            "2026-01-01 11:00:00",
            "2026-01-01 13:00:00",
            "2026-01-01 14:00:00",
        )
        == 2.0
    )


def test_confidence_high_near_identical():
    assert (
        confidence("太陽監視者", 10, "太陽監視者", 1e7, 10, 1.0, False)
        == "high"
    )


def test_confidence_medium_cross_server_large_gap():
    assert (
        confidence("太陽監視者", 10, "太陽監視者", 1e10, 10, 100.0, False)
        == "medium"
    )


def test_confidence_class_mismatch_tight_exact_is_high():
    """EXP 極近 + 觀測銜接 ≤72h + 職業不符 → 疑似轉職 high。"""
    assert (
        confidence(
            "香射手",
            10,
            "咒文刻印使",
            0,
            10,
            0.1,
            False,
            a_last="2026-06-05 21:46:00",
            b_first="2026-06-05 21:51:00",
        )
        == "high"
    )


def test_confidence_class_mismatch_outside_window_far_is_medium():
    assert (
        confidence(
            "香射手",
            10,
            "咒文刻印使",
            0,
            10,
            200.0,
            False,
            a_last="2026-01-01 10:00:00",
            b_first="2026-01-10 10:00:00",
        )
        == "medium"
    )


def test_allow_class_mismatch_in_official_window():
    # 落在 2週年自由職業變更視窗，可放寬到 7 天內
    assert allow_class_mismatch_high(
        "2026-06-12 10:00:00",
        "2026-06-15 10:00:00",
        obs_gap_hours=72.0 + 1,
        exact_exp=True,
    )
    assert class_change_label("2026-06-12 10:00:00", "2026-06-15 10:00:00")


def test_class_change_product_sale_window():
    """職業變更商品販售期間 = 可轉職時段。"""
    assert (
        class_change_label("2025-08-10 12:00:00", "2025-08-10 13:00:00")
        == "職業變更商品"
    )
    assert class_change_label("2025-04-15 12:00:00", "2025-04-15 13:00:00")


def test_realm_transfer_26th_matches_screenshot_bridge():
    from services.game_event_windows import realm_transfer_label

    # 截圖：驕傲o ~06-05 21:46 → 傲嬌o 06-05 21:51（第26次轉移窗內）
    assert (
        realm_transfer_label("2026-06-05 21:46:00", "2026-06-05 21:51:00")
        == "第26次領域轉移"
    )


def test_realm_transfer_delayed_login_two_days():
    """窗內消失（例如週五夜），兩天後才登入新服上榜。"""
    from services.game_event_windows import (
        allow_delayed_transfer_high,
        match_realm_transfer,
    )

    # 第26次：06/03 12:00 ~ 06/07 23:59；06/07 23:50 消失 → 06/09 21:00 才出現
    assert (
        match_realm_transfer("2026-06-07 23:50:00", "2026-06-09 21:00:00")
        == "第26次領域轉移"
    )
    assert allow_delayed_transfer_high(
        "2026-06-07 23:50:00",
        "2026-06-09 21:00:00",
        obs_gap_hours=45.0,
        exact_exp=True,
    )


def test_realm_transfer_pre_hours_and_grace_bounds():
    """消失可略早於窗開始（PRE_HOURS）；出現不可晚於窗結束+grace。"""
    from services.game_event_windows import (
        TRANSFER_DISAPPEAR_PRE_HOURS,
        TRANSFER_LOGIN_GRACE_DAYS,
        match_realm_transfer,
    )

    assert TRANSFER_DISAPPEAR_PRE_HOURS == 24
    assert TRANSFER_LOGIN_GRACE_DAYS == 3
    # 第26次 06/03 12:00 開始；前 24h 內消失 OK
    assert (
        match_realm_transfer("2026-06-02 12:00:00", "2026-06-04 12:00:00")
        == "第26次領域轉移"
    )
    # 再早一小時 → 不符
    assert match_realm_transfer("2026-06-02 11:00:00", "2026-06-04 12:00:00") is None
    # 窗結束 06/07 23:59 + 3 天 = 06/10 23:59；邊界內 OK
    assert (
        match_realm_transfer("2026-06-07 20:00:00", "2026-06-10 23:59:59")
        == "第26次領域轉移"
    )
    # 超過 grace
    assert match_realm_transfer("2026-06-07 20:00:00", "2026-06-11 00:00:00") is None


def test_confidence_delayed_transfer_same_class_is_high():
    assert (
        confidence(
            "香射手",
            10,
            "香射手",
            0,
            10,
            48.0,
            False,
            a_last="2026-06-07 23:50:00",
            b_first="2026-06-09 21:00:00",
        )
        == "high"
    )


def test_score_prefers_same_class():
    same = score("A", 5, 60, 1e8, 1.0, "A", 5, 60, False, forward=True)
    other = score("A", 5, 60, 1e8, 1.0, "B", 5, 60, False, forward=True)
    assert same < other


def test_pick_soft_candidates_limits_per_direction():
    cands = [
        {"name": f"n{i}", "server": "s", "direction": "forward", "score": i, "exp_diff": i}
        for i in range(5)
    ] + [
        {
            "name": f"b{i}",
            "server": "s",
            "direction": "backward",
            "score": i,
            "exp_diff": i,
        }
        for i in range(5)
    ]
    picked = pick_soft_candidates(cands, exclude_keys=[], per_direction=2)
    assert len([c for c in picked if c["direction"] == "forward"]) == 2
    assert len([c for c in picked if c["direction"] == "backward"]) == 2


def test_is_unknown_class():
    assert is_unknown_class("未知")
    assert not is_unknown_class("MirageBlade")
