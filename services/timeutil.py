"""台北時區單一來源（zoneinfo Asia/Taipei）。"""
from __future__ import annotations

from datetime import datetime, timedelta
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


def taipei_cutoff_str(days: int) -> str:
    """台北時間往前 N 天的 naive SQL 字串（供 retention／清理用）。"""
    return (now_naive_taipei() - timedelta(days=int(days))).strftime(FMT_SQL)
