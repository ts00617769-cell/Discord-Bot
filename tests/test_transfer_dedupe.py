"""轉服 dedupe 針對性查詢。"""
from __future__ import annotations

import pytest

from db.schema import apply_migrations


@pytest.mark.asyncio
async def test_transfer_pair_lookup_targeted(tmp_path):
    import aiosqlite

    db_path = tmp_path / "xfer.db"
    db = await aiosqlite.connect(str(db_path))
    try:
        await apply_migrations(db)
        await db.execute(
            """
            INSERT INTO transfer_alerts_log
            (old_name, old_server, new_name, new_server, alert_time)
            VALUES
            ('A', 'S1', 'B', 'S2', '2026-07-01 00:00:00'),
            ('C', 'S1', 'D', 'S2', '2026-07-02 00:00:00')
            """
        )
        await db.commit()

        candidates = [("A", "S1", "B", "S2"), ("X", "S1", "Y", "S2")]
        placeholders = ",".join("(?,?,?,?)" for _ in candidates)
        flat = [x for p in candidates for x in p]
        async with db.execute(
            f"""
            SELECT old_name, old_server, new_name, new_server
            FROM transfer_alerts_log
            WHERE (old_name, old_server, new_name, new_server) IN ({placeholders})
            """,
            tuple(flat),
        ) as cur:
            found = {tuple(r) for r in await cur.fetchall()}
        assert ("A", "S1", "B", "S2") in found
        assert ("X", "S1", "Y", "S2") not in found
        assert ("C", "S1", "D", "S2") not in found
    finally:
        await db.close()
