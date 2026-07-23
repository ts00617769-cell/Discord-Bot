"""天眼尋人 / 轉服掃描指令（匹配邏輯見 services.player_matching）。"""
import asyncio
import logging
import sqlite3
import time
import traceback
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands

from services import player_matching as match
from services.error_handler import allowed_channel, parse_env_channel_ids
from services.game_event_windows import (
    allow_class_mismatch_high,
    allow_delayed_transfer_high,
    class_change_label,
    realm_transfer_label,
)
from services.player_search_db import PlayerSearchStore
from services.timeutil import now_naive_taipei

logger = logging.getLogger(__name__)

# 同名短時間內快取完整回覆內容，避免連點重算
_SEARCH_RESULT_TTL_SEC = 180.0
_SEARCH_CACHE: dict[str, tuple[float, dict]] = {}

BFS_LIMIT = 15
BFS_IDLE_STOP = 3  # 連續幾 hop 沒擴出新 high 就停
PROGRESS_EVERY = 10


class PlayerSearch(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = PlayerSearchStore(bot.db)

    @property
    def TRANSFER_ALERT_CHANNEL_IDS(self):
        return parse_env_channel_ids(env_name="TRANSFER_ALERT_CHANNEL_ID")

    async def _fetch_name_profiles(self, player_name, server_name=None):
        return await self.store._fetch_name_profiles(player_name, server_name)

    async def _fetch_single_profile(self, player_name, server_name):
        return await self.store._fetch_single_profile(player_name, server_name)

    async def _fetch_profiles_by_names(self, names):
        return await self.store._fetch_profiles_by_names(names)

    async def _get_related_names(self, target_name):
        return await self.store._get_related_names(target_name)

    async def _recent_exp_anchors(self, player_name, server_name, limit=8):
        return await self.store._recent_exp_anchors(player_name, server_name, limit=limit)

    async def _early_exp_anchors(self, player_name, server_name, first_seen, days=7, limit=8):
        return await self.store._early_exp_anchors(
            player_name, server_name, first_seen, days=days, limit=limit
        )

    async def _early_window_min_exp(self, player_name, server_name, first_seen, days=7):
        return await self.store._early_window_min_exp(
            player_name, server_name, first_seen, days=days
        )

    async def _find_seamless_candidates(self, profile, exp_margin=None, window_days=30, limit=8):
        return await asyncio.wait_for(
            self.store._find_seamless_candidates(
                profile, exp_margin, window_days=window_days, limit=limit
            ),
            timeout=match.QUERY_TIMEOUT_SEC,
        )

    @commands.command(
        name="尋人回報",
        help="手動標記玩家前身身分。用法: !尋人回報 驕傲o 某某某 艾雲o 或 !尋人回報 驕傲o 清除",
    )
    @allowed_channel()
    async def report_identity(self, ctx, *args):
        args_list = [arg for arg in args if arg.strip()]
        if len(args_list) < 2:
            return await ctx.send(
                "❌ 參數不足！用法範例：`!尋人回報 驕傲o 某某某 艾雲o` 或 `!尋人回報 驕傲o 清除`"
            )

        current_name = args_list[0]
        original_names = args_list[1:]

        if len(original_names) == 1 and original_names[0] == "清除":
            try:
                await self.bot.db.execute(
                    "DELETE FROM member_registry WHERE player_name = ?", (current_name,)
                )
                await self.bot.db.commit()
                await ctx.send(f"✅ 已成功清除【{current_name}】的身分標記。")
            except sqlite3.DatabaseError as e:
                logger.error(f"Error clearing member info for '{current_name}': {e}")
                await ctx.send("❌ 清除失敗（資料庫錯誤）")
            return

        try:
            async with self.bot.db.execute(
                "SELECT original_identity FROM member_registry WHERE player_name = ?",
                (current_name,),
            ) as cursor:
                result = await cursor.fetchone()

            existing_identities = []
            if result and result[0]:
                existing_identities = [x.strip() for x in result[0].split(",")]

            added_names = []
            for name in original_names:
                if name not in existing_identities:
                    existing_identities.append(name)
                    added_names.append(name)

            if not added_names:
                return await ctx.send(
                    f"⚠️ 你輸入的名字都已經標記過了。目前的標記為：({result[0]})"
                )

            new_identity_str = ", ".join(existing_identities)
            await self.bot.db.execute(
                '''
                INSERT INTO member_registry (player_name, original_identity)
                VALUES (?, ?)
                ON CONFLICT(player_name) DO UPDATE SET original_identity=excluded.original_identity
                ''',
                (current_name, new_identity_str),
            )
            await self.bot.db.commit()
            await ctx.send(
                f"✅ 已成功為【{current_name}】新增身分標記！目前累計的身分：【{new_identity_str}】"
            )
        except sqlite3.DatabaseError as e:
            logger.error(f"Error updating member info for '{current_name}': {e}")
            await ctx.send("❌ 標記失敗（資料庫錯誤）")

    @commands.hybrid_command(
        name="尋人",
        help="利用經驗值特徵，精準追蹤改名或轉服的玩家。用法: !尋人 驕傲o",
    )
    @app_commands.describe(target_name="玩家名稱")
    @commands.cooldown(1, 15, commands.BucketType.user)
    @commands.max_concurrency(2, commands.BucketType.user, wait=False)
    @allowed_channel()
    async def track_player(self, ctx: commands.Context, *, target_name: str):
        target_name = (target_name or "").strip()
        if not target_name:
            await ctx.send("❌ 請輸入玩家名稱。用法：`!尋人 小碎冰`")
            return

        logger.info(
            f"!尋人 start user={ctx.author.id} channel={ctx.channel.id} "
            f"parent={getattr(ctx.channel, 'parent_id', None)} name={target_name!r}"
        )

        # 立刻給可見回饋（表情 + 回覆），避免長查詢看起來像沒反應
        if ctx.message is not None:
            try:
                await ctx.message.add_reaction("🔍")
            except discord.HTTPException:
                pass

        try:
            if ctx.interaction is not None:
                processing_msg = await ctx.send(
                    f"🔍 **天眼啟動**，正在掃描「**{target_name}**」…\n"
                    f"⏳ 比對經驗特徵中，請稍候（通常數秒～數十秒）。"
                )
            else:
                processing_msg = await ctx.reply(
                    f"🔍 **天眼啟動**，正在掃描「**{target_name}**」…\n"
                    f"⏳ 比對經驗特徵中，請稍候（通常數秒～數十秒）。",
                    mention_author=False,
                )
        except discord.HTTPException as e:
            logger.error(f"!尋人 無法在頻道 {ctx.channel.id} 發送訊息: {e}")
            return

        try:
            async with ctx.typing():
                cache_key = target_name.casefold()
                cached = _SEARCH_CACHE.get(cache_key)
                if cached and (time.monotonic() - cached[0]) < _SEARCH_RESULT_TTL_SEC:
                    payload = cached[1]
                    if payload.get("kind") == "embeds":
                        await self._deliver_embeds(ctx, processing_msg, payload["embeds"])
                        return
                    if payload.get("kind") == "text":
                        return await processing_msg.edit(content=payload["content"])

                related_names = await self._get_related_names(target_name)
                target_profiles = await self._fetch_profiles_by_names(related_names)

                if not target_profiles:
                    return await processing_msg.edit(
                        content=f"❌ 天眼系統找不到「{target_name}」的任何歷史紀錄。"
                    )

                timeline_entries = []
                soft_candidates = []
                seen_profiles = set()
                queue = deque()
                search_deadline = time.monotonic() + match.SEARCH_TIMEOUT_SEC

                async def add_to_queue(
                    p_name, p_server, m_type, d_text, e_val, profile=None, confidence="high"
                ) -> bool:
                    if (p_name, p_server) in seen_profiles:
                        return False
                    seen_profiles.add((p_name, p_server))
                    if profile is None:
                        profile = await self._fetch_single_profile(p_name, p_server)
                    if not profile:
                        return False
                    queue.append({
                        "profile": profile,
                        "match_type": m_type,
                        "diff_text": d_text,
                        "exp_val": e_val,
                        "confidence": confidence,
                    })
                    return True

                for tp in target_profiles:
                    label = "🎯 查詢目標" if tp[0] == target_name else "🏷️ 登錄別名"
                    await add_to_queue(tp[0], tp[1], label, "", tp[6], profile=tp)

                bfs_limit = BFS_LIMIT
                hops = 0
                idle_hops = 0

                while queue and hops < bfs_limit:
                    if time.monotonic() > search_deadline:
                        raise asyncio.TimeoutError()
                    current = queue.popleft()
                    profile = current["profile"]
                    t_name, t_server, t_lvl, t_first, t_last, t_min_exp, t_max_exp, t_cls, t_sub_grade = profile

                    timeline_entries.append({
                        "name": t_name, "server": t_server, "lvl": t_lvl, "cls": t_cls,
                        "first": t_first, "last": t_last,
                        "match_type": current["match_type"],
                        "diff_text": current["diff_text"],
                        "exp_val": current["exp_val"] or t_max_exp,
                        "sub_grade": t_sub_grade,
                        "confidence": current.get("confidence", "high"),
                    })

                    if hops == 0 or hops % PROGRESS_EVERY == 0:
                        try:
                            await processing_msg.edit(
                                content=(
                                    f"🔍 **天眼掃描中**「**{target_name}**」…\n"
                                    f"⏳ 進度：第 {hops + 1}/{bfs_limit} 步"
                                    f"（已鎖定 {len(timeline_entries)} 筆軌跡）"
                                )
                            )
                        except discord.HTTPException:
                            pass

                    anchors = await self._recent_exp_anchors(t_name, t_server, limit=8)
                    early = await self._early_exp_anchors(
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
                        sql_exact = f'''
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
                        '''
                        async with self.bot.db.execute(
                            sql_exact, tuple(anchors + [t_name, t_server])
                        ) as cursor:
                            exact_matches = await cursor.fetchall()

                        for exp, p_name, s_name, lvl, cls_name, first_seen, last_seen, sub_grade in exact_matches:
                            if t_sub_grade is not None and sub_grade is not None:
                                if first_seen >= t_last and sub_grade < t_sub_grade:
                                    continue
                                if last_seen <= t_first and t_sub_grade < sub_grade:
                                    continue
                            # 觀測區間完全無重疊且間隔 > 30 天：不進主軌 BFS，改列 soft
                            obs_gap = match.observation_gap_hours(
                                t_first, t_last, first_seen, last_seen,
                            )
                            class_ok = match.class_compatible(t_cls, cls_name)
                            # 銜接端點：較早觀測的 last ↔ 較晚觀測的 first
                            if first_seen >= t_last:
                                bridge_a, bridge_b = t_last, first_seen
                            else:
                                bridge_a, bridge_b = last_seen, t_first

                            # EXP 完全一致 + 觀測銜接緊密 + 職業不符 → 疑似轉職（可進主軌）
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

                            # 同職／異職：領域轉移後延遲登入（消失數日才上新服榜）
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
                                soft_candidates.append({
                                    "direction": "forward" if first_seen >= t_last else "backward",
                                    "name": p_name, "server": s_name, "lvl": lvl, "cls": cls_name,
                                    "first": first_seen, "last": last_seen, "exp_val": exp,
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
                                })
                                continue
                            # 觀測窗需銜接（重疊或間隔 ≤ 72h）才進 high 主軌
                            # （延遲登入已在上方用領域轉移窗放行）
                            if obs_gap > 72:
                                soft_candidates.append({
                                    "direction": "forward" if first_seen >= t_last else "backward",
                                    "name": p_name, "server": s_name, "lvl": lvl, "cls": cls_name,
                                    "first": first_seen, "last": last_seen, "exp_val": exp,
                                    "sub_grade": sub_grade,
                                    "match_type": "🔗 絕對經驗值碰撞（銜接偏遠）",
                                    "diff_text": f"EXP 完全一致（觀測間隔 {obs_gap/24:.1f} 天）",
                                    "exp_diff": 0,
                                    "score": obs_gap * 1e8,
                                    "confidence": "medium",
                                })
                                continue
                            if await add_to_queue(
                                p_name, s_name, "🔗 絕對經驗值碰撞", "EXP 完全一致", exp,
                                confidence="high",
                            ):
                                exact_added = True

                    hop_added = exact_added
                    # Exact 已擴出 high 時略過 seamless；margin 分層由 store 處理
                    if not exact_added:
                        seamless = await self._find_seamless_candidates(
                            profile, None, window_days=30, limit=5
                        )
                        for cand in seamless:
                            soft_candidates.append(cand)
                            if cand["confidence"] == "high":
                                reused = self.store.profile_tuple_from_row(
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
                                    cand["name"], cand["server"], cand["match_type"],
                                    cand["diff_text"], cand["exp_val"],
                                    profile=reused, confidence="high",
                                ):
                                    hop_added = True
                    hops += 1
                    if hop_added:
                        idle_hops = 0
                    else:
                        idle_hops += 1
                        if idle_hops >= BFS_IDLE_STOP:
                            break

                unique_entries = []
                seen = set()
                for entry in timeline_entries:
                    key = (entry["name"], entry["server"])
                    if key in seen:
                        continue
                    is_seed = entry["match_type"] in ("🎯 查詢目標", "🏷️ 登錄別名")
                    if not is_seed and entry.get("confidence") != "high":
                        continue
                    seen.add(key)
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
                    soft_unique = match.pick_soft_candidates(soft_candidates, seen)

                    if soft_unique and only_self:
                        embeds = self._build_track_embeds(
                            target_name,
                            list(unique_entries) + list(soft_unique),
                            header=(
                                f"⚠️ **未找到高信心軌跡**，以下是「{target_name}」的**可疑候選**：\n\n"
                            ),
                            title=f"👁️ 天眼追蹤（可疑候選）- {target_name}",
                            color=0xf39c12,
                            footer="僅供參考：請用 !尋人回報 確認後可提升後續追蹤精度",
                            show_confidence=True,
                        )
                        _SEARCH_CACHE[cache_key] = (
                            time.monotonic(),
                            {"kind": "embeds", "embeds": embeds},
                        )
                        await self._deliver_embeds(ctx, processing_msg, embeds)
                        return

                    if only_self:
                        tip = ""
                        if any(p[0] == target_name for p in target_profiles):
                            tip = "\n（若目標仍持續出現在原服榜上，可能尚未轉服/改名。）"
                        content = (
                            f"⚠️ 目標最後紀錄為 {target_last_exp/1000000000000:.2f} 兆。\n"
                            f"雙引擎未找到符合條件的轉服/改名軌跡。{tip}\n"
                            f"提示：可用 `!尋人回報 {target_name} 前身名` 手動標記後再查。"
                        )
                        _SEARCH_CACHE[cache_key] = (
                            time.monotonic(),
                            {"kind": "text", "content": content},
                        )
                        return await processing_msg.edit(content=content)

                embeds = self._build_track_embeds(
                    target_name,
                    unique_entries,
                    header=f"🚨 **啟動雙引擎掃描，成功捕捉「{target_name}」的軌跡！**\n\n",
                    title=f"👁️ 天眼追蹤系統 (V6) - {target_name}",
                    color=0xff0000,
                    footer="V6：denorm stats・tiered margin・covering indexes",
                )
                _SEARCH_CACHE[cache_key] = (
                    time.monotonic(),
                    {"kind": "embeds", "embeds": embeds},
                )
                await self._deliver_embeds(ctx, processing_msg, embeds)

        except sqlite3.DatabaseError as e:
            logger.error(f"DB error tracking '{target_name}': {e}\n{traceback.format_exc()}")
            try:
                await processing_msg.edit(content="❌ 天眼系統資料庫錯誤")
            except discord.NotFound:
                pass
        except asyncio.TimeoutError:
            try:
                await processing_msg.edit(content="❌ 天眼系統查詢逾時，請重試")
            except discord.NotFound:
                pass
        except KeyError as e:
            logger.error(f"Missing field in player tracking: {e}")
            try:
                await processing_msg.edit(content="❌ 尋人系統資料欄位異常")
            except discord.NotFound:
                pass
        except discord.HTTPException as e:
            logger.error(f"!尋人 Discord 錯誤 '{target_name}': {e}")
        except (ValueError, TypeError) as e:
            logger.error(f"!尋人 資料錯誤 '{target_name}': {e}\n{traceback.format_exc()}")
            try:
                await processing_msg.edit(content=f"❌ 尋人系統資料異常: {type(e).__name__}")
            except discord.NotFound:
                pass

    @staticmethod
    def _format_track_entry(idx, p, *, show_confidence=False):
        """單筆軌跡 yaml 行。無 diff_text 時顯示 EXP；有則顯示關聯。"""
        exp_zhao = p["exp_val"] / 1_000_000_000_000
        conf = p.get("confidence", "high")
        if show_confidence or conf != "high":
            type_line = f"   ▶ {p['match_type']} ({conf})\n"
        else:
            type_line = f"   ▶ {p['match_type']}\n"
        lines = (
            f"{idx}. {p['name']} [{p['server']}]\n"
            f"{type_line}"
            f"   ▶ 職業: {p['cls']} | Lv.{p['lvl']} | 討伐 {p.get('sub_grade', 0)}\n"
            f"   ▶ 觀測: {p['first'][5:16]} ~ {p['last'][5:16]}\n"
        )
        if p.get("diff_text"):
            lines += f"   ▶ 關聯: {p['diff_text']} (特徵: {exp_zhao:,.2f}兆)\n\n"
        else:
            lines += f"   ▶ EXP : {exp_zhao:,.2f} 兆\n\n"
        return lines

    def _build_track_embeds(
        self,
        target_name,
        entries,
        *,
        header,
        title,
        color,
        footer,
        show_confidence=False,
    ):
        """將軌跡 entries 切成多個 Embed（description 約 3800 字切頁）。"""
        embeds = []
        desc = f"{header}```yaml\n"
        for idx, p in enumerate(entries, 1):
            entry = self._format_track_entry(idx, p, show_confidence=show_confidence)
            if len(desc) + len(entry) > 3800:
                desc += "```"
                embeds.append(discord.Embed(title=title, description=desc, color=color))
                desc = "```yaml\n" + entry
            else:
                desc += entry
        if desc != "```yaml\n":
            desc += "```"
            embeds.append(discord.Embed(title=title, description=desc, color=color))
        if embeds:
            embeds[-1].set_footer(text=footer)
        return embeds

    async def _deliver_embeds(self, ctx, processing_msg, embeds):
        """先 edit 第一則，其餘再 send；避免 delete 後失敗變成無回應。"""
        if not embeds:
            try:
                await processing_msg.edit(content="❌ 無結果可顯示。")
            except discord.HTTPException:
                pass
            return
        try:
            await processing_msg.edit(content=None, embed=embeds[0])
        except discord.HTTPException as e:
            logger.error(f"edit processing_msg failed: {e}")
            try:
                await ctx.send(embed=embeds[0])
            except discord.HTTPException:
                return
        for embed in embeds[1:]:
            try:
                await ctx.send(embed=embed)
            except discord.HTTPException as e:
                logger.error(f"send embed failed: {e}")
                break

    @track_player.error
    async def track_player_error(self, ctx, error):
        """參數錯誤給專屬提示；冷卻／併發交由 WarRoom 統一處理。"""
        if isinstance(error, commands.MissingRequiredArgument):
            try:
                await ctx.send("❌ 請輸入玩家名稱。用法：`!尋人 小碎冰`")
            except discord.HTTPException:
                pass
            return

    @commands.command(
        name="轉服掃描",
        aliases=["移民清單", "抓包"],
        help="全服掃描近期利用轉服空窗期改名或移動的玩家",
    )
    @commands.cooldown(1, 30, commands.BucketType.guild)
    @commands.max_concurrency(1, commands.BucketType.default, wait=False)
    @allowed_channel()
    async def global_transfer_scan(self, ctx):
        processing_msg = await ctx.send("📡 正在進行全資料庫特徵碰撞比對，這可能需要幾秒鐘...")

        try:
            async with self.bot.db.execute(
                '''
                SELECT exp
                FROM exp_history
                WHERE exp > 1000000000000
                GROUP BY exp
                HAVING COUNT(DISTINCT server_name) > 1
                ORDER BY MAX(record_time) DESC
                LIMIT 30
                '''
            ) as cursor:
                shared_exps = await cursor.fetchall()

            if not shared_exps:
                return await processing_msg.edit(
                    content="💤 目前資料庫中沒有偵測到任何轉服或改名的活動軌跡。"
                )

            exp_list = [row[0] for row in shared_exps]
            placeholders = ",".join("?" for _ in exp_list)

            async with self.bot.db.execute(
                f'''
                SELECT e.exp, e.player_name, e.server_name,
                       MIN(e.record_time), MAX(e.record_time),
                       MAX(e.level),
                       COALESCE(MAX(pp.class_name), '未知'),
                       MAX(e.subjugation_grade)
                FROM exp_history e
                LEFT JOIN player_profile pp
                  ON pp.player_name = e.player_name
                 AND pp.server_name = e.server_name
                WHERE e.exp IN ({placeholders})
                GROUP BY e.exp, e.player_name, e.server_name
                ORDER BY e.exp DESC, MIN(e.record_time) ASC
                ''',
                tuple(exp_list),
            ) as cursor:
                records = await cursor.fetchall()

            grouped_data = {}
            for exp, p_name, s_name, first_seen, last_seen, lvl, cls, sub in records:
                grouped_data.setdefault(exp, []).append({
                    "name": p_name, "server": s_name,
                    "first": first_seen, "last": last_seen,
                    "lvl": lvl, "cls": cls, "sub": sub,
                })

            def _causal_pairs(players):
                """A 結束後 B 開始、不可雙活躍、同職或未知。"""
                pairs = []
                for i, a in enumerate(players):
                    for b in players[i + 1:]:
                        if a["server"] == b["server"]:
                            continue
                        # 不可觀測區間重疊（雙活躍）
                        if not (
                            a["last"] < b["first"] or b["last"] < a["first"]
                        ):
                            continue
                        earlier, later = (a, b) if a["last"] <= b["first"] else (b, a)
                        gap = match.observation_gap_hours(
                            earlier["first"], earlier["last"],
                            later["first"], later["last"],
                        )
                        if gap > 30 * 24:
                            continue
                        a_cls, b_cls = a.get("cls"), b.get("cls")
                        if not match.class_compatible(a_cls, b_cls):
                            continue
                        pairs.append((earlier, later, gap))
                return pairs

            embeds = []
            desc = "🔍 **以下玩家被系統偵測到經驗值完全重疊（含因果時間窗）：**\n\n"
            shown = 0
            for exp, players in grouped_data.items():
                causal = _causal_pairs(players)
                if not causal:
                    continue
                exp_zhao = exp / 1_000_000_000_000
                desc += f"🔗 **特徵碼：{exp_zhao:.3f} 兆**\n```yaml\n"
                for earlier, later, gap in causal[:3]:
                    desc += (
                        f"• {earlier['name']} [{earlier['server']}] "
                        f"({earlier['first'][5:16]}~{earlier['last'][5:16]})\n"
                        f"  → {later['name']} [{later['server']}] "
                        f"({later['first'][5:16]}~{later['last'][5:16]})"
                        f" 空窗 {gap/24:.1f} 天\n"
                    )
                desc += "```\n"
                shown += 1
                if len(desc) > 1500:
                    embeds.append(
                        discord.Embed(
                            title="✈️ 全服轉服與改名掃描報告",
                            description=desc,
                            color=0xe67e22,
                        )
                    )
                    desc = ""
                if shown >= 10:
                    break

            if shown == 0:
                return await processing_msg.edit(
                    content="💤 有共用 EXP，但沒有符合「先結束再開始、非雙活躍」的因果配對。"
                )

            if desc:
                embeds.append(
                    discord.Embed(
                        title="✈️ 全服轉服與改名掃描報告",
                        description=desc,
                        color=0xe67e22,
                    )
                )

            for e in embeds:
                e.set_footer(text="※ 僅列出觀測窗不重疊且職業相容的配對。")
            await self._deliver_embeds(ctx, processing_msg, embeds)

        except sqlite3.DatabaseError as e:
            logger.error(f"DB error during transfer scan: {e}")
            try:
                await processing_msg.edit(content="❌ 掃描資料庫錯誤")
            except discord.NotFound:
                pass
        except asyncio.TimeoutError:
            try:
                await processing_msg.edit(content="❌ 掃描查詢逾時，請重試")
            except discord.NotFound:
                pass

    @commands.command(name="測試轉移警報", help="發送測試訊息以確認轉移警報頻道設定是否正確。")
    @allowed_channel("TRANSFER_ALERT_CHANNEL_ID")
    async def test_transfer_alert(self, ctx):
        channel_ids = self.TRANSFER_ALERT_CHANNEL_IDS
        if not channel_ids:
            return await ctx.send(
                "❌ 系統尚未設定 `TRANSFER_ALERT_CHANNEL_ID` 環境變數，請確認 `.env` 檔案設定。"
            )

        channels = []
        for cid in channel_ids:
            ch = self.bot.get_channel(cid)
            if ch:
                channels.append(ch)
            else:
                await ctx.send(f"⚠️ 找不到頻道 ID：`{cid}`。")

        if not channels:
            return

        now = now_naive_taipei().strftime("%Y-%m-%d %H:%M:%S")
        embed = discord.Embed(
            title="【波拉西亞戰記】轉移/旅團變動警報 (測試)",
            description=(
                f"時間：{now}\n{'-' * 30}\n"
                f"✨ [即時轉移辨識] **測試玩家_舊** (測試伺服器_舊) ➔\n"
                f"**測試玩家_新** (測試伺服器_新)\n"
                f"[狀態]: 跨服轉移並改名 | [EXP變動]: +999 億 (轉移期間偷練)\n"
                f"[屬性]: Lv.99 / 測試職業 / 討伐 99\n\n"
                f"✅ **如果您看到此訊息，表示轉移警報頻道設定與權限皆正常運作中！**"
            ),
            color=0xf1c40f,
        )

        success_count = 0
        for channel in channels:
            try:
                await channel.send(embed=embed)
                success_count += 1
            except discord.Forbidden:
                await ctx.send(f"❌ 機器人沒有權限在頻道 `{channel.id}` 發送訊息。")
            except discord.HTTPException as e:
                await ctx.send(f"❌ 在頻道 `{channel.id}` 發送警報時發生錯誤：{e}")

        if success_count > 0:
            await ctx.send(f"✅ 測試轉移警報已成功發送到 {success_count} 個頻道！")


async def setup(bot):
    await bot.add_cog(PlayerSearch(bot))
