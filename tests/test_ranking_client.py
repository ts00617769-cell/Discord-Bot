"""RankingClient / BeanfunClient 重試與快取。"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.beanfun_http import BeanfunClient, FetchResult, _is_retryable_error
from services.ranking_api import RankingClient


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
