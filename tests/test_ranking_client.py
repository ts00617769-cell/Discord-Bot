"""RankingClient / BeanfunClient 重試與快取。"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.beanfun_http import BeanfunClient, FetchResult, _is_retryable_error
from services.ranking_api import RankingClient, resolve_class_key
from services.text_display import escape_like


def test_retryable_errors():
    assert _is_retryable_error("timeout") is True
    assert _is_retryable_error("HTTP 500") is True
    assert _is_retryable_error("HTTP 404") is False


@pytest.mark.asyncio
async def test_cache_returns_same_payload():
    session = MagicMock()
    http = BeanfunClient(session, cache_ttl=60.0, max_retries=0)
    ok = FetchResult(ok=True, players=[{"data": {"gc": [{"gc_name": "A"}]}}])
    http._post_once = AsyncMock(return_value=ok)  # type: ignore[method-assign]
    client = RankingClient(session, http=http)

    r1 = await client._post_raw({"world_id": "x"})
    r2 = await client._post_raw({"world_id": "x"})
    assert r1.ok and r2.ok
    assert r2.from_cache is True
    assert http._post_once.await_count == 1


@pytest.mark.asyncio
async def test_retries_on_timeout_then_succeeds():
    session = MagicMock()
    http = BeanfunClient(session, cache_ttl=0, max_retries=2)
    fail = FetchResult(ok=False, error="timeout")
    ok = FetchResult(ok=True, players=[{"data": {"gc": []}}])
    http._post_once = AsyncMock(side_effect=[fail, ok])  # type: ignore[method-assign]
    client = RankingClient(session, http=http)

    real_sleep = asyncio.sleep

    async def _fast_sleep(_):
        await real_sleep(0)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(asyncio, "sleep", AsyncMock(side_effect=_fast_sleep))
    try:
        result = await client._post_raw({"world_id": "y"})
    finally:
        monkey.undo()

    assert result.ok
    assert http._post_once.await_count == 2


@pytest.mark.asyncio
async def test_fetch_all_servers_reports_failures():
    session = MagicMock()
    client = RankingClient(session, cache_ttl=0, max_retries=0)
    ok = FetchResult(ok=True, players=[{"gc_name": "A"}])
    bad = FetchResult(ok=False, error="timeout")
    client.fetch_server = AsyncMock(side_effect=[ok, bad])  # type: ignore[method-assign]

    players, failed = await client.fetch_all_servers(
        {"服A": ("g1", "w1"), "服B": ("g2", "w2")}
    )
    assert len(players) == 1
    assert failed == ["服B"]


@pytest.mark.asyncio
async def test_fetch_class_parses_gc():
    session = MagicMock()
    client = RankingClient(session, cache_ttl=0, max_retries=0)
    client._post_raw = AsyncMock(  # type: ignore[method-assign]
        return_value=FetchResult(
            ok=True,
            players=[{"data": {"gc": [{"gc_name": "P1"}, {"gc_name": "P2"}]}}],
        )
    )
    result = await client.fetch_class("g", "w", None, limit=1)
    assert result.ok
    assert len(result.players) == 1
    assert result.players[0]["gc_name"] == "P1"


@pytest.mark.asyncio
async def test_fetch_server_merges_partial_class_success():
    """部分職業榜失敗時仍合併成功資料，不整服作廢。"""
    session = MagicMock()
    client = RankingClient(session, cache_ttl=0, max_retries=0)
    client.fetch_class = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            FetchResult(ok=True, players=[{"gc_name": "A"}]),
            FetchResult(ok=False, error="timeout"),
            FetchResult(ok=True, players=[{"gc_name": "B"}, {"gc_name": "A"}]),
        ]
    )
    result = await client.fetch_server(
        "g", "w", classes=[None, "MirageBlade", "Enforcer"]
    )
    assert result.ok
    assert result.partial is True
    assert result.overall_ok is True
    names = {p["gc_name"] for p in result.players}
    assert names == {"A", "B"}


@pytest.mark.asyncio
async def test_fetch_server_prefers_nonempty_guild():
    """總榜缺旅團時，後續職業榜有 guild 應補上。"""
    session = MagicMock()
    client = RankingClient(session, cache_ttl=0, max_retries=0)
    client.fetch_class = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            FetchResult(
                ok=True,
                players=[{"gc_name": "Hero", "guild_name": "", "gc_level": 60}],
            ),
            FetchResult(
                ok=True,
                players=[{"gc_name": "Hero", "guild_name": "狼團", "gc_level": 60}],
            ),
        ]
    )
    result = await client.fetch_server("g", "w", classes=[None, "MirageBlade"])
    assert result.ok
    assert len(result.players) == 1
    assert result.players[0]["guild_name"] == "狼團"

@pytest.mark.asyncio
async def test_fetch_server_overall_fail_marks_overall_ok_false():
    session = MagicMock()
    client = RankingClient(session, cache_ttl=0, max_retries=0)
    client.fetch_class = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            FetchResult(ok=False, error="timeout"),
            FetchResult(ok=True, players=[{"gc_name": "A"}]),
        ]
    )
    result = await client.fetch_server("g", "w", classes=[None, "Enforcer"])
    assert result.ok
    assert result.partial is True
    assert result.overall_ok is False
    assert result.players[0]["gc_name"] == "A"


@pytest.mark.asyncio
async def test_fetch_server_all_classes_fail():
    session = MagicMock()
    client = RankingClient(session, cache_ttl=0, max_retries=0)
    client.fetch_class = AsyncMock(  # type: ignore[method-assign]
        return_value=FetchResult(ok=False, error="HTTP 500")
    )
    result = await client.fetch_server("g", "w", classes=[None, "Enforcer"])
    assert not result.ok
    assert result.error == "HTTP 500"
    assert result.players == []
    assert result.overall_ok is False


def test_resolve_class_key():
    assert resolve_class_key("太陽監視者") == "SolarSentinel"
    assert resolve_class_key("深淵放逐者") == "abyssrevenant"
    assert resolve_class_key("MirageBlade") == "MirageBlade"
    assert resolve_class_key("mirageblade") == "MirageBlade"
    assert resolve_class_key("不存在") is None


def test_escape_like():
    assert escape_like("a%b_c\\d") == "a\\%b\\_c\\\\d"
