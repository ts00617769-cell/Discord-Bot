"""成員登錄查詢與雙向別名寫入（Discord 無關）。"""
from __future__ import annotations

from services.error_handler import safe_database_operation


def _split_identities(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [x.strip() for x in str(raw).split(",") if x.strip()]


def _join_identities(names: list[str]) -> str:
    # 保序去重
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


async def upsert_alias_links(db, current_name: str, aliases: list[str]) -> str:
    """雙向標記：current ↔ 各 alias；回傳 current 累計身分字串。"""
    current = (current_name or "").strip()
    alias_list = [a.strip() for a in aliases if a and a.strip() and a.strip() != current]
    if not current or not alias_list:
        existing = await _get_identity_list(db, current) if current else []
        return _join_identities(existing)

    # current → aliases
    existing = await _get_identity_list(db, current)
    added = [a for a in alias_list if a not in existing]
    for a in added:
        existing.append(a)
    await _set_identity_list(db, current, existing)

    # 各 alias → current（雙向）
    for alias in alias_list:
        rev = await _get_identity_list(db, alias)
        if current not in rev:
            rev.append(current)
            await _set_identity_list(db, alias, rev)

    await db.commit()
    return _join_identities(existing)


async def clear_member_identity(db, player_name: str) -> None:
    """清除玩家標記，並從其他列的別名串中移除該名。"""
    name = (player_name or "").strip()
    if not name:
        return
    aliases = await _get_identity_list(db, name)
    await db.execute(
        "DELETE FROM member_registry WHERE player_name = ?",
        (name,),
    )
    # 反向清理：其他列若含此名則刪除
    async with db.execute(
        "SELECT player_name, original_identity FROM member_registry"
    ) as cursor:
        rows = await cursor.fetchall()
    for other_name, raw in rows:
        identities = _split_identities(raw)
        if name not in identities:
            continue
        identities = [x for x in identities if x != name]
        await _set_identity_list(db, other_name, identities)
    # 別名列若只指向此人，上面已處理；若別名本身是獨立列且我們剛刪除的是主名，保留
    for alias in aliases:
        rev = await _get_identity_list(db, alias)
        if name in rev:
            rev = [x for x in rev if x != name]
            await _set_identity_list(db, alias, rev)
    await db.commit()
