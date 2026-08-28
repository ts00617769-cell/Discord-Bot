"""轉服警報流程：候選 pair 查重、舊角缺席過濾（Discord 無關）。"""
from __future__ import annotations

from typing import Any, Sequence

from services.transfer_detect import (
    IDX_NEW_NAME,
    IDX_NEW_SERVER,
    IDX_OLD_NAME,
    IDX_OLD_SERVER,
)


def pair_key_from_row(row: tuple) -> tuple[Any, Any, Any, Any]:
    """ranked SQL row → (old_name, old_server, new_name, new_server)。"""
    return (
        row[IDX_OLD_NAME],
        row[IDX_OLD_SERVER],
        row[IDX_NEW_NAME],
        row[IDX_NEW_SERVER],
    )


async def lookup_alerted_pairs(
    db,
    pair_keys: Sequence[tuple[str, str, str, str]],
    *,
    chunk_size: int = 80,
) -> set[tuple[str, str, str, str]]:
    """只查候選 pair，避免整表載入 transfer_alerts_log。"""
    if not pair_keys:
        return set()
    found: set[tuple[str, str, str, str]] = set()
    for i in range(0, len(pair_keys), chunk_size):
        batch = list(pair_keys[i : i + chunk_size])
        placeholders = ",".join("(?,?,?,?)" for _ in batch)
        flat: list[str] = []
        for p in batch:
            flat.extend(p)
        async with db.execute(
            f"""
            SELECT old_name, old_server, new_name, new_server
            FROM transfer_alerts_log
            WHERE (old_name, old_server, new_name, new_server) IN ({placeholders})
            """,
            tuple(flat),
        ) as cursor:
            for row in await cursor.fetchall():
                found.add(tuple(row))  # type: ignore[arg-type]
    return found


async def players_present_at_times(
    db,
    miss_times: Sequence[str],
    players: Sequence[tuple[str, str]],
) -> set[tuple[str, str, str]]:
    """批次查詢：回傳 {(record_time, player_name, server_name), ...} 有上榜者。"""
    if not miss_times or not players:
        return set()
    # 去重玩家
    unique_players = list(dict.fromkeys(players))
    present: set[tuple[str, str, str]] = set()
    # 每個 miss_time 一次 IN 查詢（玩家數通常遠小於 SQLite 變數上限）
    chunk = 200
    for miss_t in miss_times:
        for i in range(0, len(unique_players), chunk):
            batch = unique_players[i : i + chunk]
            placeholders = ",".join("(?,?)" for _ in batch)
            flat: list[str] = []
            for name, server in batch:
                flat.extend((name, server))
            async with db.execute(
                f"""
                SELECT player_name, server_name
                FROM exp_history
                WHERE record_time = ?
                  AND (player_name, server_name) IN ({placeholders})
                """,
                (miss_t, *flat),
            ) as cursor:
                for name, server in await cursor.fetchall():
                    present.add((miss_t, name, server))
    return present


async def filter_viable_ranked(
    db,
    ranked: Sequence[tuple],
    already_alerted: set[tuple],
    miss_times: Sequence[str],
) -> list[tuple]:
    """略過已報過；舊角須在 miss_times 全部缺席（呼叫端應只傳本輪時間）。"""
    candidates: list[tuple] = []
    old_keys: list[tuple[str, str]] = []
    for row in ranked:
        key = pair_key_from_row(row)
        if key in already_alerted:
            continue
        candidates.append(row)
        old_keys.append((row[IDX_OLD_NAME], row[IDX_OLD_SERVER]))

    present = await players_present_at_times(db, miss_times, old_keys)

    viable: list[tuple] = []
    for row in candidates:
        old_name, old_server = row[IDX_OLD_NAME], row[IDX_OLD_SERVER]
        if any((t, old_name, old_server) in present for t in miss_times):
            continue
        viable.append(row)
    return viable
