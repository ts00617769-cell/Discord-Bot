"""bot_settings / alert_dedupe 去重 key 清理輔助。"""
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

# alert_dedupe.created_at 為 SQL 時間字串，直接與 cutoff 比較。
# transfer kind 另用 transfer_alerts_log 的保留期，避免 per-channel claim
# 先被清掉、pair log 卻還在（或反之）造成同一組轉服重複警報。
PRUNE_ALERT_DEDUPE_SQL = """
DELETE FROM alert_dedupe
WHERE created_at < ? AND kind <> 'transfer'
"""

PRUNE_TRANSFER_DEDUPE_SQL = """
DELETE FROM alert_dedupe
WHERE created_at < ? AND kind = 'transfer'
"""
