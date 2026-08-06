"""
離線清理 prasia_data.db：刪除過期 exp_history / transfer_alerts_log，並 VACUUM 回收磁碟空間。

使用前請先停止 bot（VACUUM 需要獨占存取）。

範例：
  python cleanup_db.py
  python cleanup_db.py --days 60
  python cleanup_db.py --days 30 --dry-run
  python cleanup_db.py --for-search
  python cleanup_db.py --for-search --dry-run
  # 預設：近3天 ∪ 最近1次轉移窗+pad3天；窗+pad 稀疏化；建索引 + VACUUM（NAS 建議）
  python cleanup_db.py --build-indexes   # 離線建立尋人索引 + player_profile（大庫必做）
  python cleanup_db.py --wipe-history
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOCK_PATH = ROOT / ".bot.lock"
DEFAULT_DAYS = 60
# NAS 記憶體較緊時，偏小批次 + 定期 checkpoint 較不易被 OOM 殺掉
DELETE_BATCH_SIZE = 20_000
CHECKPOINT_EVERY_BATCHES = 10

# 允許直接執行：把專案根加入 path
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.instance_lock import get_live_instance_holder  # noqa: E402
from db.paths import resolve_db_path  # noqa: E402
from db.schema import (  # noqa: E402
    build_search_indexes_sync,
    list_missing_search_indexes,
    rebuild_player_profiles_sync,
)
from db.singleton_lock import is_process_lock_held  # noqa: E402
from services.cmd_dedupe import prune_command_dedupe_sync  # noqa: E402
from services.retention_cleanup import (  # noqa: E402
    build_retention_plan,
    prune_secondary_sync,
)
from services.retention_windows import (  # noqa: E402
    DEFAULT_MAX_TRANSFER_WINDOWS,
    DEFAULT_RECENT_DAYS,
    DEFAULT_TRANSFER_PAD_DAYS,
    build_bridge_thin_ranges,
    build_search_keep_ranges,
    build_transfer_thin_ranges,
    exp_history_outside_keep_batch_sql,
    exp_history_outside_keep_sql,
    exp_history_transfer_middle_batch_sql,
    exp_history_transfer_middle_statements,
    search_retention_cutoff,
    transfer_alert_retention_cutoff,
)
from services.settings_prune import (  # noqa: E402
    PRUNE_ALERT_DEDUPE_SQL,
    PRUNE_DEDUPE_SQL,
    boss_reminder_prune_bound,
    overspeed_prune_bound,
)
from services.timeutil import taipei_cutoff_str  # noqa: E402

try:
    from dotenv import load_dotenv
except ImportError:  # NAS host 等未裝依賴時仍可離線清理
    load_dotenv = None  # type: ignore[assignment]
else:
    load_dotenv(ROOT / ".env")


def _bot_appears_running() -> bool:
    return is_process_lock_held(LOCK_PATH)


def _refuse_if_bot_running(conn: sqlite3.Connection, *, force: bool) -> int | None:
    """本機檔案鎖或跨機 instance heartbeat 仍活著時拒絕（除非 --force）。

    回傳非 0 表示應結束程式；None 表示可繼續。
    """
    local_held = _bot_appears_running()
    remote = get_live_instance_holder(conn)
    if not local_held and remote is None:
        return None
    if force:
        if local_held:
            print(
                f"警告：--force 略過本機 {LOCK_PATH.name}；"
                "若 bot 仍在跑可能損壞資料庫。",
                flush=True,
            )
        if remote is not None:
            holder, heartbeat = remote
            print(
                f"警告：--force 略過跨機 instance lock "
                f"（holder={holder}, heartbeat={heartbeat}）。",
                flush=True,
            )
        return None
    if local_held:
        print(
            f"錯誤：偵測到 bot 仍佔用 {LOCK_PATH.name}（檔案鎖）。\n"
            "請先在 Docker 停止 prasia-bot-final，或確認已停後加 --force。\n"
            "（若容器已停仍出現此訊息，多半是舊版誤判殘留 PID；請 git pull 後重試。）",
            file=sys.stderr,
        )
        return 1
    holder, heartbeat = remote  # type: ignore[misc]
    print(
        "錯誤：偵測到共享資料庫上仍有活躍 bot 實例 "
        f"（holder={holder}, heartbeat={heartbeat}）。\n"
        "請先停止遠端 bot，或確認 heartbeat 過期後再跑；緊急時加 --force。",
        file=sys.stderr,
    )
    return 1


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _log(msg: str) -> None:
    """進度輸出立即 flush，避免 docker/管道看起來像卡住。"""
    print(msg, flush=True)


def _execute_with_busy_retry(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple = (),
    *,
    retries: int = 12,
    sleep_sec: float = 5.0,
):
    """database is locked 時重試（常見於 bot 未停乾淨／WAL 殘留）。"""
    import time

    last: BaseException | None = None
    for attempt in range(1, retries + 1):
        try:
            return conn.execute(sql, params)
        except sqlite3.OperationalError as e:
            last = e
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            _log(
                f"  資料庫忙碌（{e}），{sleep_sec:.0f}s 後重試 "
                f"{attempt}/{retries}…"
            )
            time.sleep(sleep_sec)
    assert last is not None
    raise last


def _delete_in_batches(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple,
    *,
    label: str,
    batch_size: int = DELETE_BATCH_SIZE,
) -> int:
    """重複執行 LIMIT 批次 DELETE，每批 commit，回傳總刪除數。"""
    total = 0
    batch_no = 0
    while True:
        batch_no += 1
        _log(f"  {label} 開始第 {batch_no} 批…")
        cur = _execute_with_busy_retry(conn, sql, params)
        n = cur.rowcount
        if n <= 0:
            break
        total += n
        conn.commit()
        _log(f"  {label} 第 {batch_no} 批刪 {n:,}（累計 {total:,}）")
        if batch_no % CHECKPOINT_EVERY_BATCHES == 0:
            try:
                _log(f"  {label} 中途 wal_checkpoint…")
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.OperationalError as e:
                _log(f"  checkpoint 略過：{e}")
        if n < batch_size:
            break
    return total


def _ensure_exclusive_access(conn: sqlite3.Connection) -> str | None:
    """嘗試取得寫入鎖；失敗時回傳錯誤提示字串。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.commit()
        return None
    except sqlite3.OperationalError as e:
        return (
            f"無法鎖定資料庫（{e}）。\n"
            "請先確認已停止 prasia-bot-final，且沒有其他 cleanup 在跑：\n"
            "  docker stop prasia-bot-final\n"
            "  docker exec dc-cleanup ps aux | grep cleanup_db\n"
            "停乾淨後再重試。"
        )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def build_cleanup_parser(default_db: Path | None = None) -> argparse.ArgumentParser:
    db_hint = default_db if default_db is not None else resolve_db_path(ROOT)
    parser = argparse.ArgumentParser(description="離線清理 prasia_data.db")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help=f"資料庫路徑（預設與 bot 相同：DB_PATH 或 {db_hint.name}）",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"保留天數（預設 {DEFAULT_DAYS}；與 --for-search 互斥）",
    )
    parser.add_argument(
        "--for-search",
        action="store_true",
        help=(
            "尋人導向：保留最近 N 天 ∪ 最近 K 次領域轉移窗"
            "（窗開始～結束後 pad 天），其餘刪除；"
            "轉移窗+pad 同角同服只留首尾"
        ),
    )
    parser.add_argument(
        "--recent-days",
        type=int,
        default=DEFAULT_RECENT_DAYS,
        help=f"--for-search 時保留最近天數（預設 {DEFAULT_RECENT_DAYS}）",
    )
    parser.add_argument(
        "--transfer-pad-days",
        type=int,
        default=DEFAULT_TRANSFER_PAD_DAYS,
        help=f"--for-search 時領域轉移結束後再保留天數（預設 {DEFAULT_TRANSFER_PAD_DAYS}）",
    )
    parser.add_argument(
        "--max-transfer-windows",
        type=int,
        default=DEFAULT_MAX_TRANSFER_WINDOWS,
        help=(
            f"--for-search 時只保留最近幾次領域轉移窗"
            f"（預設 {DEFAULT_MAX_TRANSFER_WINDOWS}）"
        ),
    )
    parser.add_argument(
        "--wipe-history",
        action="store_true",
        help="清空整張 exp_history 與 transfer_alerts_log（忽略 --days / --for-search）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只統計將刪除的筆數，不寫入、不 VACUUM",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使偵測到 bot 可能仍在執行也繼續",
    )
    parser.add_argument(
        "--no-vacuum",
        action="store_true",
        help="刪除後不做 VACUUM（檔案不會立刻變小）",
    )
    parser.add_argument(
        "--build-indexes",
        action="store_true",
        help="離線建立尋人索引（大表啟動時會略過；建議清庫後執行）",
    )
    return parser


