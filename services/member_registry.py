"""成員登錄查詢與雙向別名寫入（Discord 無關）。"""
from __future__ import annotations

from services.error_handler import safe_database_operation


def _split_identities(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [x.strip() for x in str(raw).split(",") if x.strip()]


def _join_identities(names: list[str]) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return ", ".join(out)


async def get_member_tag(db, name: str) -> str:
    """回傳 `({original_identity})` 或空字串。"""

    async def _query():
        async with db.execute(
            "SELECT original_identity FROM member_registry WHERE player_name = ?",
            (name,),
        ) as cursor:
            result = await cursor.fetchone()
            return f"({result[0]})" if result else ""

    tag = await safe_database_operation(f"member_tag:{name}", _query)
    return tag or ""


async def _get_identity_list(db, player_name: str) -> list[str]:
    async with db.execute(
        "SELECT original_identity FROM member_registry WHERE player_name = ?",
        (player_name,),
    ) as cursor:
        row = await cursor.fetchone()
    return _split_identities(row[0] if row else None)


async def _set_identity_list(db, player_name: str, identities: list[str]) -> None:
    identity_str = _join_identities(identities)
    if not identity_str:
        await db.execute(
            "DELETE FROM member_registry WHERE player_name = ?",
            (player_name,),
        )
        return
    await db.execute(
        """
        INSERT INTO member_registry (player_name, original_identity)
        VALUES (?, ?)
        ON CONFLICT(player_name) DO UPDATE SET original_identity=excluded.original_identity
        """,
        (player_name, identity_str),
    )


async def _collect_alias_group(db, seed_names: set[str]) -> set[str]:
    """BFS 收集同一別名群組內所有玩家名。"""
    group = set(seed_names)
    frontier = list(group)
    while frontier:
        name = frontier.pop()
        for linked in await _get_identity_list(db, name):
            if linked not in group:
                group.add(linked)
                frontier.append(linked)
    return group


async def _sync_alias_group(db, group: set[str]) -> None:
    """群組內每個名字都指向其餘所有名字（完整閉包）。"""
    for member in group:
        others = sorted(x for x in group if x != member)
        await _set_identity_list(db, member, others)


async def upsert_alias_links(db, current_name: str, aliases: list[str]) -> str:
    """雙向標記：整個別名群組內互指；回傳 current 累計身分字串。"""
    current = (current_name or "").strip()
    alias_list = [
        a.strip() for a in aliases if a and a.strip() and a.strip() != current
    ]
    if not current:
        return ""

    seeds = {current, *alias_list}
    group = await _collect_alias_group(db, seeds)
    group.update(alias_list)
    await _sync_alias_group(db, group)
    await db.commit()
    return _join_identities(sorted(x for x in group if x != current))


async def clear_member_identity(db, player_name: str) -> None:
    """清除玩家標記；群組其餘成員仍彼此互指。"""
    name = (player_name or "").strip()
    if not name:
        return
    group = await _collect_alias_group(db, {name})
    await db.execute(
        "DELETE FROM member_registry WHERE player_name = ?",
        (name,),
    )
    remaining = group - {name}
    if remaining:
        await _sync_alias_group(db, remaining)
    await db.commit()
