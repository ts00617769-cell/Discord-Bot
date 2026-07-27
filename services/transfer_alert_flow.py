"""轉服警報流程：候選 pair 查重、舊角缺席過濾（Discord 無關）。"""
from __future__ import annotations

from typing import Any, Sequence


def pair_key_from_row(row: tuple) -> tuple[Any, Any, Any, Any]:
    """ranked SQL row → (old_name, old_server, new_name, new_server)。"""
    return (row[5], row[6], row[1], row[2])


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


async def player_present_at(
    db, record_time: str, player_name: str, server_name: str
) -> bool:
    async with db.execute(
        """
        SELECT 1 FROM exp_history
        WHERE record_time = ? AND player_name = ? AND server_name = ?
        """,
        (record_time, player_name, server_name),
    ) as cursor:
        return await cursor.fetchone() is not None


async def filter_viable_ranked(
    db,
    ranked: Sequence[tuple],
    already_alerted: set[tuple],
    miss_times: Sequence[str],
) -> list[tuple]:
    """略過已報過；舊角須在 miss_times 全部缺席。"""
    viable: list[tuple] = []
    for row in ranked:
        key = pair_key_from_row(row)
        if key in already_alerted:
            continue
        old_name, old_server = row[5], row[6]
        old_missing_all = True
        for miss_t in miss_times:
            if await player_present_at(db, miss_t, old_name, old_server):
                old_missing_all = False
                break
        if old_missing_all:
            viable.append(row)
    return viable
