"""共用官方排名 API 客戶端（含併發限制）。"""
from __future__ import annotations

import asyncio
import logging
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


@dataclass
class FetchResult:
    """區分 API 失敗與真的空榜。"""

    ok: bool
    players: list = field(default_factory=list)
    error: Optional[str] = None


class RankingClient:
    """透過 bot.session 請求排名資料，並以 semaphore 限制併發。"""

    def __init__(self, session: aiohttp.ClientSession, concurrency: int = 5):
        self.session = session
        self._sem = _API_SEMAPHORE if concurrency == 5 else asyncio.Semaphore(concurrency)

    async def _post_raw(self, payload: dict, timeout: float = 10) -> FetchResult:
        """回傳 ok + 原始 dict（放在 players[0]）或錯誤。"""
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

    async def fetch_class(
        self, group_id: str, world_id: str, class_key: Optional[str], limit: int = 100
    ) -> FetchResult:
        payload = {"world_group_id": group_id, "world_id": world_id, "class": class_key}
        raw = await self._post_raw(payload)
        if not raw.ok:
            return FetchResult(ok=False, error=raw.error)
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
        return FetchResult(ok=True, players=gc[:limit])

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
        client = RankingClient(bot.session)
        bot.ranking_client = client
    return client
