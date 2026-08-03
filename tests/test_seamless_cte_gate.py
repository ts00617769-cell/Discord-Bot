"""denorm 就緒時 CTE 僅在最窄 tier 且候選為空才補跑。"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services.player_search_db import PlayerSearchStore


@pytest.mark.asyncio
async def test_cte_fallback_only_when_denorm_empty_on_narrowest_tier():
    store = PlayerSearchStore(db=None)
    store._has_denorm_stats = AsyncMock(return_value=True)
    store._early_window_min_exp = AsyncMock(return_value=None)

    calls: list[bool] = []

    async def fake_one_margin(**kwargs):
        use_denorm = kwargs["use_denorm"]
        calls.append(use_denorm)
        if use_denorm:
            # 窄 margin 已有候選 → 不應再跑 CTE
            return [
                {
                    "name": "X",
                    "server": "S2",
                    "direction": "forward",
                    "confidence": "medium",
                    "score": 10,
                    "exp_val": 1e12,
                    "lvl": 50,
                    "cls": "戰士",
                    "first": "2026-07-02 10:00:00",
                    "last": "2026-07-02 12:00:00",
                    "min_exp": 1e12,
                    "max_exp": 1.01e12,
                    "sub_grade": 5,
                    "match_type": "test",
                    "diff_text": "",
                }
            ]
        return []

    store._seamless_one_margin = AsyncMock(side_effect=fake_one_margin)

    profile = (
        "Old",
        "S1",
        50,
        "2026-07-01 10:00:00",
        "2026-07-01 12:00:00",
        1e12,
        1.01e12,
        "戰士",
        5,
    )
    await store._find_seamless_candidates(profile, exp_margin=None, limit=5)

    assert calls  # at least denorm path
    assert False not in calls  # no CTE (use_denorm=False)


@pytest.mark.asyncio
async def test_cte_runs_when_narrowest_denorm_empty():
    store = PlayerSearchStore(db=None)
    store._has_denorm_stats = AsyncMock(return_value=True)
    store._early_window_min_exp = AsyncMock(return_value=None)

    calls: list[bool] = []

    async def fake_one_margin(**kwargs):
        use_denorm = kwargs["use_denorm"]
        calls.append(use_denorm)
        if use_denorm:
            return []
        return [
            {
                "name": "Y",
                "server": "S2",
                "direction": "forward",
                "confidence": "high",
                "score": 1,
                "exp_val": 1e12,
                "lvl": 50,
                "cls": "戰士",
                "first": "2026-07-02 10:00:00",
                "last": "2026-07-02 12:00:00",
                "min_exp": 1e12,
                "max_exp": 1.01e12,
                "sub_grade": 5,
                "match_type": "test",
                "diff_text": "",
            }
        ]

    store._seamless_one_margin = AsyncMock(side_effect=fake_one_margin)

    profile = (
        "Old",
        "S1",
        50,
        "2026-07-01 10:00:00",
        "2026-07-01 12:00:00",
        1e12,
        1.01e12,
        "戰士",
        5,
    )
    cands = await store._find_seamless_candidates(profile, exp_margin=None, limit=5)
    assert ("Y", "S2") in {(c["name"], c["server"]) for c in cands}
    assert False in calls  # CTE ran once
