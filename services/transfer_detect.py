"""轉服偵測：SQL 片段與純邏輯過濾／排序／評分。"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Optional, Sequence

from services.game_event_windows import match_realm_transfer

# 同名 margin 1000 億；異名（僅同職）100 億（SQL 初篩）
NAME_MARGIN = 1000 * 100_000_000
CLASS_MARGIN = 100 * 100_000_000
# 異名且無旅團／cohort：僅允許極小 EXP 差
RENAME_STRICT_MARGIN = 10 * 100_000_000
# 同批消失 cohort 最小人數
COHORT_MIN_SIZE = 3
# 最佳／次佳分數差距不足則視為 ambiguous
AMBIGUOUS_SCORE_GAP = 40

# SQL row indices（含 guild）
IDX_NEW_EXP = 0
IDX_NEW_NAME = 1
IDX_NEW_SERVER = 2
IDX_NEW_LVL = 3
IDX_NEW_CLS = 4
IDX_NEW_GUILD = 5
IDX_OLD_NAME = 6
IDX_OLD_SERVER = 7
IDX_OLD_LVL = 8
IDX_OLD_CLS = 9
IDX_OLD_EXP = 10
IDX_OLD_GUILD = 11
IDX_NEW_SUB = 12
IDX_OLD_SUB = 13
IDX_OLD_LAST_SEEN = 14  # optional; missing-queue rows may omit

POTENTIAL_TRANSFERS_SQL = """
    SELECT DISTINCT
           t_now.exp, t_now.player_name, t_now.server_name, t_now.level, t_now.class_name,
           COALESCE(t_now.guild_name, ''),
           t_old.player_name, t_old.server_name, t_old.level, t_old.class_name,
           t_old.exp, COALESCE(t_old.guild_name, ''),
           t_now.subjugation_grade, t_old.subjugation_grade,
           t_old.record_time
    FROM exp_history t_now
    JOIN (
        SELECT e.player_name, e.server_name, e.class_name, e.level, e.subjugation_grade,
               e.exp, e.record_time, e.guild_name
        FROM exp_history e
        INNER JOIN (
            SELECT player_name, server_name, MAX(record_time) AS max_time
            FROM exp_history
            WHERE record_time <= ? AND record_time >= datetime(?, '-7 days')
            GROUP BY player_name, server_name
        ) latest
          ON e.player_name = latest.player_name
         AND e.server_name = latest.server_name
         AND e.record_time = latest.max_time
    ) t_old ON (
        (t_now.player_name = t_old.player_name
         AND t_now.exp >= t_old.exp AND t_now.exp <= (t_old.exp + ?))
        OR (
            t_now.player_name != t_old.player_name
            AND t_now.class_name = t_old.class_name
            AND t_now.class_name IS NOT NULL
            AND t_old.class_name IS NOT NULL
            AND t_now.class_name NOT IN ('', 'None', '未知')
            AND t_old.class_name NOT IN ('', 'None', '未知')
            AND COALESCE(t_now.subjugation_grade, -1) = COALESCE(t_old.subjugation_grade, -2)
            AND ABS(t_now.level - t_old.level) <= 1
            AND t_now.exp >= t_old.exp AND t_now.exp <= (t_old.exp + ?)
        )
    )
    WHERE t_now.record_time = ? AND t_now.exp > 1000000000000
      AND t_now.level >= t_old.level
      AND COALESCE(t_now.subjugation_grade, 0) >= COALESCE(t_old.subjugation_grade, 0)
      AND t_now.server_name != t_old.server_name
      AND NOT EXISTS (
          SELECT 1 FROM exp_history t_check
          WHERE t_check.record_time = ?
            AND t_check.player_name = t_now.player_name
            AND t_check.server_name = t_now.server_name
      )
