"""台北時區單一來源（zoneinfo Asia/Taipei）。"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
FMT_SQL = "%Y-%m-%d %H:%M:%S"
FMT_DATE = "%Y-%m-%d"


def now_taipei() -> datetime:
    """回傳台北當下（aware）。"""
    return datetime.now(TAIPEI)


def now_naive_taipei() -> datetime:
    """回傳台北當下的 naive datetime（與既有 SQLite 字串相容）。"""
    return now_taipei().replace(tzinfo=None)


def today_taipei_str() -> str:
    return now_taipei().strftime(FMT_DATE)
