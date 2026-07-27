"""尋人 BFS 引擎（Discord 無關）。"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from services import player_matching as match
from services.game_event_windows import (
    allow_class_mismatch_high,
    allow_delayed_transfer_high,
    class_change_label,
    realm_transfer_label,
)
from services.player_search_db import PlayerSearchStore

logger = logging.getLogger(__name__)

BFS_LIMIT = 15
BFS_IDLE_STOP = 3
PROGRESS_EVERY = 10

ProgressCb = Optional[Callable[[int, int, int], Awaitable[None]]]


@dataclass
class TrackSearchResult:
    kind: str  # not_found | soft | soft | linked
    target_name: str
    unique_entries: list[dict] = field(default_factory=list)
    soft_unique: list[dict] = field(default_factory=list)
    target_last_exp: float | None = None
    tip: str = ""


def parse_track_target(raw: str, server_names: set[str]) -> tuple[str, str | None]:
    """解析 `名字 [伺服器]`；末段若為已知伺服器則拆出。"""
    text = (raw or "").strip()
    if not text:
        return "", None
    parts = text.split()
    if len(parts) >= 2 and parts[-1] in server_names:
        name = " ".join(parts[:-1]).strip()
        return name, parts[-1]
    return text, None


async def run_track_search(
    store: PlayerSearchStore,
    db,
    target_name: str,
    *,
    server_name: str | None = None,
    on_progress: ProgressCb = None,
) -> TrackSearchResult:
    """執行尋人 BFS；db 用於 exact EXP SQL（建議唯讀連線）。"""
    related_names = await store._get_related_names(target_name)
    if server_name:
        target_profiles = list(
            await store._fetch_name_profiles(target_name, server_name=server_name)
        )
        aliases = [n for n in related_names if n != target_name]
        if aliases:
            seen = {(p[0], p[1]) for p in target_profiles}
            for p in await store._fetch_profiles_by_names(aliases):
                if (p[0], p[1]) not in seen:
                    target_profiles.append(p)
                    seen.add((p[0], p[1]))
    else:
        target_profiles = await store._fetch_profiles_by_names(related_names)

    if not target_profiles:
        return TrackSearchResult(kind="not_found", target_name=target_name)

    timeline_entries: list[dict] = []
    soft_candidates: list[dict] = []
    seen_profiles: set[tuple[Any, Any]] = set()
    queue: deque = deque()
    search_deadline = time.monotonic() + match.SEARCH_TIMEOUT_SEC

    async def add_to_queue(
        p_name, p_server, m_type, d_text, e_val, profile=None, confidence="high"
    ) -> bool:
        if (p_name, p_server) in seen_profiles:
            return False
        seen_profiles.add((p_name, p_server))
        if profile is None:
            profile = await store._fetch_single_profile(p_name, p_server)
        if not profile:
            return False
        queue.append(
            {
                "profile": profile,
                "match_type": m_type,
                "diff_text": d_text,
                "exp_val": e_val,
                "confidence": confidence,
            }
        )
        return True

    for tp in target_profiles:
        label = "🎯 查詢目標" if tp[0] == target_name else "🏷️ 登錄別名"
        await add_to_queue(tp[0], tp[1], label, "", tp[6], profile=tp)

    hops = 0
    idle_hops = 0

    while queue and hops < BFS_LIMIT:
        if time.monotonic() > search_deadline:
            raise asyncio.TimeoutError()
        current = queue.popleft()
        profile = current["profile"]
        (
            t_name,
            t_server,
            t_lvl,
            t_first,
            t_last,
            t_min_exp,
            t_max_exp,
            t_cls,
            t_sub_grade,
        ) = profile

        timeline_entries.append(
            {
                "name": t_name,
                "server": t_server,
                "lvl": t_lvl,
                "cls": t_cls,
                "first": t_first,
                "last": t_last,
                "match_type": current["match_type"],
                "diff_text": current["diff_text"],
                "exp_val": current["exp_val"] or t_max_exp,
                "sub_grade": t_sub_grade,
                "confidence": current.get("confidence", "high"),
            }
        )

        if on_progress and (hops == 0 or hops % PROGRESS_EVERY == 0):
            try:
                await on_progress(hops + 1, BFS_LIMIT, len(timeline_entries))
            except Exception:
                logger.debug("progress callback failed", exc_info=True)

        anchors = await store._recent_exp_anchors(t_name, t_server, limit=8)
        early = await store._early_exp_anchors(
            t_name, t_server, t_first, days=7, limit=8
        )
        for exp in early:
            if exp not in anchors:
                anchors.append(exp)
        if t_min_exp not in anchors:
            anchors.append(t_min_exp)
        if t_max_exp not in anchors:
            anchors.append(t_max_exp)

        exact_added = False
        if anchors:
            placeholders = ",".join("?" for _ in anchors)
            sql_exact = f"""
                SELECT e.exp, e.player_name, e.server_name, MAX(e.level),
                       COALESCE(MAX(pp.class_name), '未知'),
                       MIN(e.record_time), MAX(e.record_time),
                       MAX(e.subjugation_grade)
                FROM exp_history e
                LEFT JOIN player_profile pp
                  ON pp.player_name = e.player_name
                 AND pp.server_name = e.server_name
                WHERE e.exp IN ({placeholders})
                  AND NOT (e.player_name = ? AND e.server_name = ?)
                GROUP BY e.exp, e.player_name, e.server_name
                LIMIT 30
            """
            async with db.execute(
                sql_exact, tuple(anchors + [t_name, t_server])
            ) as cursor:
                exact_matches = await cursor.fetchall()

            for (
                exp,
                p_name,
                s_name,
                lvl,
                cls_name,
                first_seen,
                last_seen,
                sub_grade,
            ) in exact_matches:
                if t_sub_grade is not None and sub_grade is not None:
                    if first_seen >= t_last and sub_grade < t_sub_grade:
                        continue
                    if last_seen <= t_first and t_sub_grade < sub_grade:
                        continue
                obs_gap = match.observation_gap_hours(
                    t_first, t_last, first_seen, last_seen,
                )
                class_ok = match.class_compatible(t_cls, cls_name)
                if first_seen >= t_last:
                    bridge_a, bridge_b = t_last, first_seen
                else:
                    bridge_a, bridge_b = last_seen, t_first

                if (
                    not class_ok
                    and allow_class_mismatch_high(
                        bridge_a,
                        bridge_b,
                        obs_gap_hours=obs_gap,
                        exact_exp=True,
                    )
                ):
                    notes = [
                        x
                        for x in (
                            class_change_label(bridge_a, bridge_b),
                            realm_transfer_label(bridge_a, bridge_b),
                        )
                        if x
                    ]
                    note = f"（{'・'.join(notes)}）" if notes else ""
                    if await add_to_queue(
                        p_name,
                        s_name,
                        f"🔄 絕對經驗值碰撞（疑似轉職）{note}",
                        "EXP 完全一致・職業已變更",
                        exp,
                        confidence="high",
                    ):
                        exact_added = True
                    continue

                delayed = allow_delayed_transfer_high(
                    bridge_a,
                    bridge_b,
                    obs_gap_hours=obs_gap,
                    exact_exp=True,
                )
                if delayed and class_ok:
                    if await add_to_queue(
                        p_name,
                        s_name,
                        f"✈️ 絕對經驗值碰撞（{delayed}・延遲登入）",
                        f"EXP 完全一致（觀測間隔 {obs_gap/24:.1f} 天）",
                        exp,
                        confidence="high",
                    ):
                        exact_added = True
                    continue

                if obs_gap > 30 * 24 or not class_ok:
                    soft_candidates.append(
                        {
                            "direction": "forward" if first_seen >= t_last else "backward",
                            "name": p_name,
                            "server": s_name,
                            "lvl": lvl,
                            "cls": cls_name,
                            "first": first_seen,
                            "last": last_seen,
                            "exp_val": exp,
                            "sub_grade": sub_grade,
                            "match_type": (
                                "🔗 絕對經驗值碰撞（職業不符）"
                                if not class_ok
                                else "🔗 絕對經驗值碰撞（時間過遠）"
                            ),
                            "diff_text": (
                                f"EXP 完全一致"
                                + (f"（觀測間隔 {obs_gap/24:.0f} 天）" if obs_gap > 0 else "")
                            ),
                            "exp_diff": 0,
                            "score": obs_gap * 1e8 + (0 if class_ok else 1e12),
                            "confidence": "medium",
                        }
                    )
                    continue
                if obs_gap > 72:
                    soft_candidates.append(
                        {
                            "direction": "forward" if first_seen >= t_last else "backward",
                            "name": p_name,
                            "server": s_name,
                            "lvl": lvl,
                            "cls": cls_name,
                            "first": first_seen,
                            "last": last_seen,
                            "exp_val": exp,
                            "sub_grade": sub_grade,
                            "match_type": "🔗 絕對經驗值碰撞（銜接偏遠）",
                            "diff_text": f"EXP 完全一致（觀測間隔 {obs_gap/24:.1f} 天）",
                            "exp_diff": 0,
                            "score": obs_gap * 1e8,
                            "confidence": "medium",
                        }
                    )
                    continue
                if await add_to_queue(
                    p_name,
                    s_name,
                    "🔗 絕對經驗值碰撞",
                    "EXP 完全一致",
                    exp,
                    confidence="high",
                ):
                    exact_added = True

        hop_added = exact_added
        if not exact_added:
            seamless = await asyncio.wait_for(
                store._find_seamless_candidates(
                    profile, None, window_days=30, limit=5
                ),
                timeout=match.QUERY_TIMEOUT_SEC,
            )
            for cand in seamless:
                soft_candidates.append(cand)
                if cand["confidence"] == "high":
                    reused = store.profile_tuple_from_row(
                        cand["name"],
                        cand["server"],
                        cand["lvl"],
                        cand["first"],
                        cand["last"],
                        cand.get("min_exp", cand["exp_val"]),
                        cand.get("max_exp", cand["exp_val"]),
                        cand["cls"],
                        cand.get("sub_grade"),
                    )
                    if await add_to_queue(
                        cand["name"],
                        cand["server"],
                        cand["match_type"],
                        cand["diff_text"],
                        cand["exp_val"],
                        profile=reused,
                        confidence="high",
                    ):
                        hop_added = True
        hops += 1
        if hop_added:
            idle_hops = 0
        else:
            idle_hops += 1
            if idle_hops >= BFS_IDLE_STOP:
                break

    unique_entries: list[dict] = []
    seen_keys: set[tuple] = set()
    for entry in timeline_entries:
        key = (entry["name"], entry["server"])
        if key in seen_keys:
            continue
        is_seed = entry["match_type"] in ("🎯 查詢目標", "🏷️ 登錄別名")
        if not is_seed and entry.get("confidence") != "high":
            continue
        seen_keys.add(key)
        unique_entries.append(entry)
    unique_entries.sort(key=lambda x: x["first"])

    has_linked = any(
        e["match_type"] not in ("🎯 查詢目標", "🏷️ 登錄別名") for e in unique_entries
    )
    only_self = len(unique_entries) <= 1 and all(
        x["name"] == target_name for x in unique_entries
    )
    target_last_exp = max(p[6] for p in target_profiles)

    if not has_linked:
        soft_unique = match.pick_soft_candidates(soft_candidates, seen_keys)
        tip = ""
        if any(p[0] == target_name for p in target_profiles):
            tip = "\n（若目標仍持續出現在原服榜上，可能尚未轉服/改名。）"
        if soft_unique and only_self:
            return TrackSearchResult(
                kind="soft",
                target_name=target_name,
                unique_entries=list(unique_entries),
                soft_unique=list(soft_unique),
                target_last_exp=target_last_exp,
                tip=tip,
            )
        if only_self:
            return TrackSearchResult(
                kind="no_link",
                target_name=target_name,
                unique_entries=list(unique_entries),
                target_last_exp=target_last_exp,
                tip=tip,
            )

    return TrackSearchResult(
        kind="linked",
        target_name=target_name,
        unique_entries=unique_entries,
        target_last_exp=target_last_exp,
    )


def causal_transfer_pairs(players: list[dict]) -> list[tuple[dict, dict, float]]:
    """A 結束後 B 開始、不可雙活躍、同職或未知。"""
    pairs = []
    for i, a in enumerate(players):
        for b in players[i + 1 :]:
            if a["server"] == b["server"]:
                continue
            if not (a["last"] < b["first"] or b["last"] < a["first"]):
                continue
            earlier, later = (a, b) if a["last"] <= b["first"] else (b, a)
            gap = match.observation_gap_hours(
                earlier["first"],
                earlier["last"],
                later["first"],
                later["last"],
            )
            if gap > 30 * 24:
                continue
            if not match.class_compatible(a.get("cls"), b.get("cls")):
                continue
            pairs.append((earlier, later, gap))
    return pairs
