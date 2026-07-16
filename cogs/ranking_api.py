"""共用官方排名 API 客戶端（含併發限制）。"""
import asyncio
import logging
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


class RankingClient:
    """透過 bot.session 請求排名資料，並以 semaphore 限制併發。"""

    def __init__(self, session: aiohttp.ClientSession, concurrency: int = 5):
        self.session = session
        self._sem = _API_SEMAPHORE if concurrency == 5 else asyncio.Semaphore(concurrency)

    async def _post(self, payload: dict, timeout: float = 10) -> Optional[dict]:
        async with self._sem:
            try:
                async with self.session.post(
                    RANKING_API_URL, json=payload, headers=RANKING_HEADERS, timeout=timeout
                ) as response:
                    if response.status != 200:
                        logger.warning(f"Ranking API HTTP {response.status} payload={payload}")
                        return None
                    return await response.json()
            except asyncio.TimeoutError:
                logger.error(f"Ranking API timeout payload={payload}")
            except aiohttp.ClientError as e:
                logger.error(f"Ranking API client error: {e}")
            except (ValueError, TypeError) as e:
                logger.error(f"Ranking API JSON parse error: {e}")
            return None

    async def fetch_class(
        self, group_id: str, world_id: str, class_key: Optional[str], limit: int = 100
    ) -> list:
        payload = {"world_group_id": group_id, "world_id": world_id, "class": class_key}
        data = await self._post(payload)
        if not data:
            return []
        return (data.get("data") or {}).get("gc", [])[:limit]

    async def fetch_server(
        self,
        group_id: str,
        world_id: str,
        *,
        classes: Optional[list] = None,
        limit: int = 100,
        overall_only: bool = False,
    ) -> list:
        """抓取單一伺服器玩家；overall_only 只打總榜（討伐排名用）。"""
        if overall_only:
            return await self.fetch_class(group_id, world_id, None, limit=limit)

        class_list = classes if classes is not None else ALL_CLASSES
        results = await asyncio.gather(
            *[self.fetch_class(group_id, world_id, c, limit=limit) for c in class_list]
        )
        unique_players: dict[str, Any] = {}
        for res in results:
            for p in res:
                name = p.get("gc_name")
                if name and name not in unique_players:
                    unique_players[name] = p
        return list(unique_players.values())

    async def fetch_all_servers(self, server_map: dict, **kwargs) -> list:
        tasks = [
            self.fetch_server(g_id, w_id, **kwargs)
            for _, (g_id, w_id) in server_map.items()
        ]
        results = await asyncio.gather(*tasks)
        all_players = []
        for r in results:
            all_players.extend(r)
        return all_players

    async def probe_server(self, group_id: str, world_id: str) -> dict:
        """對單一伺服器打總榜探活（與官網 Ranking 同一支 API）。"""
        players = await self.fetch_class(group_id, world_id, None, limit=3)
        sample_name = ""
        world_name = ""
        if players:
            sample_name = str(players[0].get("gc_name") or "")
            world_name = str(players[0].get("world_name") or "")
        return {
            "ok": bool(players),
            "count": len(players),
            "sample_name": sample_name,
            "world_name": world_name,
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
