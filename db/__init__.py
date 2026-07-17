"""資料庫連線、PRAGMA 與版本化 schema。"""

from .connection import configure_connection, connect_db
from .schema import SCHEMA_VERSION, apply_migrations, ensure_search_indexes

__all__ = [
    "SCHEMA_VERSION",
    "apply_migrations",
    "configure_connection",
    "connect_db",
    "ensure_search_indexes",
]
