"""bot_settings 去重 key 清理輔助。"""
from __future__ import annotations


def overspeed_prune_bound(cutoff_datetime_str: str) -> str:
    """字串比較上界：key < 此值的 overspeed:* 可刪。"""
    return f"overspeed:{cutoff_datetime_str}"


def boss_reminder_prune_bound(cutoff_date_str: str) -> str:
    """字串比較上界：key < 此值的 boss_reminder:* 可刪（日期 YYYY-MM-DD）。"""
    return f"boss_reminder:{cutoff_date_str}"


def should_prune_setting_key(
    key: str, *, overspeed_bound: str, boss_bound: str
) -> bool:
    if key.startswith("overspeed:") and key < overspeed_bound:
        return True
    if key.startswith("boss_reminder:") and key < boss_bound:
        return True
    return False


PRUNE_DEDUPE_SQL = """
DELETE FROM bot_settings
WHERE (key LIKE 'overspeed:%' AND key < ?)
   OR (key LIKE 'boss_reminder:%' AND key < ?)
"""
