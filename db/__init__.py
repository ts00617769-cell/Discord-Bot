"""資料庫連線、PRAGMA 與版本化 schema。

連線層（aiosqlite）採延遲載入，讓離線腳本（如 cleanup_db.py）
可只 import db.schema，不必在 host 安裝 aiosqlite。
"""

from __future__ import annotations

from typing import Any

from .schema import (
    SCHEMA_VERSION,
    apply_migrations,
    build_search_indexes_sync,
    ensure_search_indexes,
    list_missing_search_indexes,
    rebuild_player_profiles_sync,
)

__all__ = [
    "SCHEMA_VERSION",
    "apply_migrations",
    "build_search_indexes_sync",
    "configure_connection",
    "connect_db",
    "ensure_search_indexes",
    "list_missing_search_indexes",
    "rebuild_player_profiles_sync",
]


def __getattr__(name: str) -> Any:
    if name in ("configure_connection", "connect_db"):
        from .connection import configure_connection, connect_db

        mapping = {
            "configure_connection": configure_connection,
            "connect_db": connect_db,
        }
        return mapping[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
