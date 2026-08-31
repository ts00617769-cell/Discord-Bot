"""轉服消失佇列：窗內連續缺席 → 延遲上榜配對。"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from services.game_event_windows import (
    disappear_in_transfer_window,
    match_realm_transfer,
)
from services.transfer_detect import (
    CLASS_MARGIN,
    NAME_MARGIN,
    missing_to_transfer_row,
)

# 連續缺席幾輪完整快照才進佇列
MIN_MISS_ROUNDS = 2

PRUNE_TRANSFER_MISSING_SQL = """
DELETE FROM transfer_missing
WHERE (resolved_at IS NOT NULL AND resolved_at < ?)
   OR (resolved_at IS NULL AND last_seen < ?)
"""


UPSERT_MISSING_SQL = """
    INSERT INTO transfer_missing (
        player_name, server_name, last_seen, last_exp, level, class_name,
        subjugation_grade, guild_name, miss_count, window_label, created_at,
        resolved_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL)
    ON CONFLICT(player_name, server_name) DO UPDATE SET
        last_seen = CASE
            WHEN transfer_missing.resolved_at IS NOT NULL
            THEN excluded.last_seen
            ELSE transfer_missing.last_seen
        END,
        last_exp = CASE
            WHEN transfer_missing.resolved_at IS NOT NULL
            THEN excluded.last_exp
            ELSE transfer_missing.last_exp
        END,
        level = CASE
            WHEN transfer_missing.resolved_at IS NOT NULL
            THEN excluded.level
            ELSE transfer_missing.level
        END,
        class_name = CASE
            WHEN transfer_missing.resolved_at IS NOT NULL
            THEN excluded.class_name
            ELSE transfer_missing.class_name
        END,
        subjugation_grade = CASE
            WHEN transfer_missing.resolved_at IS NOT NULL
            THEN excluded.subjugation_grade
            ELSE transfer_missing.subjugation_grade
        END,
        guild_name = CASE
            WHEN transfer_missing.resolved_at IS NOT NULL
            THEN excluded.guild_name
            ELSE transfer_missing.guild_name
        END,
        miss_count = CASE
            WHEN transfer_missing.resolved_at IS NOT NULL THEN 0
            ELSE transfer_missing.miss_count
        END,
        window_label = COALESCE(excluded.window_label, transfer_missing.window_label),
        created_at = CASE
            WHEN transfer_missing.resolved_at IS NOT NULL
            THEN excluded.created_at
            ELSE transfer_missing.created_at
        END,
        resolved_at = NULL
