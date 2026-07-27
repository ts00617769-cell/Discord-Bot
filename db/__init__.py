"""資料庫連線、PRAGMA 與版本化 schema。

連線層（aiosqlite）採延遲載入，讓離線腳本（如 cleanup_db.py）
可只 import db.schema，不必在 host 安裝 aiosqlite。
"""

from __future__ import annotations

from typing import Any

from .schema import (
    SCHEMA_VERSION,
    apply_migrations,
    backfill_player_profile_denorm,
    build_search_indexes_sync,
    denorm_coverage_stats,
    ensure_search_indexes,
    list_missing_search_indexes,
    list_missing_search_indexes_async,
    rebuild_player_profiles,
    rebuild_player_profiles_sync,
)

__all__ = [
    "SCHEMA_VERSION",
    "DatabaseIntegrityError",
    "apply_migrations",
    "backfill_player_profile_denorm",
    "build_search_indexes_sync",
    "configure_connection",
    "connect_db",
    "connect_db_ro",
    "denorm_coverage_stats",
    "ensure_search_indexes",
    "integrity_quick_check",
    "list_missing_search_indexes",
    "list_missing_search_indexes_async",
    "read_db",
    "rebuild_player_profiles",
    "rebuild_player_profiles_sync",
    "resolve_db_path",
]


def __getattr__(name: str) -> Any:
    if name in (
        "configure_connection",
        "connect_db",
        "connect_db_ro",
        "integrity_quick_check",
        "read_db",
        "resolve_db_path",
        "DatabaseIntegrityError",
    ):
        from . import connection as conn_mod

        return getattr(conn_mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
