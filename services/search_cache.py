"""尋人結果快取（services 層，供 cogs／背景任務共用）。"""
from __future__ import annotations

import time
from typing import Any

_SEARCH_RESULT_TTL_SEC = 60.0
_SEARCH_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def get_cached_search(cache_key: str) -> dict[str, Any] | None:
    cached = _SEARCH_CACHE.get(cache_key)
    if not cached:
        return None
    stored_at, payload = cached
    if (time.monotonic() - stored_at) >= _SEARCH_RESULT_TTL_SEC:
        _SEARCH_CACHE.pop(cache_key, None)
        return None
    return payload


def set_cached_search(cache_key: str, payload: dict[str, Any]) -> None:
    _SEARCH_CACHE[cache_key] = (time.monotonic(), payload)


def invalidate_player_search_cache() -> None:
    _SEARCH_CACHE.clear()


# 相容舊名稱
def invalidate_search_cache() -> None:
    invalidate_player_search_cache()
