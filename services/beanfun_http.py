"""共用 beanfun JSON POST（semaphore、重試、短 TTL 快取）。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

BEANFUN_ORIGIN = "https://warsofprasia.beanfun.com"
DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "Origin": BEANFUN_ORIGIN,
    "Referer": f"{BEANFUN_ORIGIN}/Main/Ranking",
}

_API_SEMAPHORE = asyncio.Semaphore(5)
_DEFAULT_CACHE_TTL = 45.0
_DEFAULT_MAX_RETRIES = 2


@dataclass
class FetchResult:
    """區分 API 失敗與空資料。成功時 players[0] 為完整 JSON dict。"""

    ok: bool
    players: list = field(default_factory=list)
    error: Optional[str] = None
    from_cache: bool = False
    # ranking fetch_server：部分職業榜失敗／總榜是否成功（快照品質用）
    partial: bool = False
    overall_ok: bool = True

    @property
    def data(self) -> Any:
        """便捷取用成功回應的 JSON 根物件。"""
        if self.ok and self.players:
            return self.players[0]
        return None


@dataclass
class _CacheEntry:
    result: FetchResult
    expires_at: float


def _cache_key(url: str, payload: dict) -> str:
    raw = json.dumps(
        {"url": url, "payload": payload},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_retryable_error(error: Optional[str]) -> bool:
    if not error:
        return False
    if error.startswith("HTTP 4"):
        return False
    return True


class BeanfunClient:
    """透過 bot.session 請求 beanfun API：semaphore + 重試 + 短 TTL 快取。"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        concurrency: int = 5,
        *,
        cache_ttl: float = _DEFAULT_CACHE_TTL,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ):
        self.session = session
        self._sem = _API_SEMAPHORE if concurrency == 5 else asyncio.Semaphore(concurrency)
        self._cache_ttl = max(0.0, float(cache_ttl))
        self._max_retries = max(0, int(max_retries))
        self._cache: dict[str, _CacheEntry] = {}

    def clear_cache(self) -> None:
        self._cache.clear()

    def _get_cached(self, key: str) -> Optional[FetchResult]:
        if self._cache_ttl <= 0:
            return None
        entry = self._cache.get(key)
        if not entry:
            return None
        if time.monotonic() >= entry.expires_at:
            self._cache.pop(key, None)
            return None
        return FetchResult(
            ok=entry.result.ok,
            players=list(entry.result.players),
            error=entry.result.error,
            from_cache=True,
        )

    def _store_cache(self, key: str, result: FetchResult) -> None:
        if self._cache_ttl <= 0 or not result.ok:
            return
        self._cache[key] = _CacheEntry(
            result=FetchResult(ok=True, players=list(result.players), error=None),
            expires_at=time.monotonic() + self._cache_ttl,
        )

    async def _post_once(
        self,
        url: str,
        payload: dict,
        headers: dict,
        timeout: float,
    ) -> FetchResult:
        async with self._sem:
            try:
                async with self.session.post(
                    url, json=payload, headers=headers, timeout=timeout
                ) as response:
                    if response.status != 200:
                        msg = f"HTTP {response.status}"
                        logger.warning(f"Beanfun API {msg} url={url}")
                        return FetchResult(ok=False, error=msg)
                    data = await response.json()
                    return FetchResult(ok=True, players=[data])
            except asyncio.TimeoutError:
                logger.error(f"Beanfun API timeout url={url}")
                return FetchResult(ok=False, error="timeout")
            except aiohttp.ClientError as e:
                logger.error(f"Beanfun API client error: {e}")
                return FetchResult(ok=False, error=str(e))
            except (ValueError, TypeError) as e:
                logger.error(f"Beanfun API JSON parse error: {e}")
                return FetchResult(ok=False, error=str(e))

    async def post_json(
        self,
        url: str,
        payload: Optional[dict] = None,
        *,
        headers: Optional[dict] = None,
        timeout: float = 10,
        use_cache: bool = True,
    ) -> FetchResult:
        """POST JSON；成功時 result.data 為回應根物件。"""
        body = payload if payload is not None else {}
        hdrs = {**DEFAULT_HEADERS, **(headers or {})}
        key = _cache_key(url, body) if use_cache else ""

        if use_cache:
            cached = self._get_cached(key)
            if cached is not None:
                return cached

        last = FetchResult(ok=False, error="no_attempt")
        for attempt in range(self._max_retries + 1):
            last = await self._post_once(url, body, hdrs, timeout)
            if last.ok:
                if use_cache:
                    self._store_cache(key, last)
                return last
            if not _is_retryable_error(last.error):
                return last
            if attempt < self._max_retries:
                delay = 0.4 * (2**attempt)
                logger.info(
                    f"Beanfun API retry {attempt + 1}/{self._max_retries} "
                    f"after {last.error} (sleep {delay:.1f}s)"
                )
                await asyncio.sleep(delay)
        return last


def get_beanfun_client(bot) -> BeanfunClient:
    """取得或建立掛在 bot 上的 BeanfunClient。"""
    client = getattr(bot, "beanfun_client", None)
    if client is None:
        ttl_raw = (os.getenv("RANKING_CACHE_TTL") or "").strip()
        try:
            ttl = float(ttl_raw) if ttl_raw else _DEFAULT_CACHE_TTL
        except ValueError:
            ttl = _DEFAULT_CACHE_TTL
        client = BeanfunClient(bot.session, cache_ttl=ttl)
        bot.beanfun_client = client
    return client
