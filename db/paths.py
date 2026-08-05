"""資料庫路徑解析（無 aiosqlite 依賴，供 bot 與離線 cleanup 共用）。"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DB_NAME = "prasia_data.db"


def resolve_db_path(base_dir: str | Path | None = None) -> Path:
    """解析資料庫路徑：環境變數 DB_PATH 優先。"""
    env = (os.getenv("DB_PATH") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    root = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent
    return (root / DEFAULT_DB_NAME).resolve()