"""


def _guild(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text in ("", "None", "null", "未知") else text


def row_new_guild(row: tuple) -> str:
    return _guild(row[IDX_NEW_GUILD] if len(row) > IDX_NEW_GUILD else "")


def row_old_guild(row: tuple) -> str:
    return _guild(row[IDX_OLD_GUILD] if len(row) > IDX_OLD_GUILD else "")


def row_old_last_seen(row: tuple) -> Optional[str]:
    if len(row) > IDX_OLD_LAST_SEEN and row[IDX_OLD_LAST_SEEN]:
        return str(row[IDX_OLD_LAST_SEEN])
    return None


def same_guild(row: tuple) -> bool:
    og, ng = row_old_guild(row), row_new_guild(row)
    return bool(og) and og == ng


def transfer_sort_key(row: tuple, *, cohort_boost: bool = False) -> tuple:
    """較佳候選排前面：同名 > 同旅團 > cohort > 同級 > 同討伐 > 經驗差小。"""
    return (
        0 if row[IDX_NEW_NAME] == row[IDX_OLD_NAME] else 1,
        0 if same_guild(row) else 1,
        0 if cohort_boost else 1,
        0 if row[IDX_NEW_LVL] == row[IDX_OLD_LVL] else 1,
        0
        if (
            row[IDX_NEW_SUB] is not None
            and row[IDX_OLD_SUB] is not None
            and row[IDX_NEW_SUB] == row[IDX_OLD_SUB]
        )
        else 1,
        row[IDX_NEW_EXP] - row[IDX_OLD_EXP],
    )


def should_skip_rename_mismatch(row: tuple) -> bool:
    """異名時討伐必須一致；否則略過。"""
    if row[IDX_NEW_NAME] == row[IDX_OLD_NAME]:
        return False
    if (
        row[IDX_NEW_SUB] is None
        or row[IDX_OLD_SUB] is None
        or row[IDX_NEW_SUB] != row[IDX_OLD_SUB]
    ):
        return True
    return False


def transfer_status(new_name: str, old_name: str) -> str:
    return "跨服轉移並改名" if new_name != old_name else "跨服轉移"


def format_exp_diff(exp_diff: float) -> str:
    if exp_diff == 0:
        return "+0.00% (完美吻合)"
    return f"+{(exp_diff / 100_000_000):,.0f} 億 (轉移期間偷練)"


def candidate_score(row: tuple, *, cohort_boost: bool = False) -> int:
    """多特徵分數；越高越可信。"""
    score = 0
    same_name = row[IDX_NEW_NAME] == row[IDX_OLD_NAME]
    exp_diff = float(row[IDX_NEW_EXP]) - float(row[IDX_OLD_EXP])
    if same_name:
        score += 1000
    if same_guild(row):
        score += 200
    if cohort_boost:
        score += 120
    if row[IDX_NEW_LVL] == row[IDX_OLD_LVL]:
        score += 50
    if (
        row[IDX_NEW_SUB] is not None
        and row[IDX_OLD_SUB] is not None
        and row[IDX_NEW_SUB] == row[IDX_OLD_SUB]
    ):
        score += 50
    if row[IDX_NEW_CLS] == row[IDX_OLD_CLS]:
        score += 30
    # EXP 差越小越好（每 10 億扣 1，最多扣 80）
    penalty = min(80, int(max(0.0, exp_diff) / (10 * 100_000_000)))
    score -= penalty
    if exp_diff == 0:
        score += 40
    return score


def rename_allowed(
    row: tuple,
    *,
    cohort_boost: bool = False,
    in_transfer_window: bool = True,
    max_speed_threshold: float = 200000000000,
) -> bool:
    """異名是否允許自動報警。"""
    if row[IDX_NEW_NAME] == row[IDX_OLD_NAME]:
        return True
    if not in_transfer_window:
        return False
    exp_diff = float(row[IDX_NEW_EXP]) - float(row[IDX_OLD_EXP])

    if exp_diff < 0 or exp_diff > max_speed_threshold:
        return False

    if same_guild(row) or cohort_boost:
        return exp_diff <= CLASS_MARGIN
    return exp_diff <= RENAME_STRICT_MARGIN


def build_cohort_keys(
    rows: Sequence[tuple],
    *,
    min_size: int = COHORT_MIN_SIZE,
) -> set[tuple[str, str, str]]:
    """同舊旅團、同舊服、同新服且人數 ≥ min_size → cohort key。"""
    groups: dict[tuple[str, str, str], set[tuple[str, str]]] = defaultdict(set)
    for row in rows:
        guild = row_old_guild(row)
        if not guild:
            continue
        key = (guild, row[IDX_OLD_SERVER], row[IDX_NEW_SERVER])
        groups[key].add((row[IDX_OLD_NAME], row[IDX_OLD_SERVER]))
    return {k for k, members in groups.items() if len(members) >= min_size}


def row_in_cohort(row: tuple, cohort_keys: set[tuple[str, str, str]]) -> bool:
    guild = row_old_guild(row)
    if not guild:
        return False
    return (guild, row[IDX_OLD_SERVER], row[IDX_NEW_SERVER]) in cohort_keys


def passes_window_gate(
    row: tuple,
    *,
    appear_time: str,
    in_active_period: bool,
) -> bool:
    """窗外只允許同名；窗內須符合領域轉移銜接（有 last_seen 時）。"""
    same_name = row[IDX_NEW_NAME] == row[IDX_OLD_NAME]
    if not in_active_period:
        return same_name
    last_seen = row_old_last_seen(row)
    if last_seen:
        return match_realm_transfer(last_seen, appear_time) is not None
    # 無 last_seen 時（測試／舊列）僅靠活躍期
    return True


def rank_transfer_candidates(
    transfer_records: list[tuple],
    *,
    appear_time: Optional[str] = None,
    in_active_period: bool = True,
) -> list[tuple]:
    """過濾異名討伐不符／窗規則／異名門檻後，依優先序排序。"""
    cohort_keys = build_cohort_keys(transfer_records) if in_active_period else set()
    filtered: list[tuple] = []
    for row in transfer_records:
        if should_skip_rename_mismatch(row):
            continue
        if appear_time is not None and not passes_window_gate(
            row, appear_time=appear_time, in_active_period=in_active_period
        ):
            continue
        cohort = row_in_cohort(row, cohort_keys)
        if not rename_allowed(
            row, cohort_boost=cohort, in_transfer_window=in_active_period
        ):
            continue
        filtered.append(row)
    filtered.sort(
        key=lambda r: transfer_sort_key(r, cohort_boost=row_in_cohort(r, cohort_keys))
    )
    return filtered


def pick_unique_pairs(
    ranked_rows: Iterable[tuple],
    already_alerted: set[tuple],
    *,
    in_active_period: bool = True,
    ambiguous_gap: int = AMBIGUOUS_SCORE_GAP,
) -> list[dict[str, Any]]:
    """一對一配對：舊角／新角各只能配一次；分數過近則略過（ambiguous）。"""
    rows = list(ranked_rows)
    cohort_keys = build_cohort_keys(rows) if in_active_period else set()

    # 依新角彙整候選分數，偵測 ambiguous
    by_new: dict[tuple[str, str], list[tuple[int, tuple]]] = defaultdict(list)
    by_old: dict[tuple[str, str], list[tuple[int, tuple]]] = defaultdict(list)
    for row in rows:
        cohort = row_in_cohort(row, cohort_keys)
        score = candidate_score(row, cohort_boost=cohort)
        new_key = (row[IDX_NEW_NAME], row[IDX_NEW_SERVER])
        old_key = (row[IDX_OLD_NAME], row[IDX_OLD_SERVER])
        by_new[new_key].append((score, row))
        by_old[old_key].append((score, row))

    for lst in by_new.values():
        lst.sort(key=lambda x: -x[0])
    for lst in by_old.values():
        lst.sort(key=lambda x: -x[0])

    def _is_ambiguous(new_key: tuple[str, str], old_key: tuple[str, str]) -> bool:
        new_cands = by_new.get(new_key) or []
        old_cands = by_old.get(old_key) or []
        if len(new_cands) >= 2 and (new_cands[0][0] - new_cands[1][0]) < ambiguous_gap:
            # 僅當次佳也指向不同舊角時才算模糊
            if (
                new_cands[0][1][IDX_OLD_NAME],
                new_cands[0][1][IDX_OLD_SERVER],
            ) != (
                new_cands[1][1][IDX_OLD_NAME],
                new_cands[1][1][IDX_OLD_SERVER],
            ):
                return True
        if len(old_cands) >= 2 and (old_cands[0][0] - old_cands[1][0]) < ambiguous_gap:
            if (
                old_cands[0][1][IDX_NEW_NAME],
                old_cands[0][1][IDX_NEW_SERVER],
            ) != (
                old_cands[1][1][IDX_NEW_NAME],
                old_cands[1][1][IDX_NEW_SERVER],
            ):
                return True
        return False

    # 全域依分數再排一次
    scored = sorted(
        (
            (
                candidate_score(row, cohort_boost=row_in_cohort(row, cohort_keys)),
                row,
            )
            for row in rows
        ),
        key=lambda x: (-x[0], transfer_sort_key(x[1])),
    )

    matched_old: set[tuple] = set()
    matched_new: set[tuple] = set()
    results: list[dict[str, Any]] = []

    for score, row in scored:
        new_name, new_server = row[IDX_NEW_NAME], row[IDX_NEW_SERVER]
        old_name, old_server = row[IDX_OLD_NAME], row[IDX_OLD_SERVER]
        old_key = (old_name, old_server)
        new_key = (new_name, new_server)
        if old_key in matched_old or new_key in matched_new:
            continue

        pair_key = (old_name, old_server, new_name, new_server)
        if pair_key in already_alerted:
            continue
        if _is_ambiguous(new_key, old_key):
            continue

        cohort = row_in_cohort(row, cohort_keys)
        matched_old.add(old_key)
        matched_new.add(new_key)

        confidence_score = min(1.0, max(0.0, score / 1500.0))
        is_name_change = new_name != old_name

        results.append(
            {
                "pair_key": pair_key,
                "old_key": old_key,
                "new_key": new_key,
                "new_name": new_name,
                "new_server": new_server,
                "old_name": old_name,
                "old_server": old_server,
                "new_lvl": row[IDX_NEW_LVL],
                "new_cls": row[IDX_NEW_CLS],
                "old_lvl": row[IDX_OLD_LVL],
                "old_cls": row[IDX_OLD_CLS],
                "new_sub_grade": row[IDX_NEW_SUB],
                "old_guild": row_old_guild(row),
                "new_guild": row_new_guild(row),
                "new_exp": row[IDX_NEW_EXP],
                "exp_diff": row[IDX_NEW_EXP] - row[IDX_OLD_EXP],
                "status": f"疑似改名轉服 (信心: {confidence_score:.2f})" if is_name_change else "跨服轉移",
                "score": score,
                "cohort": cohort,
                "confidence_score": confidence_score,
                "is_name_change": is_name_change,
            }
        )
    return results


def missing_to_transfer_row(
    *,
    new_exp: float,
    new_name: str,
    new_server: str,
    new_lvl: int,
    new_cls: str,
    new_guild: str,
    new_sub: Any,
    old_name: str,
    old_server: str,
    old_lvl: Any,
    old_cls: str,
    old_exp: float,
    old_guild: str,
    old_sub: Any,
    old_last_seen: str,
) -> tuple:
    """將 missing-queue 配對轉成與 SQL 相同的 row tuple。"""
    return (
        new_exp,
        new_name,
        new_server,
        new_lvl,
        new_cls,
        _guild(new_guild),
        old_name,
        old_server,
        old_lvl,
        old_cls,
        old_exp,
        _guild(old_guild),
        new_sub,
        old_sub,
        old_last_seen,
    )
