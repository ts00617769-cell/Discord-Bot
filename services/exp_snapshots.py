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


def profiles_from_insert_batch(insert_batch: Iterable[tuple]) -> list[tuple]:
    """由 insert batch 去重產生 player_profile upsert 參數（同名同服取最後一筆）。"""
    latest: dict[tuple[str, str], tuple] = {}
    for record_time, server_name, name, _level, _exp, class_name, _grade in insert_batch:
        latest[(name, server_name)] = (
            name,
            server_name,
            class_name or "未知",
            record_time,
        )
    return list(latest.values())


EXP_HISTORY_INSERT_SQL = """
    INSERT OR IGNORE INTO exp_history
    (record_time, server_name, player_name, level, exp, class_name, subjugation_grade)
    VALUES (?, ?, ?, ?, ?, ?, ?)
"""

PLAYER_PROFILE_UPSERT_SQL = """
    INSERT INTO player_profile (player_name, server_name, class_name, updated_at)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(player_name, server_name) DO UPDATE SET
        class_name=excluded.class_name,
        updated_at=excluded.updated_at
"""
