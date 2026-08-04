"""組裝 EXP 快照寫入批次（Discord 無關）。"""
from __future__ import annotations

from typing import Iterable


def normalize_guild(raw) -> str:
    if raw is None:
        return ""
    text = str(raw).strip()
    if text in ("", "None", "null", "未知"):
        return ""
    return text


# 相容舊呼叫名稱
_normalize_guild = normalize_guild


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
                _normalize_guild(p.get("guild_name")),
            )
        )
    return insert_batch


def profiles_from_insert_batch(insert_batch: Iterable[tuple]) -> list[tuple]:
    """由 insert batch 去重產生 player_profile upsert 參數（同名同服取最後一筆）。

    回傳：(name, server, class, updated_at, min_exp, max_exp, first_seen,
           last_seen, max_level, max_sub_grade, guild_name)
    單次快照內同名同服合併 min/max；跨快照由 SQL ON CONFLICT 再合併。
    """
    latest: dict[tuple[str, str], tuple] = {}
    for (
        record_time,
        server_name,
        name,
        level,
        exp,
        class_name,
        grade,
        guild_name,
    ) in insert_batch:
        key = (name, server_name)
        prev = latest.get(key)
        if prev is None:
            latest[key] = (
                name,
                server_name,
                class_name or "未知",
                record_time,
                exp,
                exp,
                record_time,
                record_time,
                level,
                grade,
                guild_name or "",
            )
            continue
        (
            _n,
            _s,
            prev_cls,
            prev_updated,
            prev_min,
            prev_max,
            prev_first,
            prev_last,
            prev_lvl,
            prev_sub,
            prev_guild,
        ) = prev
        use_newer = record_time >= prev_updated

        def _min_num(a, b):
            if a is None:
                return b
            if b is None:
                return a
            return a if a <= b else b

        def _max_num(a, b):
            if a is None:
                return b
            if b is None:
                return a
            return a if a >= b else b

        latest[key] = (
            name,
            server_name,
            (class_name or "未知") if use_newer else prev_cls,
            record_time if use_newer else prev_updated,
            _min_num(prev_min, exp),
            _max_num(prev_max, exp),
            _min_num(prev_first, record_time),
            _max_num(prev_last, record_time),
            _max_num(prev_lvl, level),
            _max_num(prev_sub, grade),
            (guild_name or "") if use_newer else prev_guild,
        )
    return list(latest.values())


EXP_HISTORY_INSERT_SQL = """
    INSERT OR IGNORE INTO exp_history
    (record_time, server_name, player_name, level, exp, class_name, subjugation_grade, guild_name)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

# 同時間戳再寫入時更新可變欄位（不依賴 unique index；有索引時仍正確）
EXP_HISTORY_TOUCH_SQL = """
    UPDATE exp_history SET
      level = ?,
      exp = ?,
      class_name = ?,
      subjugation_grade = ?,
      guild_name = CASE
        WHEN ? != '' THEN ?
        ELSE guild_name
      END
    WHERE record_time = ? AND server_name = ? AND player_name = ?
"""


def touch_params_from_insert_batch(insert_batch: Iterable[tuple]) -> list[tuple]:
    """將 insert batch 轉成 EXP_HISTORY_TOUCH_SQL 參數。"""
    out: list[tuple] = []
    for (
        record_time,
        server_name,
        name,
        level,
        exp,
        class_name,
        grade,
        guild_name,
    ) in insert_batch:
        guild = guild_name or ""
        out.append(
            (
                level,
                exp,
                class_name,
                grade,
                guild,
                guild,
                record_time,
                server_name,
                name,
            )
        )
    return out


PLAYER_PROFILE_UPSERT_SQL = """
    INSERT INTO player_profile (
        player_name, server_name, class_name, updated_at,
        min_exp, max_exp, first_seen, last_seen, max_level, max_sub_grade,
        guild_name
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(player_name, server_name) DO UPDATE SET
        class_name = CASE
            WHEN excluded.updated_at IS NOT NULL
             AND (player_profile.updated_at IS NULL
                  OR excluded.updated_at >= player_profile.updated_at)
            THEN excluded.class_name
            ELSE player_profile.class_name
        END,
        updated_at = CASE
            WHEN excluded.updated_at IS NOT NULL
             AND (player_profile.updated_at IS NULL
                  OR excluded.updated_at >= player_profile.updated_at)
            THEN excluded.updated_at
            ELSE player_profile.updated_at
        END,
        min_exp = CASE
            WHEN player_profile.min_exp IS NULL THEN excluded.min_exp
            WHEN excluded.min_exp IS NULL THEN player_profile.min_exp
            ELSE MIN(player_profile.min_exp, excluded.min_exp)
        END,
        max_exp = CASE
            WHEN player_profile.max_exp IS NULL THEN excluded.max_exp
            WHEN excluded.max_exp IS NULL THEN player_profile.max_exp
            ELSE MAX(player_profile.max_exp, excluded.max_exp)
        END,
        first_seen = CASE
            WHEN player_profile.first_seen IS NULL THEN excluded.first_seen
            WHEN excluded.first_seen IS NULL THEN player_profile.first_seen
            ELSE MIN(player_profile.first_seen, excluded.first_seen)
        END,
        last_seen = CASE
            WHEN player_profile.last_seen IS NULL THEN excluded.last_seen
            WHEN excluded.last_seen IS NULL THEN player_profile.last_seen
            ELSE MAX(player_profile.last_seen, excluded.last_seen)
        END,
        max_level = CASE
            WHEN player_profile.max_level IS NULL THEN excluded.max_level
            WHEN excluded.max_level IS NULL THEN player_profile.max_level
            ELSE MAX(player_profile.max_level, excluded.max_level)
        END,
        max_sub_grade = CASE
            WHEN player_profile.max_sub_grade IS NULL THEN excluded.max_sub_grade
            WHEN excluded.max_sub_grade IS NULL THEN player_profile.max_sub_grade
            ELSE MAX(player_profile.max_sub_grade, excluded.max_sub_grade)
        END,
        guild_name = CASE
            WHEN excluded.updated_at IS NOT NULL
             AND (player_profile.updated_at IS NULL
                  OR excluded.updated_at >= player_profile.updated_at)
            THEN excluded.guild_name
            ELSE player_profile.guild_name
        END
"""
