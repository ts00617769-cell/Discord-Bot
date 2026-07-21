"""尋人／轉服匹配的純邏輯（不含 Discord / DB I/O）。"""
from __future__ import annotations

import datetime
from typing import Any, Iterable, Optional

# 履歷聚合 SELECT（fetch profile 用；職業來自 player_profile，避免相關子查詢）
PROFILE_SELECT = """
    e.player_name, e.server_name,
    MAX(e.level), MIN(e.record_time), MAX(e.record_time),
    MIN(e.exp), MAX(e.exp),
    COALESCE(MAX(pp.class_name), '未知') AS class_name,
    MAX(e.subjugation_grade)
"""

PROFILE_FROM = """
    FROM exp_history e
    LEFT JOIN player_profile pp
      ON pp.player_name = e.player_name
     AND pp.server_name = e.server_name
"""

# 無縫查詢共用 CTE：hit 粗篩 + prof 完整履歷聚合；fwd/back/near 只差 WHERE
PROFILE_CTE = """
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
               COALESCE(MAX(pp.class_name), '未知') AS cls
        FROM exp_history e
        INNER JOIN hit
          ON hit.player_name = e.player_name
         AND hit.server_name = e.server_name
        LEFT JOIN player_profile pp
          ON pp.player_name = e.player_name
         AND pp.server_name = e.server_name
        GROUP BY e.player_name, e.server_name
    )
    SELECT player_name, server_name, lvl, cls,
           first_seen, last_seen, min_exp, max_exp, sub_grade
    FROM prof
"""

def is_unknown_class(cls_name: Optional[str]) -> bool:
    return cls_name in (None, "", "None", "未知")


def class_compatible(t_cls: Optional[str], c_cls: Optional[str]) -> bool:
    """high 信心要求職業相符，或雙方皆未知。"""
    t_unknown = is_unknown_class(t_cls)
    c_unknown = is_unknown_class(c_cls)
    if t_unknown and c_unknown:
        return True
    if t_unknown or c_unknown:
        return False
    return t_cls == c_cls


def gap_hours(anchor_str: Optional[str], point_str: Optional[str]) -> float:
    if not anchor_str or not point_str:
        return 9999.0
    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        return abs(
            (
                datetime.datetime.strptime(point_str, fmt)
                - datetime.datetime.strptime(anchor_str, fmt)
            ).total_seconds()
        ) / 3600
    except ValueError:
        return 9999.0


def observation_gap_hours(
    a_first: Optional[str],
    a_last: Optional[str],
    b_first: Optional[str],
    b_last: Optional[str],
) -> float:
    """兩段觀測區間若不重疊，回傳最近端點的小時差；重疊則 0。"""
    if not a_first or not a_last or not b_first or not b_last:
        return 9999.0
    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        af = datetime.datetime.strptime(a_first, fmt)
        al = datetime.datetime.strptime(a_last, fmt)
        bf = datetime.datetime.strptime(b_first, fmt)
        bl = datetime.datetime.strptime(b_last, fmt)
    except ValueError:
        return 9999.0
    if al < bf:
        return (bf - al).total_seconds() / 3600
    if bl < af:
        return (af - bl).total_seconds() / 3600
    return 0.0


def pick_soft_candidates(
    soft_candidates: Iterable[dict[str, Any]],
    exclude_keys: Iterable[tuple],
    per_direction: int = 2,
    max_diff_over_best: float = 2e10,
) -> list[dict[str, Any]]:
    """每方向最多 per_direction 名；同方向經驗差遠大於最佳則淘汰。"""
    soft_unique: list[dict[str, Any]] = []
    soft_seen = set(exclude_keys)
    for direction in ("forward", "backward"):
        dir_cands = sorted(
            (c for c in soft_candidates if c.get("direction") == direction),
            key=lambda x: x["score"],
        )
        best_by_key: dict[tuple, dict] = {}
        for cand in dir_cands:
            key = (cand["name"], cand["server"], direction)
            if key not in best_by_key:
                best_by_key[key] = cand
        dir_cands = sorted(best_by_key.values(), key=lambda x: x["score"])

        picked: list[dict[str, Any]] = []
        for cand in dir_cands:
            name_server = (cand["name"], cand["server"])
            if name_server in soft_seen:
                continue
            if picked:
                best_diff = picked[0].get("exp_diff", 0)
                cand_diff = cand.get("exp_diff", best_diff)
                if cand_diff > best_diff + max_diff_over_best:
                    continue
            soft_seen.add(name_server)
            picked.append(cand)
            if len(picked) >= per_direction:
                break
        soft_unique.extend(picked)
    soft_unique.sort(key=lambda x: x["score"])
    return soft_unique


def confidence(
    t_cls,
    t_sub,
    c_cls,
    exp_diff,
    c_sub,
    gap_h: float,
    same_server: bool,
    *,
    a_last: Optional[str] = None,
    b_first: Optional[str] = None,
) -> str:
    class_ok = class_compatible(t_cls, c_cls)
    sub_ok = c_sub is None or t_sub is None or c_sub == t_sub

    # 經驗極近 + 職業不符：銜接緊密／轉職窗／轉移延遲登入 → high
    if not class_ok and exp_diff <= 1e8 and sub_ok:
        from services.game_event_windows import allow_class_mismatch_high

        if allow_class_mismatch_high(
            a_last, b_first, obs_gap_hours=gap_h, exact_exp=True
        ):
            return "high"

    # 同職極近：一般 ≤72h；或領域轉移後延遲登入
    if class_ok and exp_diff <= 1e8 and sub_ok:
        if gap_h <= 72:
            return "high"
        from services.game_event_windows import allow_delayed_transfer_high

        if allow_delayed_transfer_high(
            a_last, b_first, obs_gap_hours=gap_h, exact_exp=True
        ):
            return "high"

    if same_server:
        if (
            class_ok
            and not is_unknown_class(t_cls)
            and exp_diff < 1e9
            and gap_h <= 24
            and sub_ok
        ):
            return "high"
        return "medium"
    if (
        class_ok
        and not is_unknown_class(t_cls)
        and exp_diff < 5e9
        and gap_h <= 48
        and sub_ok
    ):
        return "high"
    return "medium"


def score(
    t_cls,
    t_sub,
    t_lvl,
    exp_diff,
    gap_h: float,
    c_cls,
    c_sub,
    c_lvl,
    same_server: bool,
    *,
    forward: bool = True,
) -> float:
    s = exp_diff + gap_h * 1e8
    if not is_unknown_class(t_cls) and c_cls == t_cls:
        s -= 5e11
    if c_sub == t_sub:
        s -= 1e11
    elif (
        c_sub is not None
        and t_sub is not None
        and abs(c_sub - t_sub) <= 1
    ):
        s -= 5e10
    if forward:
        if c_lvl == t_lvl:
            s -= 5e10
        if same_server:
            s -= 2e10
    else:
        if c_lvl == t_lvl or c_lvl == t_lvl - 1:
            s -= 5e10
    if exp_diff <= 1e8:
        s -= 1e12
    return s


def format_track_entry(idx: int, p: dict, *, show_confidence: bool = False) -> str:
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
