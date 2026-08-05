"""尋人結果快取（services 層，供 cogs／背景任務共用）。"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

_SEARCH_RESULT_TTL_SEC = 60.0
# 上限避免短時間大量不同查詢把記憶體撐大（快照寫入前不會自動清空）
_SEARCH_CACHE_MAX_ENTRIES = 256
_SEARCH_CACHE: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()


def _purge_expired(now: float) -> None:
    expired = [
        key
        for key, (stored_at, _payload) in _SEARCH_CACHE.items()
        if (now - stored_at) >= _SEARCH_RESULT_TTL_SEC
    ]
    for key in expired:
        _SEARCH_CACHE.pop(key, None)


def get_cached_search(cache_key: str) -> dict[str, Any] | None:
    cached = _SEARCH_CACHE.get(cache_key)
    if not cached:
        return None
    stored_at, payload = cached
    if (time.monotonic() - stored_at) >= _SEARCH_RESULT_TTL_SEC:
        _SEARCH_CACHE.pop(cache_key, None)
        return None
    _SEARCH_CACHE.move_to_end(cache_key)
    return payload


def set_cached_search(cache_key: str, payload: dict[str, Any]) -> None:
    now = time.monotonic()
    _SEARCH_CACHE[cache_key] = (now, payload)
    _SEARCH_CACHE.move_to_end(cache_key)
    _purge_expired(now)
    while len(_SEARCH_CACHE) > _SEARCH_CACHE_MAX_ENTRIES:
        _SEARCH_CACHE.popitem(last=False)


def cached_search_size() -> int:
    return len(_SEARCH_CACHE)


def invalidate_player_search_cache() -> None:
    _SEARCH_CACHE.clear()


# 相容舊名稱
def invalidate_search_cache() -> None:
    invalidate_player_search_cache()
