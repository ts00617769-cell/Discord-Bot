"""尋人快取失效（避免 cogs 互相 lazy-import 重複包裝）。"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def invalidate_player_search_cache() -> None:
    try:
        from cogs.player_search import invalidate_search_cache
    except ImportError:
        logger.warning("無法載入尋人 cache invalidator")
        return
    invalidate_search_cache()
