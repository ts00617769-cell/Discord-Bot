"""轉服偵測：SQL 片段與純邏輯過濾／排序。"""
from __future__ import annotations

from typing import Any, Iterable, Optional

# 同名 margin 1000 億；異名（僅同職）100 億
NAME_MARGIN = 1000 * 100_000_000
CLASS_MARGIN = 100 * 100_000_000

POTENTIAL_TRANSFERS_SQL = """
    SELECT DISTINCT t_now.exp, t_now.player_name, t_now.server_name, t_now.level, t_now.class_name,
                    t_old.player_name, t_old.server_name, t_old.level, t_old.class_name,
                    t_old.exp, t_now.subjugation_grade, t_old.subjugation_grade
    FROM exp_history t_now
    JOIN (
        SELECT e.player_name, e.server_name, e.class_name, e.level, e.subjugation_grade, e.exp, e.record_time
        FROM exp_history e
        INNER JOIN (
            SELECT player_name, server_name, MAX(record_time) AS max_time
            FROM exp_history
            WHERE record_time <= ? AND record_time >= datetime(?, '-7 days')
            GROUP BY player_name, server_name
        ) latest
          ON e.player_name = latest.player_name
         AND e.server_name = latest.server_name
         AND e.record_time = latest.max_time
    ) t_old ON (
        (t_now.player_name = t_old.player_name
         AND t_now.exp >= t_old.exp AND t_now.exp <= (t_old.exp + ?))
        OR (
            t_now.player_name != t_old.player_name
            AND t_now.class_name = t_old.class_name
            AND t_now.class_name IS NOT NULL
            AND t_old.class_name IS NOT NULL
            AND t_now.class_name NOT IN ('', 'None', '未知')
            AND t_old.class_name NOT IN ('', 'None', '未知')
            AND COALESCE(t_now.subjugation_grade, -1) = COALESCE(t_old.subjugation_grade, -2)
            AND ABS(t_now.level - t_old.level) <= 1
            AND t_now.exp >= t_old.exp AND t_now.exp <= (t_old.exp + ?)
        )
    )
    WHERE t_now.record_time = ? AND t_now.exp > 1000000000000
      AND t_now.level >= t_old.level
      AND COALESCE(t_now.subjugation_grade, 0) >= COALESCE(t_old.subjugation_grade, 0)
      AND t_now.server_name != t_old.server_name
      AND NOT EXISTS (
          SELECT 1 FROM exp_history t_check
          WHERE t_check.record_time = ?
            AND t_check.player_name = t_now.player_name
            AND t_check.server_name = t_now.server_name
      )
"""


def build_alias_map(registry_rows: Iterable[tuple]) -> dict[str, set[str]]:
    """(player_name, original_identity) → {name: set(aliases)}。"""
    alias_map: dict[str, set[str]] = {}
    for row in registry_rows:
        name = row[0]
        identities = [i.strip() for i in row[1].split(",")] if row[1] else []
        alias_map[name] = set(identities)
    return alias_map


def is_known_alias(
    alias_map: dict[str, set[str]], new_name: str, old_name: str
) -> bool:
    return (
        old_name in alias_map.get(new_name, set())
        or new_name in alias_map.get(old_name, set())
    )


def transfer_sort_key(row: tuple, alias_map: dict[str, set[str]]) -> tuple:
    """較佳候選排前面：同名 > 登錄別名 > 同級 > 同討伐 > 經驗差小。"""
    return (
        0 if row[1] == row[5] else 1,
        0 if is_known_alias(alias_map, row[1], row[5]) else 1,
        0 if row[3] == row[7] else 1,
        0 if (row[10] is not None and row[11] is not None and row[10] == row[11]) else 1,
        row[0] - row[9],
    )


def should_skip_rename_mismatch(row: tuple, alias_map: dict[str, set[str]]) -> bool:
    """異名且非別名時，討伐必須一致；否則略過。"""
    new_name, old_name = row[1], row[5]
    if new_name == old_name or is_known_alias(alias_map, new_name, old_name):
        return False
    if row[10] is None or row[11] is None or row[10] != row[11]:
        return True
    return False


def transfer_status(new_name: str, old_name: str) -> str:
    return "跨服轉移並改名" if new_name != old_name else "跨服轉移"


def format_exp_diff(exp_diff: float) -> str:
    if exp_diff == 0:
        return "+0.00% (完美吻合)"
    return f"+{(exp_diff / 100_000_000):,.0f} 億 (轉移期間偷練)"


def rank_transfer_candidates(
    transfer_records: list[tuple],
    alias_map: dict[str, set[str]],
) -> list[tuple]:
    """過濾異名討伐不符後，依優先序排序。"""
    filtered = [
        row
        for row in transfer_records
        if not should_skip_rename_mismatch(row, alias_map)
    ]
    filtered.sort(key=lambda x: transfer_sort_key(x, alias_map))
    return filtered


def pick_unique_pairs(
    ranked_rows: Iterable[tuple],
    already_alerted: set[tuple],
) -> list[dict[str, Any]]:
    """一對一配對：舊角／新角各只能配一次；略過已報過的 pair。"""
    matched_old: set[tuple] = set()
    matched_new: set[tuple] = set()
    results: list[dict[str, Any]] = []

    for row in ranked_rows:
        new_exp = row[0]
        new_name, new_server, new_lvl, new_cls = row[1], row[2], row[3], row[4]
        old_name, old_server, old_lvl, old_cls = row[5], row[6], row[7], row[8]
        old_exp = row[9]
        new_sub_grade = row[10]

        old_key = (old_name, old_server)
        new_key = (new_name, new_server)
        if old_key in matched_old or new_key in matched_new:
            continue

        pair_key = (old_name, old_server, new_name, new_server)
        if pair_key in already_alerted:
            continue

        matched_old.add(old_key)
        matched_new.add(new_key)
        results.append(
            {
                "pair_key": pair_key,
                "old_key": old_key,
                "new_key": new_key,
                "new_name": new_name,
                "new_server": new_server,
                "old_name": old_name,
                "old_server": old_server,
                "new_lvl": new_lvl,
                "new_cls": new_cls,
                "old_lvl": old_lvl,
                "old_cls": old_cls,
                "new_sub_grade": new_sub_grade,
                "exp_diff": new_exp - old_exp,
                "status": transfer_status(new_name, old_name),
            }
        )
    return results
