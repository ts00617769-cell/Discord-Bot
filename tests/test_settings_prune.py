"""bot_settings 去重 key 清理。"""
from services.settings_prune import (
    boss_reminder_prune_bound,
    overspeed_prune_bound,
    should_prune_setting_key,
)


def test_should_prune_overspeed_and_boss_keys():
    over_bound = overspeed_prune_bound("2026-05-20 00:00:00")
    boss_bound = boss_reminder_prune_bound("2026-05-20")

    assert should_prune_setting_key(
        "overspeed:2026-05-01 12:00:00|prev|全服|30",
        overspeed_bound=over_bound,
        boss_bound=boss_bound,
    )
    assert not should_prune_setting_key(
        "overspeed:2026-06-01 12:00:00|prev|全服|30",
        overspeed_bound=over_bound,
        boss_bound=boss_bound,
    )
    assert should_prune_setting_key(
        "boss_reminder:2026-05-01:12",
        overspeed_bound=over_bound,
        boss_bound=boss_bound,
    )
    assert not should_prune_setting_key(
        "boss_reminder:2026-05-21:09",
        overspeed_bound=over_bound,
        boss_bound=boss_bound,
    )
    assert not should_prune_setting_key(
        "alert_enabled",
        overspeed_bound=over_bound,
        boss_bound=boss_bound,
    )


def test_prune_bounds_format():
    assert overspeed_prune_bound("2026-01-01 00:00:00").startswith("overspeed:")
    assert boss_reminder_prune_bound("2026-01-01") == "boss_reminder:2026-01-01"
