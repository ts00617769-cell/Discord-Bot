"""profile rebuild / prune / denorm coverage."""
from __future__ import annotations

import sqlite3

import aiosqlite
import pytest

from db.schema import (
    DENORM_COVERAGE_READY_RATIO,
    apply_migrations,
    denorm_is_ready,
    prune_orphaned_player_profiles,
    rebuild_player_profiles,
    rebuild_player_profiles_sync,
)


@pytest.mark.asyncio
async def test_rebuild_player_profiles_ok(tmp_path):
    db_path = tmp_path / "rb.db"
    db = await aiosqlite.connect(str(db_path))
    try:
        await apply_migrations(db)
        await db.execute(
            """
            INSERT INTO exp_history
            (record_time, server_name, player_name, level, exp, class_name, subjugation_grade)
            VALUES ('2026-07-01 10:00:00', 'S1', 'A', 10, 100.0, '戰士', 1)
            """
        )
        await db.commit()
        n = await rebuild_player_profiles(db)
        assert n == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_prune_orphaned_player_profiles(tmp_path):
    db_path = tmp_path / "prune.db"
    db = await aiosqlite.connect(str(db_path))
    try:
        await apply_migrations(db)
        await db.execute(
            """
            INSERT INTO exp_history
            (record_time, server_name, player_name, level, exp, class_name, subjugation_grade)
            VALUES ('2026-07-01 10:00:00', 'S1', 'Keep', 10, 100.0, '戰士', 1)
            """
        )
        await db.commit()
        await rebuild_player_profiles(db)
        await db.execute(
            """
            INSERT INTO player_profile
            (player_name, server_name, class_name, updated_at, min_exp, max_exp, first_seen, last_seen)
            VALUES ('Gone', 'S1', '戰士', '2026-07-01', 1, 2, '2026-07-01', '2026-07-01')
            """
        )
        await db.commit()
        deleted = await prune_orphaned_player_profiles(db)
        assert deleted >= 1
        async with db.execute(
            "SELECT player_name FROM player_profile ORDER BY player_name"
        ) as cur:
            names = [r[0] for r in await cur.fetchall()]
        assert names == ["Keep"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_denorm_is_ready_requires_coverage(tmp_path):
    db_path = tmp_path / "cov.db"
    db = await aiosqlite.connect(str(db_path))
    try:
        await apply_migrations(db)
        # 10 列 profile，僅 1 列有 denorm → 不 ready
        for i in range(10):
            await db.execute(
                """
                INSERT INTO player_profile
                (player_name, server_name, class_name, updated_at, min_exp, first_seen)
                VALUES (?, 'S1', '戰士', '2026-07-01', ?, ?)
                """,
                (f"P{i}", 1.0 if i == 0 else None, "2026-07-01" if i == 0 else None),
            )
        await db.commit()
        assert await denorm_is_ready(db, min_ratio=DENORM_COVERAGE_READY_RATIO) is False
        # 全填滿 → ready
        await db.execute(
            "UPDATE player_profile SET min_exp=1.0, first_seen='2026-07-01'"
        )
        await db.commit()
        assert await denorm_is_ready(db, min_ratio=DENORM_COVERAGE_READY_RATIO) is True
    finally:
        await db.close()


def test_rebuild_sync_rollback(tmp_path):
    db_path = tmp_path / "sync_rb.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE exp_history (
                record_time TIMESTAMP, server_name TEXT, player_name TEXT,
                level INTEGER, exp REAL, class_name TEXT, subjugation_grade INTEGER
            )
            """
        )
        conn.execute(
            "INSERT INTO exp_history VALUES ('2026-07-01','S1','A',1,1.0,'戰士',0)"
        )
        conn.commit()
        n = rebuild_player_profiles_sync(conn)
        assert n == 1
    finally:
        conn.close()
