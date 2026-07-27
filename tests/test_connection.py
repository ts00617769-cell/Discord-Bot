"""連線層：integrity quick_check、唯讀連線。"""
from __future__ import annotations

import sqlite3

import aiosqlite
import pytest

from db.connection import (
    connect_db,
    connect_db_ro,
    integrity_quick_check,
)
from db.schema import SCHEMA_VERSION, apply_migrations


@pytest.mark.asyncio
async def test_integrity_quick_check_ok(tmp_path):
    db_path = tmp_path / "ok.db"
    db = await connect_db(db_path, check_integrity=True)
    try:
        await integrity_quick_check(db)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_connect_db_ro_and_cannot_write(tmp_path):
    db_path = tmp_path / "ro.db"
    rw = await connect_db(db_path, check_integrity=True)
    try:
        await apply_migrations(rw)
    finally:
        await rw.close()

    ro = await connect_db_ro(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            await ro.execute(
                "INSERT INTO bot_settings (key, value) VALUES ('x', '1')"
            )
    finally:
        await ro.close()


@pytest.mark.asyncio
async def test_schema_has_alert_dedupe(tmp_path):
    db_path = tmp_path / "v6.db"
    db = await aiosqlite.connect(str(db_path))
    try:
        ver = await apply_migrations(db)
        assert ver == SCHEMA_VERSION
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alert_dedupe'"
        ) as cur:
            assert await cur.fetchone() is not None
    finally:
        await db.close()
