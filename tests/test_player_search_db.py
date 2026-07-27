"""PlayerSearchStore denorm / seamless smoke tests."""
from __future__ import annotations

import sqlite3

import aiosqlite
import pytest

from db.schema import apply_migrations, rebuild_player_profiles_sync
from services.player_search_db import PlayerSearchStore, normalize_profile_rows


@pytest.mark.asyncio
async def test_seamless_uses_denorm_stats(tmp_path):
    db_path = tmp_path / "search.db"
    db = await aiosqlite.connect(str(db_path))
    try:
        await apply_migrations(db)
        await db.executemany(
            """
            INSERT INTO exp_history
            (record_time, server_name, player_name, level, exp, class_name, subjugation_grade)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("2026-07-01 10:00:00", "S1", "Old", 50, 1e12, "戰士", 5),
                ("2026-07-01 12:00:00", "S1", "Old", 50, 1.01e12, "戰士", 5),
                ("2026-07-02 10:00:00", "S2", "New", 50, 1.02e12, "戰士", 5),
                ("2026-07-02 12:00:00", "S2", "New", 51, 1.03e12, "戰士", 5),
            ],
        )
        await db.commit()
    finally:
        await db.close()

    conn = sqlite3.connect(str(db_path))
    try:
        rebuild_player_profiles_sync(conn)
    finally:
        conn.close()

    db = await aiosqlite.connect(str(db_path))
    try:
        store = PlayerSearchStore(db)
        assert await store._has_denorm_stats() is True
        profile = await store._fetch_single_profile("Old", "S1")
        assert profile is not None
        assert profile[0] == "Old"
        cands = await store._find_seamless_candidates(profile, exp_margin=1e11, limit=5)
        names = {(c["name"], c["server"]) for c in cands}
        assert ("New", "S2") in names
    finally:
        await db.close()
