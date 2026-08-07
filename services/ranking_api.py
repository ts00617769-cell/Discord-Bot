"""共用官方排名 API 客戶端（建立在 BeanfunClient 之上）。"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from .beanfun_http import (
    DEFAULT_HEADERS,
    BeanfunClient,
    FetchResult,
    get_beanfun_client,
)

logger = logging.getLogger(__name__)

RANKING_API_URL = "https://warsofprasia.beanfun.com/api/Records/PostLiveapiGCRanking"
RANKING_HEADERS = {
    **DEFAULT_HEADERS,
    "Referer": "https://warsofprasia.beanfun.com/Main/Ranking",
}
# 總榜 + 六職業（官網 Ranking class 參數；已對照台港澳職業名）
ALL_CLASSES = [
    None,
    "abyssrevenant",
    "SolarSentinel",
    "MirageBlade",
    "IncenseArcher",
    "RuneScribe",
    "Enforcer",
]

# 中文顯示名 / API key → Ranking API class 參數
CLASS_NAME_TO_KEY = {
    "深淵放逐者": "abyssrevenant",
    "太陽監視者": "SolarSentinel",
    "幻影劍士": "MirageBlade",
    "香射手": "IncenseArcher",
    "咒文刻印使": "RuneScribe",
    "執行官": "Enforcer",
    "abyssrevenant": "abyssrevenant",
    "SolarSentinel": "SolarSentinel",
    "MirageBlade": "MirageBlade",
    "IncenseArcher": "IncenseArcher",
    "RuneScribe": "RuneScribe",
    "Enforcer": "Enforcer",
}

_DEFAULT_CACHE_TTL = 45.0


def resolve_class_key(name: Optional[str]) -> Optional[str]:
    """將中文職業名或 API key 解析為 Ranking class 參數；無法對應則 None。"""
    if not name:
        return None
    key = CLASS_NAME_TO_KEY.get(name)
    if key:
        return key
    lower_map = {k.lower(): v for k, v in CLASS_NAME_TO_KEY.items()}
    return lower_map.get(name.lower())


class RankingClient:
    """透過 BeanfunClient 請求排名資料。"""

    def __init__(
        self,
        session,
        concurrency: int = 5,
        *,
        cache_ttl: float = _DEFAULT_CACHE_TTL,
        max_retries: int = 2,
        http: Optional[BeanfunClient] = None,
    ):
        self._http = http or BeanfunClient(
            session,
            concurrency=concurrency,
            cache_ttl=cache_ttl,
            max_retries=max_retries,
        )
        self.session = self._http.session

    def clear_cache(self) -> None:
        self._http.clear_cache()

    async def _post_raw(self, payload: dict, timeout: float = 10) -> FetchResult:
        return await self._http.post_json(
            RANKING_API_URL,
            payload,
            headers=RANKING_HEADERS,
            timeout=timeout,
        )

    async def fetch_class(
        self, group_id: str, world_id: str, class_key: Optional[str], limit: int = 100
    ) -> FetchResult:
        payload = {"world_group_id": group_id, "world_id": world_id, "class": class_key}
        raw = await self._post_raw(payload)
        if not raw.ok:
            return FetchResult(ok=False, error=raw.error, from_cache=raw.from_cache)
        data = raw.players[0] if raw.players else {}
        body = data.get("data")
        gc: list = []
        if body is None:
            pass
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
        """抓取單一伺服器玩家；overall_only 只打總榜（較快、樣本較窄）。"""
        if overall_only:
            result = await self.fetch_class(group_id, world_id, None, limit=limit)
            result.overall_ok = result.ok
            result.partial = False
            return result

        class_list = classes if classes is not None else ALL_CLASSES
        results = await asyncio.gather(
            *[self.fetch_class(group_id, world_id, c, limit=limit) for c in class_list]
        )
        ok_results = [r for r in results if r.ok]
        failed = [r for r in results if not r.ok]
        overall_ok = True
        if None in class_list:
            overall_ok = bool(results[class_list.index(None)].ok)

        aggregate_error: Optional[str] = None
        if failed:
            aggregate_error = (
                "session_closed"
                if any(r.error == "session_closed" for r in failed)
                else (failed[0].error or "class_fetch_failed")
            )
            logger.warning(
                f"fetch_server incomplete group={group_id} world={world_id}: "
                f"{len(failed)}/{len(results)} class endpoints failed "
                f"({aggregate_error})"
            )
            if not ok_results:
                return FetchResult(
                    ok=False,
                    error=aggregate_error,
                    partial=True,
                    overall_ok=overall_ok,
                )

        unique_players: dict[str, Any] = {}
        for res in ok_results:
            for p in res.players:
                if not isinstance(p, dict):
                    continue
                name = p.get("gc_name")
                if not name:
                    continue
                prev = unique_players.get(name)
                if prev is None:
                    unique_players[name] = p
                    continue
                # 總榜常缺旅團；後續職業榜有 guild 時補上，勿永遠卡在空字串
                prev_guild = (prev.get("guild_name") or "").strip()
                new_guild = (p.get("guild_name") or "").strip()
                if (not prev_guild or prev_guild in ("None", "null", "未知")) and new_guild:
                    merged = dict(prev)
                    merged["guild_name"] = p.get("guild_name")
                    unique_players[name] = merged
        return FetchResult(
            ok=True,
            players=list(unique_players.values()),
            error=aggregate_error,
            partial=bool(failed),
            overall_ok=overall_ok,
        )

    async def fetch_all_servers(
        self, server_map: dict, **kwargs
    ) -> tuple[list, list[str]]:
        """回傳 (players, failed_server_names)。部分服失敗時仍彙整成功資料。"""
        names = list(server_map.keys())
        tasks = [
            self.fetch_server(g_id, w_id, **kwargs)
            for _, (g_id, w_id) in server_map.items()
        ]
        results = await asyncio.gather(*tasks)
        all_players = []
        failed: list[str] = []
        for name, r in zip(names, results, strict=False):
            if r.ok:
                all_players.extend(r.players)
            else:
                failed.append(name)
        if failed:
            logger.warning(
                f"fetch_all_servers partial failure: {len(failed)}/{len(names)} "
                f"failed ({', '.join(failed)})"
            )
        return all_players, failed

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
    """取得或建立掛在 bot 上的 RankingClient（共用 BeanfunClient）。"""
    client = getattr(bot, "ranking_client", None)
    if client is None:
        ttl_raw = (os.getenv("RANKING_CACHE_TTL") or "").strip()
        try:
            ttl = float(ttl_raw) if ttl_raw else _DEFAULT_CACHE_TTL
        except ValueError:
            ttl = _DEFAULT_CACHE_TTL
        http = get_beanfun_client(bot)
        # 與 beanfun 共用同一 cache／semaphore 實例
        client = RankingClient(bot.session, cache_ttl=ttl, http=http)
        bot.ranking_client = client
    return client
