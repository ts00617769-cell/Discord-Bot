"""整輪快照必須全成或全敗。"""
from __future__ import annotations

import sqlite3

import pytest

from db.schema import apply_migrations
from services.exp_snapshots import persist_snapshot_round


def _row(server: str, name: str) -> tuple:
    return (
        "2026-08-04 12:00:00",
        server,
        name,
        60,
        1_000.0,
        "戰士",
        10,
        "狼團",
    )


@pytest.mark.asyncio
async def test_persist_snapshot_round_commits_all_servers(tmp_path):
    import aiosqlite

    db = await aiosqlite.connect(tmp_path / "snapshot.db")
    try:
        await apply_migrations(db)
        await persist_snapshot_round(db, [[_row("S1", "A")], [_row("S2", "B")]])
        async with db.execute(
            "SELECT server_name, player_name FROM exp_history ORDER BY server_name"
        ) as cursor:
            assert await cursor.fetchall() == [("S1", "A"), ("S2", "B")]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_persist_snapshot_round_rolls_back_partial_failure(tmp_path):
    import aiosqlite

    db = await aiosqlite.connect(tmp_path / "rollback.db")
    try:
        await apply_migrations(db)
        await db.execute(
            """
            CREATE TRIGGER reject_s2 BEFORE INSERT ON exp_history
            WHEN NEW.server_name = 'S2'
            BEGIN
                SELECT RAISE(ABORT, 'reject S2');
            END
            """
        )
        await db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            await persist_snapshot_round(
                db,
                [[_row("S1", "A")], [_row("S2", "B")]],
            )
        async with db.execute("SELECT COUNT(*) FROM exp_history") as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await db.close()
