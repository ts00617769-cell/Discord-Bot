"""天眼尋人 / 轉服掃描指令（自 exp_tracker 拆出）。"""
import asyncio
import datetime
import logging
import sqlite3
import traceback
from collections import deque

import discord
from discord.ext import commands

from .error_handler import require_allowed_channel, parse_env_channel_ids

logger = logging.getLogger(__name__)

# 履歷聚合 SELECT（fetch profile 用）
_PROFILE_SELECT = """
    e.player_name, e.server_name,
    MAX(e.level), MIN(e.record_time), MAX(e.record_time),
    MIN(e.exp), MAX(e.exp),
    (SELECT e2.class_name FROM exp_history e2
     WHERE e2.player_name = e.player_name AND e2.server_name = e.server_name
     ORDER BY e2.record_time DESC LIMIT 1) AS class_name,
    MAX(e.subjugation_grade)
"""

# 無縫查詢共用 CTE：hit 粗篩 + prof 完整履歷聚合；fwd/back/near 只差 WHERE
_PROFILE_CTE = """
    WITH hit AS (
        SELECT DISTINCT player_name, server_name
        FROM exp_history
        WHERE NOT (player_name = ? AND server_name = ?)
          AND exp BETWEEN ? AND ?
    ),
    prof AS (
        SELECT e.player_name, e.server_name,
               MAX(e.level) AS lvl,
               MIN(e.record_time) AS first_seen,
               MAX(e.record_time) AS last_seen,
               MIN(e.exp) AS min_exp,
               MAX(e.exp) AS max_exp,
               MAX(e.subjugation_grade) AS sub_grade,
               (SELECT e2.class_name FROM exp_history e2
                WHERE e2.player_name = e.player_name
                  AND e2.server_name = e.server_name
                ORDER BY e2.record_time DESC LIMIT 1) AS cls
        FROM exp_history e
        INNER JOIN hit
          ON hit.player_name = e.player_name
         AND hit.server_name = e.server_name
        GROUP BY e.player_name, e.server_name
    )
    SELECT player_name, server_name, lvl, cls,
           first_seen, last_seen, min_exp, max_exp, sub_grade
    FROM prof
"""