def validate_cleanup_args(args: argparse.Namespace, argv: list[str]) -> str | None:
    """回傳錯誤訊息；通過則 None。"""
    if args.wipe_history and args.for_search:
        return "錯誤：--wipe-history 與 --for-search 不可同時使用"
    indexes_only = bool(args.build_indexes) and not (
        args.wipe_history or args.for_search or "--days" in argv
    )
    if args.for_search:
        if args.recent_days < 0:
            return "錯誤：--recent-days 必須 >= 0"
        if args.transfer_pad_days < 0:
            return "錯誤：--transfer-pad-days 必須 >= 0"
        if args.max_transfer_windows < 0:
            return "錯誤：--max-transfer-windows 必須 >= 0"
    elif not indexes_only and args.days < 1 and not args.wipe_history:
        return (
            "錯誤：--days 必須 >= 1（或改用 --wipe-history / --for-search / --build-indexes）"
        )
    return None


def is_indexes_only(args: argparse.Namespace, argv: list[str]) -> bool:
    return bool(args.build_indexes) and not (
        args.wipe_history or args.for_search or "--days" in argv
    )


def main() -> int:
    default_db = resolve_db_path(ROOT)
    parser = build_cleanup_parser(default_db)
    args = parser.parse_args()

    err = validate_cleanup_args(args, sys.argv)
    if err:
        print(err, file=sys.stderr)
        return 2
    indexes_only = is_indexes_only(args, sys.argv)

    db_path: Path = args.db if args.db is not None else default_db
    if not db_path.is_file():
        print(f"錯誤：找不到資料庫 {db_path}", file=sys.stderr)
        return 1

    before = _file_size(db_path)
    wal = Path(str(db_path) + "-wal")
    shm = Path(str(db_path) + "-shm")
    before_total = before + _file_size(wal) + _file_size(shm)

    _log(f"資料庫：{db_path}")
    _log(f"清理前大小：{_fmt_bytes(before)}（含 WAL/SHM 約 {_fmt_bytes(before_total)}）")

    conn = sqlite3.connect(str(db_path), timeout=120)
    try:
        conn.execute("PRAGMA busy_timeout=120000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")

        refuse = _refuse_if_bot_running(conn, force=bool(args.force))
        if refuse is not None:
            return refuse
        # 約 64MB page cache，降低大 DELETE 被 OOM 的機率
        conn.execute("PRAGMA cache_size=-65536")
        lock_err = _ensure_exclusive_access(conn)
        if lock_err:
            _log(lock_err)
            return 1
        has_exp = _table_exists(conn, "exp_history")
        has_transfer = _table_exists(conn, "transfer_alerts_log")
        has_settings = _table_exists(conn, "bot_settings")
        has_alert_dedupe = _table_exists(conn, "alert_dedupe")
        has_transfer_missing = _table_exists(conn, "transfer_missing")
        has_cmd_dedupe = _table_exists(conn, "cmd_dedupe")

        if indexes_only:
            if args.dry_run:
                missing = list_missing_search_indexes(conn)
                _log("模式：僅檢查尋人索引（dry-run）")
                _log(f"將建立：{', '.join(missing) if missing else '（已齊全）'}")
                return 0
            _log("模式：僅建立尋人索引")
            created = build_search_indexes_sync(conn)
            _log(
                f"索引：新建 {len(created)} 個"
                + (f"（{', '.join(created)}）" if created else "（原本已齊全）")
            )
            if has_exp:
                _log("正在重建 player_profile…")
                n_prof = rebuild_player_profiles_sync(conn)
                _log(f"player_profile：{n_prof:,} 筆")
            return 0

        # 大庫全表 COUNT 極慢；實際刪除改看 DELETE rowcount。
        # 僅 --dry-run 才預先統計（仍可能需數十分鐘）。
        do_precount = bool(args.dry_run)
        exp_total = -1
        transfer_total = -1
        exp_stale = -1
        exp_middle_stale = -1
        transfer_stale = -1
        settings_stale = -1
        alert_dedupe_stale = -1

        keep_ranges: list[tuple[str, str]] = []
        thin_ranges: list[tuple[str, str]] = []
        settings_cutoff: str | None = None
        transfer_cutoff: str | None = None

        if args.wipe_history:
            mode = "清空全部歷史"
            if do_precount:
                _log("正在統計筆數（dry-run，大庫可能很久）…")
                exp_total = (
                    _count(conn, "SELECT COUNT(*) FROM exp_history") if has_exp else 0
                )
                transfer_total = (
                    _count(conn, "SELECT COUNT(*) FROM transfer_alerts_log")
                    if has_transfer
                    else 0
                )
                exp_stale = exp_total
                transfer_stale = transfer_total
                settings_stale = (
                    _count(
                        conn,
                        """
                        SELECT COUNT(*) FROM bot_settings
                        WHERE key LIKE 'overspeed:%' OR key LIKE 'boss_reminder:%'
                        """,
                    )
                    if has_settings
                    else 0
                )
                alert_dedupe_stale = (
                    _count(conn, "SELECT COUNT(*) FROM alert_dedupe")
                    if has_alert_dedupe
                    else 0
                )
        elif args.for_search:
            keep_ranges = build_search_keep_ranges(
                recent_days=args.recent_days,
                pad_days=args.transfer_pad_days,
                max_transfer_windows=args.max_transfer_windows,
            )
            thin_ranges = build_transfer_thin_ranges(
                max_transfer_windows=args.max_transfer_windows,
                pad_days=args.transfer_pad_days,
            )
            thin_ranges.extend(
                build_bridge_thin_ranges(
                    recent_days=args.recent_days,
                    max_transfer_windows=args.max_transfer_windows,
                    pad_days=args.transfer_pad_days,
                )
            )
            thin_ranges.sort(key=lambda item: item[0])
            settings_cutoff = search_retention_cutoff(
                recent_days=args.recent_days,
                pad_days=args.transfer_pad_days,
                max_transfer_windows=args.max_transfer_windows,
            )
            transfer_cutoff = transfer_alert_retention_cutoff()
            mode = (
                f"尋人導向（最近 {args.recent_days} 天 ∪ "
                f"最近 {args.max_transfer_windows} 次轉移窗"
                f"～結束後+{args.transfer_pad_days} 天；"
                f"轉移窗+pad 同角同服只留首尾）"
            )
            # 先印區間，避免大庫 COUNT 讓人以為卡住
            _log(f"模式：{mode}")
            if keep_ranges:
                _log("保留區間：")
                for start, end in keep_ranges:
                    _log(f"  {start}  ~  {end}")
            if thin_ranges:
                _log("轉移窗+橋接區稀疏化（同角同服只留首尾）：")
                for start, end in thin_ranges:
                    _log(f"  {start}  ~  {end}")
            if do_precount:
                _log("正在統計筆數（dry-run，千萬筆級可能需數十分鐘）…")
                exp_total = (
                    _count(conn, "SELECT COUNT(*) FROM exp_history") if has_exp else 0
                )
                _log(f"  exp_history 總筆數：{exp_total:,}")
                count_sql, count_params = exp_history_outside_keep_sql(keep_ranges)
                exp_stale = (
                    _count(conn, count_sql, count_params) if has_exp else 0
                )
                _log(f"  窗外將刪：{exp_stale:,}")
                exp_middle_stale = 0
                if has_exp and thin_ranges:
                    for i, (count_sql_m, count_params_m) in enumerate(
                        exp_history_transfer_middle_statements(
                            thin_ranges, for_delete=False
                        ),
                        start=1,
                    ):
                        _log(f"  統計轉移窗中間 #{i}/{len(thin_ranges)}…")
                        exp_middle_stale += _count(conn, count_sql_m, count_params_m)
                _log(f"  轉移窗中間將刪：{exp_middle_stale:,}")
                transfer_total = (
                    _count(conn, "SELECT COUNT(*) FROM transfer_alerts_log")
                    if has_transfer
                    else 0
                )
                transfer_stale = (
                    _count(
                        conn,
                        """
                        SELECT COUNT(*) FROM transfer_alerts_log
                        WHERE alert_time < ?
                        """,
                        (transfer_cutoff,),
                    )
                    if has_transfer
                    else 0
                )
                settings_stale = (
                    _count(
                        conn,
                        """
                        SELECT COUNT(*) FROM bot_settings
                        WHERE (key LIKE 'overspeed:%' AND key < ?)
                           OR (key LIKE 'boss_reminder:%' AND key < ?)
                        """,
                        (
                            overspeed_prune_bound(settings_cutoff),
                            boss_reminder_prune_bound(settings_cutoff[:10]),
                        ),
                    )
                    if has_settings
                    else 0
                )
                alert_dedupe_stale = (
                    _count(
                        conn,
                        "SELECT COUNT(*) FROM alert_dedupe WHERE created_at < ?",
                        (settings_cutoff,),
                    )
                    if has_alert_dedupe
                    else 0
                )
        else:
            cutoff = taipei_cutoff_str(args.days)
            settings_cutoff = cutoff
            mode = f"保留最近 {args.days} 天（截止 {cutoff} 台北）"
            if do_precount:
                _log("正在統計筆數（dry-run，大庫可能很久）…")
                exp_total = (
                    _count(conn, "SELECT COUNT(*) FROM exp_history") if has_exp else 0
                )
                transfer_total = (
                    _count(conn, "SELECT COUNT(*) FROM transfer_alerts_log")
                    if has_transfer
                    else 0
                )
                exp_stale = (
                    _count(
                        conn,
                        """
                        SELECT COUNT(*) FROM exp_history
                        WHERE record_time < ?
                        """,
                        (cutoff,),
                    )
                    if has_exp
                    else 0
                )
                transfer_stale = (
                    _count(
                        conn,
                        """
                        SELECT COUNT(*) FROM transfer_alerts_log
                        WHERE alert_time < ?
                        """,
                        (cutoff,),
                    )
                    if has_transfer
                    else 0
                )
                settings_stale = (
                    _count(
                        conn,
                        """
                        SELECT COUNT(*) FROM bot_settings
                        WHERE (key LIKE 'overspeed:%' AND key < ?)
                           OR (key LIKE 'boss_reminder:%' AND key < ?)
                        """,
                        (
                            overspeed_prune_bound(cutoff),
                            boss_reminder_prune_bound(cutoff[:10]),
                        ),
                    )
                    if has_settings
                    else 0
                )
                alert_dedupe_stale = (
                    _count(
                        conn,
                        "SELECT COUNT(*) FROM alert_dedupe WHERE created_at < ?",
                        (cutoff,),
                    )
                    if has_alert_dedupe
                    else 0
                )

        if not args.for_search:
            _log(f"模式：{mode}")
            if keep_ranges:
                _log("保留區間：")
                for start, end in keep_ranges:
                    _log(f"  {start}  ~  {end}")
            if thin_ranges:
                _log("轉移窗+橋接區稀疏化（同角同服只留首尾）：")
                for start, end in thin_ranges:
                    _log(f"  {start}  ~  {end}")

        if do_precount:
            _log(
                f"exp_history：共 {exp_total:,} 筆，窗外將刪 {exp_stale:,} 筆"
            )
            if args.for_search:
                _log(f"轉移窗中間將刪 {exp_middle_stale:,} 筆")
            _log(
                f"transfer_alerts_log：共 {transfer_total:,} 筆，將刪 {transfer_stale:,} 筆"
            )
            if has_settings:
                _log(f"bot_settings 去重 key：將刪 {settings_stale:,} 筆")
            else:
                _log("bot_settings：表不存在，略過")
            if has_alert_dedupe:
                _log(f"alert_dedupe：將刪 {alert_dedupe_stale:,} 筆")
            else:
                _log("alert_dedupe：表不存在，略過")
            _log("dry-run：未修改資料庫。")
            return 0

        _log("略過預先 COUNT（大庫太慢）；改直接 DELETE，結束後印實際刪除數。")

        deleted_exp = 0
        deleted_middle = 0
        deleted_transfer = 0
        deleted_settings = 0
        deleted_alert_dedupe = 0
        deleted_transfer_missing = 0
        deleted_cmd_dedupe = 0
        if args.wipe_history:
            if has_exp:
                _log("正在清空 exp_history…")
                deleted_exp = conn.execute("DELETE FROM exp_history").rowcount
            if has_transfer:
                deleted_transfer = conn.execute("DELETE FROM transfer_alerts_log").rowcount
            if has_settings:
                deleted_settings = conn.execute(
                    """
                    DELETE FROM bot_settings
                    WHERE key LIKE 'overspeed:%' OR key LIKE 'boss_reminder:%'
                    """
                ).rowcount
            if has_alert_dedupe:
                deleted_alert_dedupe = conn.execute("DELETE FROM alert_dedupe").rowcount
            if has_transfer_missing:
                deleted_transfer_missing = conn.execute(
                    "DELETE FROM transfer_missing"
                ).rowcount
        elif args.for_search:
            if has_exp and keep_ranges:
                _log(
                    f"正在分批刪除保留窗外 exp_history"
                    f"（每批 {DELETE_BATCH_SIZE:,}，可能很久）…"
                )
                batch_sql, batch_params = exp_history_outside_keep_batch_sql(
                    keep_ranges, batch_size=DELETE_BATCH_SIZE
                )
                deleted_exp = _delete_in_batches(
                    conn,
                    batch_sql,
                    batch_params,
                    label="窗外",
                    batch_size=DELETE_BATCH_SIZE,
                )
                _log(f"  窗外合計已刪 {deleted_exp:,} 筆")
            if has_exp and thin_ranges:
                for i, (start, end) in enumerate(thin_ranges, start=1):
                    _log(
                        f"正在分批稀疏化轉移窗+pad #{i}/{len(thin_ranges)} "
                        f"{start} ~ {end}…"
                    )
                    thin_sql, thin_params = exp_history_transfer_middle_batch_sql(
                        start, end, batch_size=DELETE_BATCH_SIZE
                    )
                    n = _delete_in_batches(
                        conn,
                        thin_sql,
                        thin_params,
                        label=f"窗{i}中間",
                        batch_size=DELETE_BATCH_SIZE,
                    )
                    deleted_middle += n
                    _log(f"  本窗中間合計已刪 {n:,} 筆")
            assert settings_cutoff is not None
            assert transfer_cutoff is not None
            plan = build_retention_plan(
                recent_days=args.recent_days,
                pad_days=args.transfer_pad_days,
                max_transfer_windows=args.max_transfer_windows,
            )
            # dry-run 計數路徑仍用上方 cutoff；實際刪除走共用 prune
            secondary = prune_secondary_sync(
                conn,
                plan,
                has_transfer=has_transfer,
                has_settings=has_settings,
                has_alert_dedupe=has_alert_dedupe,
                has_transfer_missing=has_transfer_missing,
                prune_cmd_dedupe=has_cmd_dedupe,
            )
            deleted_transfer = secondary.deleted_transfer
            deleted_settings = secondary.deleted_settings
            deleted_alert_dedupe = secondary.deleted_alert_dedupe
            deleted_transfer_missing = secondary.deleted_transfer_missing
            deleted_cmd_dedupe = secondary.deleted_cmd_dedupe
        else:
            cutoff = taipei_cutoff_str(args.days)
            if has_exp:
                _log(f"正在刪除 {cutoff} 之前的 exp_history…")
                deleted_exp = conn.execute(
                    """
                    DELETE FROM exp_history
                    WHERE record_time < ?
                    """,
                    (cutoff,),
                ).rowcount
                _log(f"  已刪 {deleted_exp:,} 筆")
            if has_transfer:
                deleted_transfer = conn.execute(
                    """
                    DELETE FROM transfer_alerts_log
                    WHERE alert_time < ?
                    """,
                    (cutoff,),
                ).rowcount
            if has_settings:
                deleted_settings = conn.execute(
                    PRUNE_DEDUPE_SQL,
                    (
                        overspeed_prune_bound(cutoff),
                        boss_reminder_prune_bound(cutoff[:10]),
                    ),
                ).rowcount
            if has_alert_dedupe:
                deleted_alert_dedupe = conn.execute(
                    PRUNE_ALERT_DEDUPE_SQL, (cutoff,)
                ).rowcount
            if has_transfer_missing:
                from services.transfer_missing import prune_stale_missing_sync

                deleted_transfer_missing = prune_stale_missing_sync(
                    conn, before=cutoff
                )
            if has_cmd_dedupe:
                deleted_cmd_dedupe = prune_command_dedupe_sync(conn, days=2)

        conn.commit()
        middle_note = (
            f"（含轉移窗中間 {deleted_middle:,}）" if deleted_middle else ""
        )
        _log(
            f"已刪除：exp_history {deleted_exp + deleted_middle:,}{middle_note}、"
            f"transfer_alerts_log {deleted_transfer:,}、"
            f"bot_settings 去重 {deleted_settings:,}、"
            f"alert_dedupe {deleted_alert_dedupe:,}、"
            f"transfer_missing {deleted_transfer_missing:,}、"
            f"cmd_dedupe {deleted_cmd_dedupe:,}"
        )

        if (
            deleted_exp == 0
            and deleted_middle == 0
            and deleted_transfer == 0
            and deleted_settings == 0
            and deleted_alert_dedupe == 0
            and deleted_transfer_missing == 0
            and deleted_cmd_dedupe == 0
            and args.no_vacuum
        ):
            _log("沒有可刪資料，略過。")
            return 0

        if not args.no_vacuum:
            _log("正在 checkpoint + VACUUM（大庫可能需數十分鐘）…")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            _log("VACUUM 完成。")
        else:
            _log("已跳過 VACUUM（檔案大小可能尚未縮小）。")

        # 清庫後預設建索引；也可用 --build-indexes 強制再跑一次
        if args.build_indexes or args.for_search:
            _log("正在建立尋人索引（大庫可能需數分鐘）…")
            created = build_search_indexes_sync(conn)
            _log(
                f"索引：新建 {len(created)} 個"
                + (f"（{', '.join(created)}）" if created else "（原本已齊全）")
            )

        if has_exp:
            _log("正在重建 player_profile…")
            n_prof = rebuild_player_profiles_sync(conn)
            _log(f"player_profile：{n_prof:,} 筆")
    except Exception as e:
        import traceback

        _log(f"清理異常中止：{type(e).__name__}: {e}")
        traceback.print_exc()
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return 1
    finally:
        conn.close()

    after = _file_size(db_path)
    after_total = after + _file_size(wal) + _file_size(shm)
    _log(f"清理後大小：{_fmt_bytes(after)}（含 WAL/SHM 約 {_fmt_bytes(after_total)}）")
    if after < before:
        _log(f"回收約 {_fmt_bytes(before - after)}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
