"""天眼尋人資料庫查詢與無縫接軌候選（Discord 無關）。"""
from __future__ import annotations

from services import player_matching as match
from services.text_display import escape_like

_PROFILE_SELECT = match.PROFILE_SELECT
_PROFILE_FROM = match.PROFILE_FROM
_PROFILE_SELECT_AGG = match.PROFILE_SELECT_AGG
_PROFILE_FROM_AGG = match.PROFILE_FROM_AGG
_PROFILE_STATS_SELECT = match.PROFILE_STATS_SELECT
_PROFILE_CTE = match.PROFILE_CTE


def normalize_profile_rows(result) -> list[tuple]:
    """_fetch_name_profiles 可能回傳 list、單列 tuple 或 None。"""
    if result is None:
        return []
    if isinstance(result, list):
        return result
    return [result]


class PlayerSearchStore:
    def __init__(self, db):
        self.db = db
        self._denorm_ready: bool | None = None

    async def _has_denorm_stats(self) -> bool:
        """player_profile 是否已回填統計（抽樣檢查）。"""
        if self._denorm_ready is not None:
            return self._denorm_ready
        try:
            async with self.db.execute(
                """
                SELECT 1 FROM player_profile
                WHERE min_exp IS NOT NULL AND first_seen IS NOT NULL
                LIMIT 1
                """
            ) as cursor:
                row = await cursor.fetchone()
            self._denorm_ready = row is not None
        except Exception:
            self._denorm_ready = False
        return self._denorm_ready

    async def _fetch_name_profiles(self, player_name, server_name=None):
        """依玩家名取履歷；可選 server_name 則回傳單筆（或 None）。"""
        use_denorm = await self._has_denorm_stats()
        if use_denorm:
            if server_name is None:
                sql = f"""
                    SELECT {_PROFILE_SELECT}
                    {_PROFILE_FROM}
                    WHERE pp.player_name = ?
                      AND pp.min_exp IS NOT NULL
                      AND pp.first_seen IS NOT NULL
                """
                async with self.db.execute(sql, (player_name,)) as cursor:
                    rows = await cursor.fetchall()
                if rows:
                    return rows
            else:
                sql = f"""
                    SELECT {_PROFILE_SELECT}
                    {_PROFILE_FROM}
                    WHERE pp.player_name = ? AND pp.server_name = ?
                      AND pp.min_exp IS NOT NULL
                      AND pp.first_seen IS NOT NULL
                """
                async with self.db.execute(sql, (player_name, server_name)) as cursor:
                    row = await cursor.fetchone()
                if row:
                    return row

        # 回退：exp_history 聚合
        if server_name is None:
            sql = f"""
                SELECT {_PROFILE_SELECT_AGG}
                {_PROFILE_FROM_AGG}
                WHERE e.player_name = ?
                GROUP BY e.player_name, e.server_name
            """
            async with self.db.execute(sql, (player_name,)) as cursor:
                return await cursor.fetchall()
        sql = f"""
            SELECT {_PROFILE_SELECT_AGG}
            {_PROFILE_FROM_AGG}
            WHERE e.player_name = ? AND e.server_name = ?
            GROUP BY e.player_name, e.server_name
        """
        async with self.db.execute(sql, (player_name, server_name)) as cursor:
            return await cursor.fetchone()

    async def _fetch_single_profile(self, player_name, server_name):
        return await self._fetch_name_profiles(player_name, server_name)

    async def _fetch_profiles_by_names(self, names):
        name_list = list(names)
        if not name_list:
            return []
        placeholders = ",".join("?" for _ in name_list)
        use_denorm = await self._has_denorm_stats()
        if use_denorm:
            sql = f'''
                SELECT {_PROFILE_SELECT}
                {_PROFILE_FROM}
                WHERE pp.player_name IN ({placeholders})
                  AND pp.min_exp IS NOT NULL
                  AND pp.first_seen IS NOT NULL
            '''
            async with self.db.execute(sql, tuple(name_list)) as cursor:
                rows = await cursor.fetchall()
            if rows:
                return rows
        sql = f'''
            SELECT {_PROFILE_SELECT_AGG}
            {_PROFILE_FROM_AGG}
            WHERE e.player_name IN ({placeholders})
            GROUP BY e.player_name, e.server_name
        '''
        async with self.db.execute(sql, tuple(name_list)) as cursor:
            return await cursor.fetchall()

    @staticmethod
    def _escape_like(value: str) -> str:
        """跳脫 LIKE 萬用字元 % / _ 與跳脫符本身。"""
        return escape_like(value)

    async def _get_related_names(self, target_name):
        """定向查詢別名群組，避免全表掃描 member_registry。"""
        names = {target_name}
        like = f"%{self._escape_like(target_name)}%"
        async with self.db.execute(
            '''
            SELECT player_name, original_identity FROM member_registry
            WHERE player_name = ?
               OR original_identity = ?
               OR original_identity LIKE ? ESCAPE '\\'
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
        async with self.db.execute(
            '''
            SELECT exp FROM exp_history
            WHERE player_name = ? AND server_name = ?
            GROUP BY exp
            ORDER BY MAX(record_time) DESC
            LIMIT ?
            ''',
            (player_name, server_name, limit),
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]

    async def _early_exp_anchors(self, player_name, server_name, first_seen, days=7, limit=8):
        """觀測初期 EXP 錨點（轉服／轉職當下的特徵，避免只查近期高 EXP 漏掉前身）。"""
        if not first_seen:
            return []
        async with self.db.execute(
            '''
            SELECT exp FROM exp_history
            WHERE player_name = ? AND server_name = ?
              AND record_time <= datetime(?, ?)
            GROUP BY exp
            ORDER BY MIN(record_time) ASC
            LIMIT ?
            ''',
            (player_name, server_name, first_seen, f'+{int(days)} days', limit),
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]

    async def _early_window_min_exp(self, player_name, server_name, first_seen, days=7):
        """觀測前 N 天內的最小 EXP（避免終生 MIN 撞早期路人）。"""
        async with self.db.execute(
            '''
            SELECT MIN(exp) FROM exp_history
            WHERE player_name = ? AND server_name = ?
              AND record_time <= datetime(?, ?)
            ''',
            (player_name, server_name, first_seen, f'+{int(days)} days'),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else None

    @staticmethod
    def profile_tuple_from_row(
        name, server, lvl, first, last, min_exp, max_exp, cls, sub
    ):
        """組成與 fetch profile 相同的 9 元組，供 BFS 入隊重用。"""
        return (name, server, lvl, first, last, min_exp, max_exp, cls, sub)

    async def _find_seamless_candidates(self, profile, exp_margin=None, window_days=30, limit=8):
        """無縫接軌候選。

        優先用 player_profile denorm 統計過濾（正確：完整履歷 min/max，非窗內切片）。
        denorm 未回填時回退 PROFILE_CTE。
        margin 採分層：先窄後寬，出現 high 即停。
        """
        t_name, t_server, t_lvl, t_first, t_last, t_min_exp, t_max_exp, t_cls, t_sub = profile
        unknown_cls = match.is_unknown_class(t_cls)

        early_min = await self._early_window_min_exp(t_name, t_server, t_first, days=7)
        if early_min is not None:
            t_min_exp = early_min

        if exp_margin is None:
            tiers = list(match.EXP_MARGIN_TIERS)
        else:
            tiers = [m for m in match.EXP_MARGIN_TIERS if m <= float(exp_margin)]
            if not tiers or tiers[-1] < float(exp_margin):
                tiers.append(float(exp_margin))

        use_denorm = await self._has_denorm_stats()
        merged: list[dict] = []
        for margin in tiers:
            batch = await self._seamless_one_margin(
                profile=(
                    t_name, t_server, t_lvl, t_first, t_last,
                    t_min_exp, t_max_exp, t_cls, t_sub,
                ),
                exp_margin=margin,
                window_days=window_days,
                limit=limit,
                unknown_cls=unknown_cls,
                use_denorm=use_denorm,
            )
            # 合併去重，保留較佳 score
            by_key: dict[tuple, dict] = {
                (c["name"], c["server"], c["direction"]): c for c in merged
            }
            for c in batch:
                key = (c["name"], c["server"], c["direction"])
                prev = by_key.get(key)
                if prev is None or c["score"] < prev["score"]:
                    by_key[key] = c
            merged = list(by_key.values())
            if any(c["confidence"] == "high" for c in batch):
                break

        merged.sort(key=lambda x: x["score"])
        forward = [c for c in merged if c["direction"] == "forward"][:limit]
        backward = [c for c in merged if c["direction"] == "backward"][:limit]
        return forward + backward

    async def _seamless_one_margin(
        self,
        *,
        profile,
        exp_margin,
        window_days,
        limit,
        unknown_cls,
        use_denorm,
    ):
        t_name, t_server, t_lvl, t_first, t_last, t_min_exp, t_max_exp, t_cls, t_sub = profile
        candidates: list[dict] = []

        cls_filter = "" if unknown_cls else "AND cls = ?"
        cls_params: list = [] if unknown_cls else [t_cls]

        window_fwd = f'+{int(window_days)} days'
        window_back = f'-{int(window_days)} days'

        if use_denorm:
            sql_fwd = f'''
                {_PROFILE_STATS_SELECT}
                  AND min_exp >= ? AND min_exp <= ?
                  AND first_seen >= datetime(?, ?)
                  AND first_seen <= datetime(?, ?)
                  AND lvl >= ?
                  AND (sub_grade >= ? OR sub_grade IS NULL OR ? IS NULL)
                  {{cls_filter}}
                ORDER BY (min_exp - ?) ASC
                LIMIT 40
            '''
            params_fwd_base = (
                t_name, t_server,
                t_max_exp, t_max_exp + exp_margin,
                t_last, "-1 days", t_last, window_fwd, t_lvl, t_sub, t_sub,
            )
            back_lo = max(0, t_min_exp - exp_margin)
            back_hi = t_min_exp + exp_margin
            sql_back = f'''
                {_PROFILE_STATS_SELECT}
                  AND max_exp >= ? AND max_exp <= ?
                  AND ABS(max_exp - ?) <= ?
                  AND last_seen >= datetime(?, ?)
                  AND last_seen <= datetime(?, ?)
                  AND lvl <= ?
                  AND (sub_grade <= ? OR sub_grade IS NULL OR ? IS NULL)
                  {{cls_filter}}
                ORDER BY ABS(max_exp - ?) ASC
                LIMIT 40
            '''
            params_back_base = (
                t_name, t_server,
                back_lo, back_hi,
                t_min_exp, exp_margin,
                t_first, window_back, t_first, "+1 days", t_lvl, t_sub, t_sub,
            )
        else:
            sql_fwd = f'''
                {_PROFILE_CTE}
                WHERE min_exp >= ? AND min_exp <= ?
                  AND first_seen >= datetime(?, ?)
                  AND first_seen <= datetime(?, ?)
                  AND lvl >= ?
                  AND (sub_grade >= ? OR sub_grade IS NULL OR ? IS NULL)
                  {{cls_filter}}
                ORDER BY (min_exp - ?) ASC
                LIMIT 40
            '''
            params_fwd_base = (
                t_name, t_server, t_max_exp, t_max_exp + exp_margin,
                t_max_exp, t_max_exp + exp_margin,
                t_last, "-1 days", t_last, window_fwd, t_lvl, t_sub, t_sub,
            )
            back_lo = max(0, t_min_exp - exp_margin)
            back_hi = t_min_exp + exp_margin
            sql_back = f'''
                {_PROFILE_CTE}
                WHERE ABS(max_exp - ?) <= ?
                  AND last_seen >= datetime(?, ?)
                  AND last_seen <= datetime(?, ?)
                  AND lvl <= ?
                  AND (sub_grade <= ? OR sub_grade IS NULL OR ? IS NULL)
                  {{cls_filter}}
                ORDER BY ABS(max_exp - ?) ASC
                LIMIT 40
            '''
            params_back_base = (
                t_name, t_server, back_lo, back_hi,
                t_min_exp, exp_margin,
                t_first, window_back, t_first, "+1 days", t_lvl, t_sub, t_sub,
            )

        async def _fetch(sql, params):
            async with self.db.execute(sql, params) as cursor:
                return await cursor.fetchall()

        # 同職優先；無 high 才開不限職（減少 CTE／profile 掃描）
        async def _run_pair(extra_cls_filter: str, extra_cls_params: list):
            fwd = await _fetch(
                sql_fwd.format(cls_filter=extra_cls_filter),
                (*params_fwd_base, *extra_cls_params, t_max_exp),
            )
            back = await _fetch(
                sql_back.format(cls_filter=extra_cls_filter),
                (*params_back_base, *extra_cls_params, t_min_exp),
            )
            return fwd, back

        fwd_rows, back_rows = await _run_pair(cls_filter, cls_params)
        self._append_fwd_cands(
            candidates, fwd_rows, t_name, t_server, t_lvl, t_last, t_max_exp, t_cls, t_sub
        )
        self._append_back_cands(
            candidates, back_rows, t_name, t_server, t_lvl, t_first, t_min_exp, t_cls, t_sub
        )

        has_high = any(c["confidence"] == "high" for c in candidates)
        if not has_high and not unknown_cls:
            fwd2, back2 = await _run_pair("", [])
            self._append_fwd_cands(
                candidates, fwd2, t_name, t_server, t_lvl, t_last, t_max_exp, t_cls, t_sub
            )
            self._append_back_cands(
                candidates, back2, t_name, t_server, t_lvl, t_first, t_min_exp, t_cls, t_sub
            )

        has_fwd_high = any(
            c["direction"] == "forward" and c["confidence"] == "high" for c in candidates
        )
        if not has_fwd_high:
            near_margin = min(1e8, exp_margin)
            if use_denorm:
                sql_near = f'''
                    {_PROFILE_STATS_SELECT}
                      AND min_exp >= ? AND min_exp <= ?
                      AND first_seen >= datetime(?, ?)
                      AND first_seen <= datetime(?, ?)
                      AND (sub_grade >= ? OR sub_grade IS NULL OR ? IS NULL)
                      {cls_filter}
                    ORDER BY (min_exp - ?) ASC
                    LIMIT 15
                '''
                near_params = (
                    t_name, t_server, t_max_exp, t_max_exp + near_margin,
                    t_last, "-1 days", t_last, "+7 days", t_sub, t_sub,
                    *cls_params, t_max_exp,
                )
            else:
                sql_near = f'''
                    {_PROFILE_CTE}
                    WHERE min_exp >= ? AND min_exp <= ?
                      AND first_seen >= datetime(?, ?)
                      AND first_seen <= datetime(?, ?)
                      AND (sub_grade >= ? OR sub_grade IS NULL OR ? IS NULL)
                      {cls_filter}
                    ORDER BY (min_exp - ?) ASC
                    LIMIT 15
                '''
                near_params = (
                    t_name, t_server, t_max_exp, t_max_exp + near_margin,
                    t_max_exp, t_max_exp + near_margin,
                    t_last, "-1 days", t_last, "+7 days", t_sub, t_sub,
                    *cls_params, t_max_exp,
                )
            near_rows = await _fetch(sql_near, near_params)
            existing = {(c["name"], c["server"]) for c in candidates}
            for row in near_rows:
                c_name, c_server, c_lvl, c_cls, c_first, c_last, c_min, c_max, c_sub = row
                if (c_name, c_server) in existing:
                    continue
                exp_diff = c_min - t_max_exp
                if exp_diff < 0:
                    continue
                gap_hours = match.gap_hours(t_last, c_first)
                same_server = c_server == t_server
                if match.confidence(
                    t_cls, t_sub, c_cls, exp_diff, c_sub, gap_hours, same_server,
                    a_last=t_last, b_first=c_first,
                ) != "high":
                    continue
                class_ok = match.class_compatible(t_cls, c_cls)
                label = (
                    "✈️ 疑似轉服/改名後" if class_ok else "🔄 疑似轉服+轉職"
                )
                candidates.append({
                    "direction": "forward",
                    "name": c_name, "server": c_server, "lvl": c_lvl, "cls": c_cls,
                    "first": c_first, "last": c_last, "exp_val": c_min, "sub_grade": c_sub,
                    "min_exp": c_min, "max_exp": c_max,
                    "match_type": label,
                    "diff_text": f"空窗偷練 +{exp_diff/100000000:,.0f} 億",
                    "exp_diff": exp_diff,
                    "score": exp_diff + gap_hours * 1e8 - 1e12,
                    "confidence": "high",
                })

        candidates.sort(key=lambda x: x["score"])
        forward = [c for c in candidates if c["direction"] == "forward"][:limit]
        backward = [c for c in candidates if c["direction"] == "backward"][:limit]
        return forward + backward

    @staticmethod
    def _append_fwd_cands(
        candidates, fwd_rows, t_name, t_server, t_lvl, t_last, t_max_exp, t_cls, t_sub
    ):
        seen_fwd = {(c["name"], c["server"]) for c in candidates if c["direction"] == "forward"}
        for row in fwd_rows:
            c_name, c_server, c_lvl, c_cls, c_first, c_last, c_min, c_max, c_sub = row
            key = (c_name, c_server)
            if key in seen_fwd:
                continue
            seen_fwd.add(key)
            exp_diff = c_min - t_max_exp
            if exp_diff < 0:
                continue
            same_server = c_server == t_server
            gap_hours = match.gap_hours(t_last, c_first)
            class_ok = match.class_compatible(t_cls, c_cls)
            if same_server:
                label = "✏️ 疑似同服改名" if class_ok else "🔄 疑似同服轉職"
            else:
                label = "✈️ 疑似轉服/改名後" if class_ok else "🔄 疑似轉服+轉職"
            candidates.append({
                "direction": "forward",
                "name": c_name, "server": c_server, "lvl": c_lvl, "cls": c_cls,
                "first": c_first, "last": c_last, "exp_val": c_min, "sub_grade": c_sub,
                "min_exp": c_min, "max_exp": c_max,
                "match_type": label,
                "diff_text": f"空窗偷練 +{exp_diff/100000000:,.0f} 億",
                "exp_diff": exp_diff,
                "score": match.score(
                    t_cls, t_sub, t_lvl, exp_diff, gap_hours, c_cls, c_sub, c_lvl,
                    same_server, forward=True,
                ),
                "confidence": match.confidence(
                    t_cls, t_sub, c_cls, exp_diff, c_sub, gap_hours, same_server,
                    a_last=t_last, b_first=c_first,
                ),
            })

    @staticmethod
    def _append_back_cands(
        candidates, back_rows, t_name, t_server, t_lvl, t_first, t_min_exp, t_cls, t_sub
    ):
        seen_back = {(c["name"], c["server"]) for c in candidates if c["direction"] == "backward"}
        for row in back_rows:
            c_name, c_server, c_lvl, c_cls, c_first, c_last, c_min, c_max, c_sub = row
            key = (c_name, c_server)
            if key in seen_back:
                continue
            seen_back.add(key)
            raw_diff = t_min_exp - c_max
            exp_diff = abs(raw_diff)
            same_server = c_server == t_server
            gap_hours = match.gap_hours(t_first, c_last)
            class_ok = match.class_compatible(t_cls, c_cls)
            if raw_diff >= 0:
                diff_text = f"空窗偷練 +{raw_diff/100000000:,.0f} 億"
            else:
                diff_text = f"特徵接近 (回差 {exp_diff/100000000:,.0f} 億)"
            if same_server:
                label = "✏️ 疑似同服改名前身" if class_ok else "🔄 疑似同服轉職前身"
            else:
                label = "🔍 疑似前身" if class_ok else "🔄 疑似轉職前身"
            candidates.append({
                "direction": "backward",
                "name": c_name, "server": c_server, "lvl": c_lvl, "cls": c_cls,
                "first": c_first, "last": c_last, "exp_val": c_max, "sub_grade": c_sub,
                "min_exp": c_min, "max_exp": c_max,
                "match_type": label,
                "diff_text": diff_text,
                "exp_diff": exp_diff,
                "score": match.score(
                    t_cls, t_sub, t_lvl, exp_diff, gap_hours, c_cls, c_sub, c_lvl,
                    same_server, forward=False,
                ),
                "confidence": match.confidence(
                    t_cls, t_sub, c_cls, exp_diff, c_sub, gap_hours, same_server,
                    a_last=c_last, b_first=t_first,
                ),
            })