class PlayerSearch(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def TRANSFER_ALERT_CHANNEL_IDS(self):
        return parse_env_channel_ids(env_name="TRANSFER_ALERT_CHANNEL_ID")

    def _is_unknown_class(self, cls_name):
        return cls_name in (None, "", "None", "未知")

    @staticmethod
    def _gap_hours(anchor_str, point_str):
        try:
            fmt = "%Y-%m-%d %H:%M:%S"
            return abs(
                (
                    datetime.datetime.strptime(point_str, fmt)
                    - datetime.datetime.strptime(anchor_str, fmt)
                ).total_seconds()
            ) / 3600
        except (TypeError, ValueError):
            return 9999.0

    @staticmethod
    def _observation_gap_hours(a_first, a_last, b_first, b_last):
        """兩段觀測區間若不重疊，回傳最近端點的小時差；重疊則 0。"""
        try:
            fmt = "%Y-%m-%d %H:%M:%S"
            af = datetime.datetime.strptime(a_first, fmt)
            al = datetime.datetime.strptime(a_last, fmt)
            bf = datetime.datetime.strptime(b_first, fmt)
            bl = datetime.datetime.strptime(b_last, fmt)
        except (TypeError, ValueError):
            return 9999.0
        if al < bf:
            return (bf - al).total_seconds() / 3600
        if bl < af:
            return (af - bl).total_seconds() / 3600
        return 0.0

    def _pick_soft_candidates(
        self, soft_candidates, exclude_keys, per_direction=2, max_diff_over_best=2e10,
    ):
        """每方向最多 per_direction 名；同方向經驗差遠大於最佳則淘汰。"""
        soft_unique = []
        soft_seen = set(exclude_keys)
        for direction in ("forward", "backward"):
            dir_cands = sorted(
                (c for c in soft_candidates if c.get("direction") == direction),
                key=lambda x: x["score"],
            )
            # 同 (name, server, direction) 只留最佳 score
            best_by_key = {}
            for cand in dir_cands:
                key = (cand["name"], cand["server"], direction)
                if key not in best_by_key:
                    best_by_key[key] = cand
            dir_cands = sorted(best_by_key.values(), key=lambda x: x["score"])

            picked = []
            for cand in dir_cands:
                key = (cand["name"], cand["server"])
                if key in soft_seen:
                    continue
                if picked:
                    best_diff = picked[0].get("exp_diff", 0)
                    cand_diff = cand.get("exp_diff", best_diff)
                    if cand_diff > best_diff + max_diff_over_best:
                        continue
                soft_seen.add(key)
                picked.append(cand)
                if len(picked) >= per_direction:
                    break
            soft_unique.extend(picked)
        soft_unique.sort(key=lambda x: x["score"])
        return soft_unique

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
        self, target_name, entries, *, header, title, color, footer, show_confidence=False,
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

    def _confidence(self, t_cls, t_sub, c_cls, exp_diff, c_sub, gap_hours, same_server):
        unknown_cls = self._is_unknown_class(t_cls)
        sub_ok = c_sub is None or t_sub is None or c_sub == t_sub
        if exp_diff <= 1e8 and gap_hours <= 72 and sub_ok:
            return "high"
        if same_server:
            if (
                not unknown_cls
                and c_cls == t_cls
                and exp_diff < 1e9
                and gap_hours <= 24
                and sub_ok
            ):
                return "high"
            return "medium"
        if (
            not unknown_cls
            and c_cls == t_cls
            and exp_diff < 5e9
            and gap_hours <= 48
            and sub_ok
        ):
            return "high"
        return "medium"

    def _score(
        self, t_cls, t_sub, t_lvl, exp_diff, gap_hours, c_cls, c_sub, c_lvl, same_server,
        forward=True,
    ):
        score = exp_diff + gap_hours * 1e8
        if not self._is_unknown_class(t_cls) and c_cls == t_cls:
            score -= 5e11
        if c_sub == t_sub:
            score -= 1e11
        elif (
            c_sub is not None
            and t_sub is not None
            and abs(c_sub - t_sub) <= 1
        ):
            score -= 5e10
        if forward:
            if c_lvl == t_lvl:
                score -= 5e10
            if same_server:
                score -= 2e10
        else:
            if c_lvl == t_lvl or c_lvl == t_lvl - 1:
                score -= 5e10
        if exp_diff <= 1e8:
            score -= 1e12
        return score

    async def _fetch_name_profiles(self, player_name, server_name=None):
        """依玩家名取履歷；可選 server_name 則回傳單筆（或 None）。"""
        if server_name is None:
            sql = f'''
                SELECT {_PROFILE_SELECT}
                FROM exp_history e
                WHERE e.player_name = ?
                GROUP BY e.player_name, e.server_name
            '''
            async with self.bot.db.execute(sql, (player_name,)) as cursor:
                return await cursor.fetchall()
        sql = f'''
            SELECT {_PROFILE_SELECT}
            FROM exp_history e
            WHERE e.player_name = ? AND e.server_name = ?
            GROUP BY e.player_name, e.server_name
        '''
        async with self.bot.db.execute(sql, (player_name, server_name)) as cursor:
            return await cursor.fetchone()

    async def _fetch_single_profile(self, player_name, server_name):
        return await self._fetch_name_profiles(player_name, server_name)

    async def _fetch_profiles_by_names(self, names):
        name_list = list(names)
        if not name_list:
            return []
        placeholders = ",".join("?" for _ in name_list)
        sql = f'''
            SELECT {_PROFILE_SELECT}
            FROM exp_history e
            WHERE e.player_name IN ({placeholders})
            GROUP BY e.player_name, e.server_name
        '''
        async with self.bot.db.execute(sql, tuple(name_list)) as cursor:
            return await cursor.fetchall()

    async def _get_related_names(self, target_name):
        """定向查詢別名群組，避免全表掃描 member_registry。"""
        names = {target_name}
        like = f"%{target_name}%"
        async with self.bot.db.execute(
            '''
            SELECT player_name, original_identity FROM member_registry
            WHERE player_name = ?
               OR original_identity = ?
               OR original_identity LIKE ?
            ''',
            (target_name, target_name, like),
        ) as cursor:
            rows = await cursor.fetchall()
        for player_name, identity in rows:
            aliases = [x.strip() for x in (identity or "").split(",") if x.strip()]
            group = {player_name, *aliases}
            if target_name in group:
                names.update(group)
        return names

    async def _recent_exp_anchors(self, player_name, server_name, limit=8):
        async with self.bot.db.execute(
            '''
            SELECT DISTINCT exp FROM exp_history
            WHERE player_name = ? AND server_name = ?
            ORDER BY record_time DESC
            LIMIT ?
            ''',
            (player_name, server_name, limit),
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]

    async def _find_seamless_candidates(self, profile, exp_margin, window_days=30, limit=8):
        """無縫接軌候選。

        重要：不可用 WHERE exp BETWEEN … GROUP BY 直接算 MIN/MAX(exp)/時間。
        那樣會變成「經驗窗內切片」，把持續練等的路人誤判成轉服/前身。
        正確做法：先用經驗窗粗篩 (name, server)，再對完整履歷聚合後過濾。
        """
        t_name, t_server, t_lvl, t_first, t_last, t_min_exp, t_max_exp, t_cls, t_sub = profile
        unknown_cls = self._is_unknown_class(t_cls)
        candidates = []

        cls_filter = "" if unknown_cls else "AND cls = ?"
        cls_params = [] if unknown_cls else [t_cls]

        # 完整履歷聚合：hit 只負責粗篩，prof 才是真實 first/last/min/max
        sql_fwd = f'''
            {_PROFILE_CTE}
            WHERE min_exp >= ? AND min_exp <= ?
              AND first_seen >= datetime(?, '-1 days')
              AND first_seen <= datetime(?, '+{int(window_days)} days')
              AND lvl >= ?
              AND (sub_grade >= ? OR sub_grade IS NULL OR ? IS NULL)
              {cls_filter}
            ORDER BY (min_exp - ?) ASC
            LIMIT 40
        '''
        params_fwd = (
            t_name, t_server, t_max_exp, t_max_exp + exp_margin,
            t_max_exp, t_max_exp + exp_margin,
            t_last, t_last, t_lvl, t_sub, t_sub,
            *cls_params, t_max_exp,
        )

        # 前身：允許 max_exp 略高於 t_min（榜單取樣誤差），用絕對差 <= margin
        back_lo = max(0, t_min_exp - exp_margin)
        back_hi = t_min_exp + exp_margin
        sql_back = f'''
            {_PROFILE_CTE}
            WHERE ABS(max_exp - ?) <= ?
              AND last_seen >= datetime(?, '-{int(window_days)} days')
              AND last_seen <= datetime(?, '+1 days')
              AND lvl <= ?
              AND (sub_grade <= ? OR sub_grade IS NULL OR ? IS NULL)
              {cls_filter}
            ORDER BY ABS(max_exp - ?) ASC
            LIMIT 40
        '''
        params_back = (
            t_name, t_server, back_lo, back_hi,
            t_min_exp, exp_margin,
            t_first, t_first, t_lvl, t_sub, t_sub,
            *cls_params, t_min_exp,
        )

        async def _fetch(sql, params):
            async with self.bot.db.execute(sql, params) as cursor:
                return await cursor.fetchall()

        fwd_rows, back_rows = await asyncio.gather(
            _fetch(sql_fwd, params_fwd),
            _fetch(sql_back, params_back),
        )

        for row in fwd_rows:
            c_name, c_server, c_lvl, c_cls, c_first, c_last, c_min, c_max, c_sub = row
            exp_diff = c_min - t_max_exp
            if exp_diff < 0:
                continue
            same_server = c_server == t_server
            gap_hours = self._gap_hours(t_last, c_first)
            label = "✏️ 疑似同服改名" if same_server else "✈️ 疑似轉服/改名後"
            candidates.append({
                "direction": "forward",
                "name": c_name, "server": c_server, "lvl": c_lvl, "cls": c_cls,
                "first": c_first, "last": c_last, "exp_val": c_min, "sub_grade": c_sub,
                "match_type": label,
                "diff_text": f"空窗偷練 +{exp_diff/100000000:,.0f} 億",
                "exp_diff": exp_diff,
                "score": self._score(
                    t_cls, t_sub, t_lvl, exp_diff, gap_hours, c_cls, c_sub, c_lvl,
                    same_server, forward=True,
                ),
                "confidence": self._confidence(
                    t_cls, t_sub, c_cls, exp_diff, c_sub, gap_hours, same_server,
                ),
            })

        for row in back_rows:
            c_name, c_server, c_lvl, c_cls, c_first, c_last, c_min, c_max, c_sub = row
            raw_diff = t_min_exp - c_max
            exp_diff = abs(raw_diff)
            same_server = c_server == t_server
            gap_hours = self._gap_hours(t_first, c_last)
            if raw_diff >= 0:
                diff_text = f"空窗偷練 +{raw_diff/100000000:,.0f} 億"
            else:
                diff_text = f"特徵接近 (回差 {exp_diff/100000000:,.0f} 億)"
            label = "✏️ 疑似同服改名前身" if same_server else "🔍 疑似前身"
            candidates.append({
                "direction": "backward",
                "name": c_name, "server": c_server, "lvl": c_lvl, "cls": c_cls,
                "first": c_first, "last": c_last, "exp_val": c_max, "sub_grade": c_sub,
                "match_type": label,
                "diff_text": diff_text,
                "exp_diff": exp_diff,
                "score": self._score(
                    t_cls, t_sub, t_lvl, exp_diff, gap_hours, c_cls, c_sub, c_lvl,
                    same_server, forward=False,
                ),
                "confidence": self._confidence(
                    t_cls, t_sub, c_cls, exp_diff, c_sub, gap_hours, same_server,
                ),
            })

        # near 救援：僅在 forward 無任何 high 時才跑，避免每 hop 多一次重 CTE
        has_fwd_high = any(
            c["direction"] == "forward" and c["confidence"] == "high" for c in candidates
        )
        if not has_fwd_high:
            near_margin = 1e8
            sql_near = f'''
                {_PROFILE_CTE}
                WHERE min_exp >= ? AND min_exp <= ?
                  AND first_seen >= datetime(?, '-1 days')
                  AND first_seen <= datetime(?, '+7 days')
                  AND (sub_grade >= ? OR sub_grade IS NULL OR ? IS NULL)
                  {cls_filter}
                ORDER BY (min_exp - ?) ASC
                LIMIT 15
            '''
            near_rows = await _fetch(
                sql_near,
                (
                    t_name, t_server, t_max_exp, t_max_exp + near_margin,
                    t_max_exp, t_max_exp + near_margin,
                    t_last, t_last, t_sub, t_sub,
                    *cls_params, t_max_exp,
                ),
            )
            existing = {(c["name"], c["server"]) for c in candidates}
            for row in near_rows:
                c_name, c_server, c_lvl, c_cls, c_first, c_last, c_min, c_max, c_sub = row
                if (c_name, c_server) in existing:
                    continue
                exp_diff = c_min - t_max_exp
                if exp_diff < 0:
                    continue
                gap_hours = self._gap_hours(t_last, c_first)
                same_server = c_server == t_server
                if self._confidence(
                    t_cls, t_sub, c_cls, exp_diff, c_sub, gap_hours, same_server,
                ) != "high":
                    continue
                candidates.append({
                    "direction": "forward",
                    "name": c_name, "server": c_server, "lvl": c_lvl, "cls": c_cls,
                    "first": c_first, "last": c_last, "exp_val": c_min, "sub_grade": c_sub,
                    "match_type": "✈️ 疑似轉服/改名後",
                    "diff_text": f"空窗偷練 +{exp_diff/100000000:,.0f} 億",
                    "exp_diff": exp_diff,
                    "score": exp_diff + gap_hours * 1e8 - 1e12,
                    "confidence": "high",
                })

        candidates.sort(key=lambda x: x["score"])
        forward = [c for c in candidates if c["direction"] == "forward"][:limit]
        backward = [c for c in candidates if c["direction"] == "backward"][:limit]
        return forward + backward

    @commands.command(
        name="尋人回報",
        help="手動標記玩家前身身分。用法: !尋人回報 驕傲o 某某某 艾雲o 或 !尋人回報 驕傲o 清除",
    )
    async def report_identity(self, ctx, *args):
        if not await require_allowed_channel(ctx):
            return
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

    @commands.command(name="尋人", help="利用經驗值特徵，精準追蹤改名或轉服的玩家。用法: !尋人 驕傲o")
    @commands.cooldown(1, 15, commands.BucketType.user)
    @commands.max_concurrency(2, commands.BucketType.user, wait=False)
    async def track_player(self, ctx, *, target_name: str):
        target_name = (target_name or "").strip()
        if not target_name:
            await ctx.send("❌ 請輸入玩家名稱。用法：`!尋人 小碎冰`")
            return
        if not await require_allowed_channel(ctx):
            return

        logger.info(
            f"!尋人 start user={ctx.author.id} channel={ctx.channel.id} "
            f"parent={getattr(ctx.channel, 'parent_id', None)} name={target_name!r}"
        )
        try:
            processing_msg = await ctx.send(
                f"🔍 啟動天眼雙引擎，正在掃描「{target_name}」..."
            )
        except discord.HTTPException as e:
            logger.error(f"!尋人 無法在頻道 {ctx.channel.id} 發送訊息: {e}")
            return

        try:
            related_names = await self._get_related_names(target_name)
            target_profiles = await self._fetch_profiles_by_names(related_names)

            if not target_profiles:
                return await processing_msg.edit(
                    content=f"❌ 天眼系統找不到「{target_name}」的任何歷史紀錄。"
                )

            EXP_MARGIN = 1.0 * 1000000000000
            timeline_entries = []
            soft_candidates = []
            seen_profiles = set()
            queue = deque()

            async def add_to_queue(
                p_name, p_server, m_type, d_text, e_val, profile=None, confidence="high"
            ):
                if (p_name, p_server) in seen_profiles:
                    return
                seen_profiles.add((p_name, p_server))
                if profile is None:
                    profile = await self._fetch_single_profile(p_name, p_server)
                if not profile:
                    return
                queue.append({
                    "profile": profile,
                    "match_type": m_type,
                    "diff_text": d_text,
                    "exp_val": e_val,
                    "confidence": confidence,
                })

            for tp in target_profiles:
                label = "🎯 查詢目標" if tp[0] == target_name else "🏷️ 登錄別名"
                await add_to_queue(tp[0], tp[1], label, "", tp[6], profile=tp)

            bfs_limit = 30
            hops = 0

            while queue and hops < bfs_limit:
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

                anchors = await self._recent_exp_anchors(t_name, t_server, limit=8)
                if t_min_exp not in anchors:
                    anchors.append(t_min_exp)
                if t_max_exp not in anchors:
                    anchors.append(t_max_exp)

                if anchors:
                    placeholders = ",".join("?" for _ in anchors)
                    sql_exact = f'''
                        SELECT exp, player_name, server_name, MAX(level),
                               (SELECT e2.class_name FROM exp_history e2
                                WHERE e2.player_name = exp_history.player_name
                                  AND e2.server_name = exp_history.server_name
                                ORDER BY e2.record_time DESC LIMIT 1),
                               MIN(record_time), MAX(record_time), MAX(subjugation_grade)
                        FROM exp_history
                        WHERE exp IN ({placeholders})
                          AND NOT (player_name = ? AND server_name = ?)
                        GROUP BY exp, player_name, server_name
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
                        obs_gap = self._observation_gap_hours(
                            t_first, t_last, first_seen, last_seen,
                        )
                        if obs_gap > 30 * 24:
                            soft_candidates.append({
                                "direction": "forward" if first_seen >= t_last else "backward",
                                "name": p_name, "server": s_name, "lvl": lvl, "cls": cls_name,
                                "first": first_seen, "last": last_seen, "exp_val": exp,
                                "sub_grade": sub_grade,
                                "match_type": "🔗 絕對經驗值碰撞（時間過遠）",
                                "diff_text": f"EXP 完全一致（觀測間隔 {obs_gap/24:.0f} 天）",
                                "exp_diff": 0,
                                "score": obs_gap * 1e8,
                                "confidence": "medium",
                            })
                            continue
                        await add_to_queue(
                            p_name, s_name, "🔗 絕對經驗值碰撞", "EXP 完全一致", exp, confidence="high"
                        )

                seamless = await self._find_seamless_candidates(
                    profile, EXP_MARGIN, window_days=30, limit=5
                )
                for cand in seamless:
                    soft_candidates.append(cand)
                    if cand["confidence"] == "high":
                        await add_to_queue(
                            cand["name"], cand["server"], cand["match_type"],
                            cand["diff_text"], cand["exp_val"], confidence="high",
                        )
                hops += 1

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
                soft_unique = self._pick_soft_candidates(soft_candidates, seen)

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
                    await processing_msg.delete()
                    for embed in embeds:
                        await ctx.send(embed=embed)
                    return

                if only_self:
                    tip = ""
                    if any(p[0] == target_name for p in target_profiles):
                        tip = "\n（若目標仍持續出現在原服榜上，可能尚未轉服/改名。）"
                    return await processing_msg.edit(
                        content=(
                            f"⚠️ 目標最後紀錄為 {target_last_exp/1000000000000:.2f} 兆。\n"
                            f"雙引擎未找到符合條件的轉服/改名軌跡。{tip}\n"
                            f"提示：可用 `!尋人回報 {target_name} 前身名` 手動標記後再查。"
                        )
                    )

            embeds = self._build_track_embeds(
                target_name,
                unique_entries,
                header=f"🚨 **啟動雙引擎掃描，成功捕捉「{target_name}」的軌跡！**\n\n",
                title=f"👁️ 天眼追蹤系統 (V5.1) - {target_name}",
                color=0xff0000,
                footer="V5.1：主軌僅高信心・絕對碰撞可跨職業・medium 不混入",
            )
            await processing_msg.delete()
            for embed in embeds:
                await ctx.send(embed=embed)

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

    @commands.command(
        name="轉服掃描",
        aliases=["移民清單", "抓包"],
        help="全服掃描近期利用轉服空窗期改名或移動的玩家",
    )
    @commands.cooldown(1, 30, commands.BucketType.guild)
    @commands.max_concurrency(1, commands.BucketType.default, wait=False)
    async def global_transfer_scan(self, ctx):
        if not await require_allowed_channel(ctx):
            return
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
                LIMIT 10
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
                SELECT exp, player_name, server_name, MIN(record_time), MAX(record_time)
                FROM exp_history
                WHERE exp IN ({placeholders})
                GROUP BY exp, player_name, server_name
                ORDER BY exp DESC, MIN(record_time) ASC
                ''',
                tuple(exp_list),
            ) as cursor:
                records = await cursor.fetchall()

            grouped_data = {}
            for exp, p_name, s_name, first_seen, last_seen in records:
                grouped_data.setdefault(exp, []).append(
                    {"name": p_name, "server": s_name, "first": first_seen, "last": last_seen}
                )

            embeds = []
            desc = "🔍 **以下玩家被系統偵測到經驗值完全重疊：**\n\n"
            for exp, players in grouped_data.items():
                exp_zhao = exp / 1_000_000_000_000
                desc += f"🔗 **特徵碼：{exp_zhao:.3f} 兆**\n```yaml\n"
                for idx, p in enumerate(players, 1):
                    desc += f"{idx}. {p['name']} [{p['server']}]\n"
                    desc += f"   (觀測區間: {p['first'][5:16]} ~ {p['last'][5:16]})\n"
                desc += "```\n"
                if len(desc) > 1500:
                    embeds.append(
                        discord.Embed(
                            title="✈️ 全服轉服與改名掃描報告",
                            description=desc,
                            color=0xe67e22,
                        )
                    )
                    desc = ""

            if desc:
                embeds.append(
                    discord.Embed(
                        title="✈️ 全服轉服與改名掃描報告",
                        description=desc,
                        color=0xe67e22,
                    )
                )

            await processing_msg.delete()
            for e in embeds:
                e.set_footer(text="※ 原理：轉服期間經驗值會凍結，利用相同特徵追蹤移動軌跡。")
                await ctx.send(embed=e)

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
    async def test_transfer_alert(self, ctx):
        if not await require_allowed_channel(ctx):
            return
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

        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
