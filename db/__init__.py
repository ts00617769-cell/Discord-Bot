"""資料庫連線、PRAGMA 與版本化 schema。"""

from .connection import configure_connection, connect_db
from .schema import (
    SCHEMA_VERSION,
    apply_migrations,
    build_search_indexes_sync,
    ensure_search_indexes,
    list_missing_search_indexes,
)

__all__ = [
    "SCHEMA_VERSION",
    "apply_migrations",
    "build_search_indexes_sync",
    "configure_connection",
    "connect_db",
    "ensure_search_indexes",
    "list_missing_search_indexes",
]
