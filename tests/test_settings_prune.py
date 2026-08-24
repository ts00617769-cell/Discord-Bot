"""bot_settings 去重 key 清理。"""
from services.settings_prune import (
    boss_reminder_prune_bound,
    overspeed_prune_bound,
)


def test_prune_bounds_format():
    assert overspeed_prune_bound("2026-01-01 00:00:00").startswith("overspeed:")
    assert boss_reminder_prune_bound("2026-01-01") == "boss_reminder:2026-01-01"
