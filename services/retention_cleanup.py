"""線上／離線共用的尋人導向保留清理編排。"""
from __future__ import annotations

from dataclasses import dataclass

from services.cmd_dedupe import prune_command_dedupe_sync
from services.retention_windows import (
    DEFAULT_MAX_TRANSFER_WINDOWS,
    DEFAULT_RECENT_DAYS,
    DEFAULT_TRANSFER_PAD_DAYS,
    ONLINE_DELETE_BATCH_SIZE,
    build_bridge_thin_ranges,
    build_search_keep_ranges,
    build_transfer_thin_ranges,
    exp_history_outside_keep_batch_sql,
    exp_history_transfer_middle_batch_sql,
    search_retention_cutoff,
    transfer_alert_retention_cutoff,
)
from services.settings_prune import (
    PRUNE_ALERT_DEDUPE_SQL,
    PRUNE_DEDUPE_SQL,
    boss_reminder_prune_bound,
    overspeed_prune_bound,
)
from services.timeutil import taipei_cutoff_str, today_taipei_str


@dataclass(frozen=True)
class RetentionPlan:
    keep_ranges: list[tuple[str, str]]
    thin_ranges: list[tuple[str, str]]
    retention_cutoff: str
    transfer_cutoff: str


@dataclass
class SecondaryPruneStats:
    deleted_transfer: int = 0
    deleted_settings: int = 0
    deleted_alert_dedupe: int = 0
    deleted_cmd_dedupe: int = 0


def build_retention_plan(
    *,
    recent_days: int = DEFAULT_RECENT_DAYS,
    pad_days: int = DEFAULT_TRANSFER_PAD_DAYS,
    max_transfer_windows: int = DEFAULT_MAX_TRANSFER_WINDOWS,
) -> RetentionPlan:
    keep_ranges = build_search_keep_ranges(
        recent_days=recent_days,
        pad_days=pad_days,
        max_transfer_windows=max_transfer_windows,
    )
    thin_ranges = build_transfer_thin_ranges(
        max_transfer_windows=max_transfer_windows,
        pad_days=pad_days,
    )
    thin_ranges.extend(
        build_bridge_thin_ranges(
            recent_days=recent_days,
            max_transfer_windows=max_transfer_windows,
            pad_days=pad_days,
        )
    )
    thin_ranges.sort(key=lambda item: item[0])
    return RetentionPlan(
        keep_ranges=keep_ranges,
        thin_ranges=thin_ranges,
        retention_cutoff=search_retention_cutoff(
            recent_days=recent_days,
            pad_days=pad_days,
            max_transfer_windows=max_transfer_windows,
        ),
        transfer_cutoff=transfer_alert_retention_cutoff(),
    )


def outside_keep_batch(
    plan: RetentionPlan, *, batch_size: int = ONLINE_DELETE_BATCH_SIZE
) -> tuple[str, tuple]:
    return exp_history_outside_keep_batch_sql(
        plan.keep_ranges, batch_size=batch_size
    )


def thin_range_batches(
    plan: RetentionPlan, *, batch_size: int = ONLINE_DELETE_BATCH_SIZE
) -> list[tuple[str, tuple, str]]:
    """回傳 [(sql, params, label), ...]。"""
    out: list[tuple[str, tuple, str]] = []
    for i, (start, end) in enumerate(plan.thin_ranges, 1):
        sql, params = exp_history_transfer_middle_batch_sql(
            start, end, batch_size=batch_size
        )
        out.append((sql, params, f"轉移窗中間#{i}"))
    return out


def _settings_date_only(retention_cutoff: str) -> str:
    if len(retention_cutoff) >= 10:
        return retention_cutoff[:10]
    return today_taipei_str()


async def prune_secondary_async(
    db,
    plan: RetentionPlan,
    *,
    prune_cmd_dedupe: bool = True,
    cmd_dedupe_days: int = 2,
) -> SecondaryPruneStats:
    """刪除 transfer / settings / alert_dedupe / cmd_dedupe 並 commit。"""
    stats = SecondaryPruneStats()
    async with db.execute(
        """
        DELETE FROM transfer_alerts_log
        WHERE alert_time < ?
        """,
        (plan.transfer_cutoff,),
    ) as cursor:
        stats.deleted_transfer = cursor.rowcount or 0

    cutoff_date_only = _settings_date_only(plan.retention_cutoff)
    async with db.execute(
        PRUNE_DEDUPE_SQL,
        (
            overspeed_prune_bound(plan.retention_cutoff),
            boss_reminder_prune_bound(cutoff_date_only),
        ),
    ) as cursor:
        stats.deleted_settings = cursor.rowcount or 0

    async with db.execute(
        PRUNE_ALERT_DEDUPE_SQL, (plan.retention_cutoff,)
    ) as cursor:
        stats.deleted_alert_dedupe = cursor.rowcount or 0

    if prune_cmd_dedupe:
        cmd_cutoff = taipei_cutoff_str(cmd_dedupe_days)
        async with db.execute(
            "DELETE FROM cmd_dedupe WHERE claimed_at < ?",
            (cmd_cutoff,),
        ) as cursor:
            stats.deleted_cmd_dedupe = cursor.rowcount or 0

    await db.commit()
    return stats


def prune_secondary_sync(
    conn,
    plan: RetentionPlan,
    *,
    has_transfer: bool = True,
    has_settings: bool = True,
    has_alert_dedupe: bool = True,
    prune_cmd_dedupe: bool = False,
    cmd_dedupe_days: int = 2,
) -> SecondaryPruneStats:
    stats = SecondaryPruneStats()
    if has_transfer:
        stats.deleted_transfer = conn.execute(
            """
            DELETE FROM transfer_alerts_log
            WHERE alert_time < ?
            """,
            (plan.transfer_cutoff,),
        ).rowcount
    if has_settings:
        cutoff_date_only = _settings_date_only(plan.retention_cutoff)
        stats.deleted_settings = conn.execute(
            PRUNE_DEDUPE_SQL,
            (
                overspeed_prune_bound(plan.retention_cutoff),
                boss_reminder_prune_bound(cutoff_date_only),
            ),
        ).rowcount
    if has_alert_dedupe:
        stats.deleted_alert_dedupe = conn.execute(
            PRUNE_ALERT_DEDUPE_SQL, (plan.retention_cutoff,)
        ).rowcount
    if prune_cmd_dedupe:
        stats.deleted_cmd_dedupe = prune_command_dedupe_sync(
            conn, days=cmd_dedupe_days
        )
    return stats
