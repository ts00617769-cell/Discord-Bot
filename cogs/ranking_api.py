"""共用官方排名 API 客戶端（併發限制、重試、短 TTL 快取）。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

RANKING_API_URL = "https://warsofprasia.beanfun.com/api/Records/PostLiveapiGCRanking"
RANKING_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://warsofprasia.beanfun.com",
    "Referer": "https://warsofprasia.beanfun.com/Main/Ranking",
}
ALL_CLASSES = [
    None,
    "abyssrevenant",
    "SolarSentinel",
    "MirageBlade",
    "IncenseArcher",
    "RuneScribe",
    "Enforcer",
]

# 全 bot 共用：避免背景掃描與手動指令同時打爆官方 API
_API_SEMAPHORE = asyncio.Semaphore(5)

# 預設：成功回應快取秒數；可用環境變數覆寫
_DEFAULT_CACHE_TTL = 45.0
_DEFAULT_MAX_RETRIES = 2


@dataclass
class FetchResult:
    """區分 API 失敗與真的空榜。"""

    ok: bool
    players: list = field(default_factory=list)
    error: Optional[str] = None
    from_cache: bool = False


@dataclass
class _CacheEntry:
    result: FetchResult
    expires_at: float


def _payload_cache_key(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_retryable_error(error: Optional[str]) -> bool:
    if not error:
        return False
    if error.startswith("HTTP 4"):
        return False  # 客戶端錯誤不重試
    return True


class RankingClient:
    """透過 bot.session 請求排名資料：semaphore + 重試 + 短 TTL 快取。"""

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
        cached = FetchResult(
            ok=entry.result.ok,
            players=list(entry.result.players),
            error=entry.result.error,
            from_cache=True,
        )
        return cached

    def _store_cache(self, key: str, result: FetchResult) -> None:
        if self._cache_ttl <= 0 or not result.ok:
            return
        self._cache[key] = _CacheEntry(
            result=FetchResult(ok=True, players=list(result.players), error=None),
            expires_at=time.monotonic() + self._cache_ttl,
        )

    async def _post_once(self, payload: dict, timeout: float) -> FetchResult:
        async with self._sem:
            try:
                async with self.session.post(
                    RANKING_API_URL, json=payload, headers=RANKING_HEADERS, timeout=timeout
                ) as response:
                    if response.status != 200:
                        msg = f"HTTP {response.status}"
                        logger.warning(f"Ranking API {msg} payload={payload}")
                        return FetchResult(ok=False, error=msg)
                    data = await response.json()
                    if not isinstance(data, dict):
                        msg = f"non-dict JSON type={type(data).__name__}"
                        logger.error(f"Ranking API {msg} payload={payload}")
                        return FetchResult(ok=False, error=msg)
                    return FetchResult(ok=True, players=[data])
            except asyncio.TimeoutError:
                logger.error(f"Ranking API timeout payload={payload}")
                return FetchResult(ok=False, error="timeout")
            except aiohttp.ClientError as e:
                logger.error(f"Ranking API client error: {e}")
                return FetchResult(ok=False, error=str(e))
            except (ValueError, TypeError) as e:
                logger.error(f"Ranking API JSON parse error: {e}")
                return FetchResult(ok=False, error=str(e))

    async def _post_raw(self, payload: dict, timeout: float = 10) -> FetchResult:
        """回傳 ok + 原始 dict（放在 players[0]）或錯誤；含快取與重試。"""
        key = _payload_cache_key(payload)
        cached = self._get_cached(key)
        if cached is not None:
            return cached

        last = FetchResult(ok=False, error="no_attempt")
        for attempt in range(self._max_retries + 1):
            last = await self._post_once(payload, timeout)
            if last.ok:
                self._store_cache(key, last)
                return last
            if not _is_retryable_error(last.error):
                return last
            if attempt < self._max_retries:
                delay = 0.4 * (2**attempt)
                logger.info(
                    f"Ranking API retry {attempt + 1}/{self._max_retries} "
                    f"after {last.error} (sleep {delay:.1f}s)"
                )
                await asyncio.sleep(delay)
        return last

    async def fetch_class(
        self, group_id: str, world_id: str, class_key: Optional[str], limit: int = 100
    ) -> FetchResult:
        payload = {"world_group_id": group_id, "world_id": world_id, "class": class_key}
        raw = await self._post_raw(payload)
        if not raw.ok:
            return FetchResult(ok=False, error=raw.error, from_cache=raw.from_cache)
        data = raw.players[0] if raw.players else {}
        body = data.get("data")
        if body is None:
            gc = []
        elif isinstance(body, dict):
            gc = body.get("gc") or []
        else:
            logger.error(f"Ranking API data field not dict: {type(body).__name__}")
            return FetchResult(ok=False, error="data_not_dict")
        if not isinstance(gc, list):
            logger.error(f"Ranking API gc not list: {type(gc).__name__}")
            return FetchResult(ok=False, error="gc_not_list")
        return FetchResult(
            ok=True, players=gc[:limit], from_cache=raw.from_cache
        )

    async def fetch_server(
        self,
        group_id: str,
        world_id: str,
        *,
        classes: Optional[list] = None,
        limit: int = 100,
        overall_only: bool = False,
    ) -> FetchResult:
        """抓取單一伺服器玩家；overall_only 只打總榜（討伐排名用）。"""
        if overall_only:
            return await self.fetch_class(group_id, world_id, None, limit=limit)

        class_list = classes if classes is not None else ALL_CLASSES
        results = await asyncio.gather(
            *[self.fetch_class(group_id, world_id, c, limit=limit) for c in class_list]
        )
        failed = [r for r in results if not r.ok]
        if failed:
            err = failed[0].error or "class_fetch_failed"
            logger.warning(
                f"fetch_server incomplete group={group_id} world={world_id}: "
                f"{len(failed)}/{len(results)} class endpoints failed ({err})"
            )
            return FetchResult(ok=False, error=err)

        unique_players: dict[str, Any] = {}
        for res in results:
            for p in res.players:
                if not isinstance(p, dict):
                    continue
                name = p.get("gc_name")
                if name and name not in unique_players:
                    unique_players[name] = p
        return FetchResult(ok=True, players=list(unique_players.values()))

    async def fetch_all_servers(self, server_map: dict, **kwargs) -> list:
        tasks = [
            self.fetch_server(g_id, w_id, **kwargs)
            for _, (g_id, w_id) in server_map.items()
        ]
        results = await asyncio.gather(*tasks)
        all_players = []
        for r in results:
            if r.ok:
                all_players.extend(r.players)
        return all_players

    async def probe_server(self, group_id: str, world_id: str) -> dict:
        """對單一伺服器打總榜探活（與官網 Ranking API）。"""
        result = await self.fetch_class(group_id, world_id, None, limit=3)
        players = result.players if result.ok else []
        sample_name = ""
        world_name = ""
        if players:
            sample_name = str(players[0].get("gc_name") or "")
            world_name = str(players[0].get("world_name") or "")
        return {
            "ok": bool(result.ok and players),
            "count": len(players),
            "sample_name": sample_name,
            "world_name": world_name,
            "error": result.error,
            "from_cache": result.from_cache,
        }

    async def validate_server_map(self, server_map: dict) -> dict:
        """逐服探活 SERVER_MAP；回傳 {顯示名: probe_result}。"""
        results = {}
        for name, (g_id, w_id) in server_map.items():
            probe = await self.probe_server(g_id, w_id)
            probe["group_id"] = g_id
            probe["world_id"] = w_id
            results[name] = probe
            await asyncio.sleep(0.2)
        return results


def get_ranking_client(bot) -> RankingClient:
    """取得或建立掛在 bot 上的 RankingClient。"""
    client = getattr(bot, "ranking_client", None)
    if client is None:
        import os

        ttl_raw = (os.getenv("RANKING_CACHE_TTL") or "").strip()
        try:
            ttl = float(ttl_raw) if ttl_raw else _DEFAULT_CACHE_TTL
        except ValueError:
            ttl = _DEFAULT_CACHE_TTL
        client = RankingClient(bot.session, cache_ttl=ttl)
        bot.ranking_client = client
    return client
