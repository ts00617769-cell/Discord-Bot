"""組裝 EXP 快照寫入批次（Discord 無關）。"""
from __future__ import annotations

from typing import Iterable


def players_to_insert_batch(record_time, server_name: str, players: Iterable[dict]) -> list[tuple]:
    """將 Ranking API 玩家列轉成 exp_history INSERT 參數列。"""
    insert_batch: list[tuple] = []
    for p in players:
        name = p.get("gc_name")
        if not name:
            continue
        grade_val = (p.get("string_map") or {}).get("grade", "0")
        try:
            grade = int(grade_val)
        except (ValueError, TypeError):
            grade = 0
        insert_batch.append(
            (
                record_time,
                server_name,
                name,
                p.get("gc_level"),
                p.get("gc_exp", 0),
                p.get("class_name", "未知"),
                grade,
            )
        )
    return insert_batch


EXP_HISTORY_INSERT_SQL = """
    INSERT OR IGNORE INTO exp_history
    (record_time, server_name, player_name, level, exp, class_name, subjugation_grade)
    VALUES (?, ?, ?, ?, ?, ?, ?)
"""
