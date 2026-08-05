"""線上清庫的快取失效與寫入協調。"""
from unittest.mock import patch

from cogs.war_room import _invalidate_player_search_cache


def test_cleanup_invalidates_player_search_cache():
    with patch("cogs.player_search.invalidate_search_cache") as invalidate:
        _invalidate_player_search_cache()
    invalidate.assert_called_once_with()