"""


async def upsert_disappeared(
    db,
    *,
    time_now: str,
    time_prev: str,
    created_at: str,
    commit: bool = True,
) -> int:
    """把上一完整快照有、本輪缺席、且 last_seen 落在轉移窗的玩家寫入佇列。

    新列 miss_count=0；同輪接著 bump_still_missing 會 +1。
    回傳本輪新寫入／重開列數。
    """

    # 取得各服第 50 名經驗值門檻 cutoff_exp
    cutoff_sql = """
        SELECT server_name, MIN(exp) as cutoff_exp
        FROM (
            SELECT server_name, exp,
                   ROW_NUMBER() OVER (PARTITION BY server_name ORDER BY exp DESC) as rn
            FROM exp_history
            WHERE record_time = ?
        )
        WHERE rn <= 50
        GROUP BY server_name
    """
    async with db.execute(cutoff_sql, (time_prev,)) as cursor:
        cutoff_rows = await cursor.fetchall()
        cutoffs = {row[0]: float(row[1]) for row in cutoff_rows}

    sql = """
        SELECT e.player_name, e.server_name, e.record_time, e.exp, e.level,
               e.class_name, e.subjugation_grade, COALESCE(e.guild_name, '')
        FROM exp_history e
        WHERE e.record_time = ?
          AND e.exp > 1000000000000
          AND NOT EXISTS (
              SELECT 1 FROM exp_history n
              WHERE n.record_time = ?
                AND n.player_name = e.player_name
                AND n.server_name = e.server_name
          )
    """
    async with db.execute(sql, (time_prev, time_now)) as cursor:
        rows = await cursor.fetchall()

    written = 0
    for name, server, last_seen, exp, level, cls, sub, guild in rows:
        label = disappear_in_transfer_window(str(last_seen))
        if not label:
            continue

        cutoff_exp = cutoffs.get(server)
        if cutoff_exp and abs(exp - cutoff_exp) / cutoff_exp < 0.005:
            label = "疑似掉榜 (Rank 50+ Drop)"

        await db.execute(
            UPSERT_MISSING_SQL,
            (
                name,
                server,
                last_seen,
                exp,
                level,
                cls or "未知",
                sub if sub is not None else 0,
                guild or "",
                label,
                created_at,
            ),
        )
        written += 1
    if written and commit:
        await db.commit()
    return written


async def bump_still_missing(db, *, time_now: str, commit: bool = True) -> int:
    """未解決且本輪仍缺席 → miss_count + 1（含剛 upsert 的新列）。"""
    cursor = await db.execute(
        """
        UPDATE transfer_missing
        SET miss_count = miss_count + 1
        WHERE resolved_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM exp_history e
              WHERE e.record_time = ?
                AND e.player_name = transfer_missing.player_name
                AND e.server_name = transfer_missing.server_name
          )
        """,
        (time_now,),
    )
    n = cursor.rowcount or 0
    if n and commit:
        await db.commit()
    return int(n)


async def resolve_reappeared(db, *, time_now: str, commit: bool = True) -> int:
    """若舊服身分又出現在榜上，標記 resolved。"""
    sql = """
        UPDATE transfer_missing
        SET resolved_at = ?
        WHERE resolved_at IS NULL
          AND EXISTS (
              SELECT 1 FROM exp_history e
              WHERE e.record_time = ?
                AND e.player_name = transfer_missing.player_name
                AND e.server_name = transfer_missing.server_name
          )
    """
    cursor = await db.execute(sql, (time_now, time_now))
    n = cursor.rowcount or 0
    if n and commit:
        await db.commit()
    return int(n)


async def fetch_open_missing(
    db,
    *,
    min_miss_count: int = MIN_MISS_ROUNDS,
) -> list[dict[str, Any]]:
    """尚未解決且缺席達標的佇列列。"""
    async with db.execute(
        """
        SELECT player_name, server_name, last_seen, last_exp, level, class_name,
               subjugation_grade, guild_name, miss_count, window_label
        FROM transfer_missing
        WHERE resolved_at IS NULL
          AND miss_count >= ?
          AND window_label != '疑似掉榜 (Rank 50+ Drop)'
        ORDER BY last_seen DESC
        """,
        (min_miss_count,),
    ) as cursor:
        rows = await cursor.fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "player_name": r[0],
                "server_name": r[1],
                "last_seen": r[2],
                "last_exp": r[3],
                "level": r[4],
                "class_name": r[5] or "未知",
                "subjugation_grade": r[6] if r[6] is not None else 0,
                "guild_name": r[7] or "",
                "miss_count": r[8],
                "window_label": r[9],
            }
        )
    return out


async def fetch_newcomers(
    db,
    *,
    time_now: str,
    time_prev: str,
) -> list[dict[str, Any]]:
    """本輪首次出現在該服的玩家（相對 time_prev）。"""
    sql = """
        SELECT player_name, server_name, level, exp, class_name,
               subjugation_grade, COALESCE(guild_name, '')
        FROM exp_history
        WHERE record_time = ?
          AND exp > 1000000000000
          AND NOT EXISTS (
              SELECT 1 FROM exp_history p
              WHERE p.record_time = ?
                AND p.player_name = exp_history.player_name
                AND p.server_name = exp_history.server_name
          )
    """
    async with db.execute(sql, (time_now, time_prev)) as cursor:
        rows = await cursor.fetchall()
    return [
        {
            "player_name": r[0],
            "server_name": r[1],
            "level": r[2],
            "exp": r[3],
            "class_name": r[4] or "未知",
            "subjugation_grade": r[5] if r[5] is not None else 0,
            "guild_name": r[6] or "",
        }
        for r in rows
    ]


def match_newcomer_to_missing(
    newcomer: dict[str, Any],
    missing: dict[str, Any],
    *,
    appear_time: str,
) -> Optional[tuple]:
    """特徵可配且符合轉移窗銜接 → 回傳 transfer row；否則 None。"""
    if newcomer["server_name"] == missing["server_name"]:
        return None
    if not match_realm_transfer(str(missing["last_seen"]), appear_time):
        return None

    same_name = newcomer["player_name"] == missing["player_name"]
    new_exp = float(newcomer["exp"])
    old_exp = float(missing["last_exp"])
    if new_exp < old_exp:
        return None
    exp_diff = new_exp - old_exp

    new_lvl = newcomer["level"] if newcomer["level"] is not None else 0
    old_lvl = missing["level"] if missing["level"] is not None else 0
    if new_lvl < old_lvl:
        return None
    new_sub = newcomer["subjugation_grade"]
    old_sub = missing["subjugation_grade"]
    if new_sub < old_sub:
        return None

    if same_name:
        if exp_diff > NAME_MARGIN:
            return None
    else:
        if (newcomer["class_name"] or "未知") != (missing["class_name"] or "未知"):
            return None
        if newcomer["class_name"] in ("", "None", "未知"):
            return None
        if new_sub != old_sub:
            return None
        if abs(new_lvl - old_lvl) > 1:
            return None
        if exp_diff > CLASS_MARGIN:
            return None

    return missing_to_transfer_row(
        new_exp=new_exp,
        new_name=newcomer["player_name"],
        new_server=newcomer["server_name"],
        new_lvl=new_lvl,
        new_cls=newcomer["class_name"],
        new_guild=newcomer["guild_name"],
        new_sub=new_sub,
        old_name=missing["player_name"],
        old_server=missing["server_name"],
        old_lvl=old_lvl,
        old_cls=missing["class_name"],
        old_exp=old_exp,
        old_guild=missing["guild_name"],
        old_sub=old_sub,
        old_last_seen=str(missing["last_seen"]),
    )


def build_missing_queue_rows(
    newcomers: Sequence[dict[str, Any]],
    open_missing: Sequence[dict[str, Any]],
    *,
    appear_time: str,
) -> list[tuple]:
    """新進玩家 × 消失佇列 → 候選 transfer rows。"""
    rows: list[tuple] = []
    for neu in newcomers:
        for miss in open_missing:
            pair = match_newcomer_to_missing(neu, miss, appear_time=appear_time)
            if pair is not None:
                rows.append(pair)
    return rows


async def mark_missing_resolved(
    db,
    old_name: str,
    old_server: str,
    *,
    resolved_at: str,
) -> None:
    await db.execute(
        """
        UPDATE transfer_missing
        SET resolved_at = ?
        WHERE player_name = ? AND server_name = ? AND resolved_at IS NULL
        """,
        (resolved_at, old_name, old_server),
    )


async def prune_stale_missing(
    db,
    *,
    before: str,
) -> int:
    """刪除已解決或過舊的佇列列。"""
    cursor = await db.execute(PRUNE_TRANSFER_MISSING_SQL, (before, before))
    n = cursor.rowcount or 0
    if n:
        await db.commit()
    return int(n)


def prune_stale_missing_sync(conn, *, before: str) -> int:
    """同步版：供 cleanup_db／retention 離線路徑。"""
    return int(conn.execute(PRUNE_TRANSFER_MISSING_SQL, (before, before)).rowcount or 0)