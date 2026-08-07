"""組裝 EXP 快照寫入批次（Discord 無關）。"""
from __future__ import annotations

from typing import Any, Iterable


async def fetch_recent_complete_snapshot_times(
    db: Any, min_servers: int, *, limit: int = 10
) -> list[tuple]:
    """回傳完整快照時間列 [(record_time,), ...]，新→舊。"""
    async with db.execute(
        """
        SELECT record_time
        FROM exp_history
        GROUP BY record_time
        HAVING COUNT(DISTINCT server_name) >= ?
        ORDER BY record_time DESC LIMIT ?
        """,
        (min_servers, limit),
    ) as cursor:
        return [tuple(r) for r in await cursor.fetchall()]


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


async def persist_snapshot_round(
    db,
    server_batches: Iterable[Iterable[tuple]],
    *,
    persist_history: bool = True,
    read_db=None,
) -> None:
    """整輪伺服器快照單一交易寫入；任一失敗即 rollback，不留下半套時間點。

    read_db: 可選唯讀連線，用於在 BEGIN IMMEDIATE 之前查既有 key，縮短持鎖時間。
    """
    batches = [list(batch) for batch in server_batches if batch]
    if not batches:
        return
    insert_batch = [row for batch in batches for row in batch]
    profile_batch = profiles_from_insert_batch(insert_batch)
    existing_keys: set[tuple[str, str, str]] = set()
    if persist_history:
        # 正常新快照只 INSERT；不要再把剛寫入的數千列逐筆 UPDATE。
        # 正式大庫可能略過 unique index，舊作法會讓每輪交易長時間持鎖。
        # 既有 key 查詢移出交易，縮短 BEGIN IMMEDIATE 持鎖時間（呼叫端持有寫入鎖）。
        source = read_db if read_db is not None else db
        record_times = {str(row[0]) for row in insert_batch}
        for record_time in record_times:
            async with source.execute(
                """
                SELECT record_time, server_name, player_name
                FROM exp_history
                WHERE record_time = ?
                """,
                (record_time,),
            ) as cursor:
                existing_keys.update(
                    (str(row[0]), str(row[1]), str(row[2]))
                    for row in await cursor.fetchall()
                )

    try:
        await db.execute("BEGIN IMMEDIATE")
        if persist_history:
            new_rows = [
                row
                for row in insert_batch
                if (str(row[0]), str(row[1]), str(row[2])) not in existing_keys
            ]
            existing_rows = [
                row
                for row in insert_batch
                if (str(row[0]), str(row[1]), str(row[2])) in existing_keys
            ]
            if new_rows:
                await db.executemany(EXP_HISTORY_INSERT_SQL, new_rows)
            if existing_rows:
                await db.executemany(
                    EXP_HISTORY_TOUCH_SQL,
                    touch_params_from_insert_batch(existing_rows),
                )
        if profile_batch:
            await db.executemany(PLAYER_PROFILE_UPSERT_SQL, profile_batch)
        await db.commit()
    except Exception:
        await db.rollback()
        raise


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
