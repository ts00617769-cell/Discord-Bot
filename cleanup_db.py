"""
離線清理 prasia_data.db：刪除過期 exp_history / transfer_alerts_log，並 VACUUM 回收磁碟空間。

使用前請先停止 bot（VACUUM 需要獨占存取）。

範例：
  python cleanup_db.py
  python cleanup_db.py --days 60
  python cleanup_db.py --days 30 --dry-run
  python cleanup_db.py --for-search
  python cleanup_db.py --for-search --dry-run
  python cleanup_db.py --build-indexes   # 離線建立尋人索引 + player_profile（大庫必做）
  python cleanup_db.py --wipe-history
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOCK_PATH = ROOT / ".bot.lock"
DEFAULT_DAYS = 60

# 允許直接執行：把專案根加入 path
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.schema import (  # noqa: E402
    build_search_indexes_sync,
    list_missing_search_indexes,
    rebuild_player_profiles_sync,
)
from services.retention_windows import (  # noqa: E402
    DEFAULT_MAX_TRANSFER_WINDOWS,
    DEFAULT_RECENT_DAYS,
    DEFAULT_TRANSFER_PAD_DAYS,
    build_search_keep_ranges,
    exp_history_outside_keep_sql,
    search_retention_cutoff,
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


def resolve_db_path(base_dir: Path | None = None) -> Path:
    """與 db.connection.resolve_db_path 相同，避免為清理腳本拉入 aiosqlite。"""
    env = (os.getenv("DB_PATH") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    root = base_dir if base_dir is not None else ROOT
    return (root / "prasia_data.db").resolve()


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


def _pid_cmdline(pid: int) -> str:
    """讀取 Linux /proc 指令列；失敗回空字串。"""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def _pid_looks_like_bot(pid: int) -> bool:
    """PID 存活且指令列像本專案 bot（避免 Docker 容器 PID 撞上 host 無關行程）。"""
    try:
        if sys.platform == "win32":
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            # Windows 難可靠讀 cmdline；有行程就算可疑，交由 --force 覆寫
            return True
        os.kill(pid, 0)
    except OSError:
        return False
    cmd = _pid_cmdline(pid).lower()
    if not cmd:
        return False
    markers = ("bot.py", "dc_bot", "prasia", "discord")
    return any(m in cmd for m in markers)


def _bot_appears_running() -> bool:
    """是否真有 bot 佔用 .bot.lock。

    優先試 OS 檔案鎖（與 bot.py 相同）：容器已停時 flock 會釋放，
    即使殘留 PID 文字也不應擋清理。PID 檢查僅作備援，且須像本 bot。
    """
    if not LOCK_PATH.exists():
        return False

    try:
        with open(LOCK_PATH, "a+", encoding="utf-8") as fp:
            if sys.platform == "win32":
                import msvcrt

                fp.seek(0)
                try:
                    msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    return True
                try:
                    msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
                return False

            import fcntl

            try:
                fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            try:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            # 鎖得下來＝沒人佔用；殘留 PID 視為過期
            return False
    except OSError:
        pass

    # flock 不可用時才看 PID
    try:
        raw = LOCK_PATH.read_text(encoding="utf-8").strip()
        if not raw.isdigit():
            return False
        return _pid_looks_like_bot(int(raw))
    except (OSError, ValueError):
        return False


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


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
            "（窗開始～結束後 pad 天），其餘刪除"
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

    if _bot_appears_running() and not args.force:
        print(
            f"錯誤：偵測到 bot 仍佔用 {LOCK_PATH.name}（檔案鎖）。\n"
            "請先在 Docker 停止 prasia-bot-final，或確認已停後加 --force。\n"
            "（若容器已停仍出現此訊息，多半是舊版誤判殘留 PID；請 git pull 後重試。）",
            file=sys.stderr,
        )
        return 1

    before = _file_size(db_path)
    wal = Path(str(db_path) + "-wal")
    shm = Path(str(db_path) + "-shm")
    before_total = before + _file_size(wal) + _file_size(shm)

    print(f"資料庫：{db_path}")
    print(f"清理前大小：{_fmt_bytes(before)}（含 WAL/SHM 約 {_fmt_bytes(before_total)}）")

    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        has_exp = _table_exists(conn, "exp_history")
        has_transfer = _table_exists(conn, "transfer_alerts_log")
        has_settings = _table_exists(conn, "bot_settings")
        has_alert_dedupe = _table_exists(conn, "alert_dedupe")

        if indexes_only:
            if args.dry_run:
                missing = list_missing_search_indexes(conn)
                print("模式：僅檢查尋人索引（dry-run）")
                print(f"將建立：{', '.join(missing) if missing else '（已齊全）'}")
                return 0
            print("模式：僅建立尋人索引")
            created = build_search_indexes_sync(conn)
            print(
                f"索引：新建 {len(created)} 個"
                + (f"（{', '.join(created)}）" if created else "（原本已齊全）")
            )
            if has_exp:
                print("正在重建 player_profile…")
                n_prof = rebuild_player_profiles_sync(conn)
                print(f"player_profile：{n_prof:,} 筆")
            return 0

        exp_total = _count(conn, "SELECT COUNT(*) FROM exp_history") if has_exp else 0
        transfer_total = (
            _count(conn, "SELECT COUNT(*) FROM transfer_alerts_log") if has_transfer else 0
        )

        keep_ranges: list[tuple[str, str]] = []
        exp_delete_sql = ""
        exp_delete_params: tuple[str, ...] = ()
        settings_cutoff: str | None = None

        if args.wipe_history:
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
            mode = "清空全部歷史"
        elif args.for_search:
            keep_ranges = build_search_keep_ranges(
                recent_days=args.recent_days,
                pad_days=args.transfer_pad_days,
                max_transfer_windows=args.max_transfer_windows,
            )
            count_sql, count_params = exp_history_outside_keep_sql(keep_ranges)
            exp_delete_sql, exp_delete_params = exp_history_outside_keep_sql(
                keep_ranges, for_delete=True
            )
            exp_stale = _count(conn, count_sql, count_params) if has_exp else 0
            # transfer / settings / alert_dedupe：與「最近 N 天」對齊
            settings_cutoff = search_retention_cutoff(
                recent_days=args.recent_days,
                pad_days=args.transfer_pad_days,
                max_transfer_windows=args.max_transfer_windows,
            )
            transfer_stale = (
                _count(
                    conn,
                    """
                    SELECT COUNT(*) FROM transfer_alerts_log
                    WHERE alert_time < ?
                    """,
                    (settings_cutoff,),
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
            mode = (
                f"尋人導向（最近 {args.recent_days} 天 ∪ "
                f"最近 {args.max_transfer_windows} 次轉移窗"
                f"～結束後+{args.transfer_pad_days} 天）"
            )
        else:
            cutoff = taipei_cutoff_str(args.days)
            settings_cutoff = cutoff
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
            mode = f"保留最近 {args.days} 天（截止 {cutoff} 台北）"

        print(f"模式：{mode}")
        if keep_ranges:
            print("保留區間：")
            for start, end in keep_ranges:
                print(f"  {start}  ~  {end}")
        print(f"exp_history：共 {exp_total:,} 筆，將刪 {exp_stale:,} 筆")
        print(f"transfer_alerts_log：共 {transfer_total:,} 筆，將刪 {transfer_stale:,} 筆")
        if has_settings:
            print(f"bot_settings 去重 key：將刪 {settings_stale:,} 筆")
        else:
            print("bot_settings：表不存在，略過")
        if has_alert_dedupe:
            print(f"alert_dedupe：將刪 {alert_dedupe_stale:,} 筆")
        else:
            print("alert_dedupe：表不存在，略過")

        if args.dry_run:
            print("dry-run：未修改資料庫。")
            return 0

        if (
            exp_stale == 0
            and transfer_stale == 0
            and settings_stale == 0
            and alert_dedupe_stale == 0
            and args.no_vacuum
        ):
            print("沒有可刪資料，略過。")
            return 0

        deleted_exp = 0
        deleted_transfer = 0
        deleted_settings = 0
        deleted_alert_dedupe = 0
        if args.wipe_history:
            if has_exp:
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
        elif args.for_search:
            if has_exp and exp_delete_sql:
                deleted_exp = conn.execute(exp_delete_sql, exp_delete_params).rowcount
            assert settings_cutoff is not None
            if has_transfer:
                deleted_transfer = conn.execute(
                    """
                    DELETE FROM transfer_alerts_log
                    WHERE alert_time < ?
                    """,
                    (settings_cutoff,),
                ).rowcount
            if has_settings:
                deleted_settings = conn.execute(
                    PRUNE_DEDUPE_SQL,
                    (
                        overspeed_prune_bound(settings_cutoff),
                        boss_reminder_prune_bound(settings_cutoff[:10]),
                    ),
                ).rowcount
            if has_alert_dedupe:
                deleted_alert_dedupe = conn.execute(
                    PRUNE_ALERT_DEDUPE_SQL, (settings_cutoff,)
                ).rowcount
        else:
            cutoff = taipei_cutoff_str(args.days)
            if has_exp:
                deleted_exp = conn.execute(
                    """
                    DELETE FROM exp_history
                    WHERE record_time < ?
                    """,
                    (cutoff,),
                ).rowcount
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

        conn.commit()
        print(
            f"已刪除：exp_history {deleted_exp:,}、"
            f"transfer_alerts_log {deleted_transfer:,}、"
            f"bot_settings 去重 {deleted_settings:,}、"
            f"alert_dedupe {deleted_alert_dedupe:,}"
        )

        if not args.no_vacuum:
            print("正在 checkpoint + VACUUM（大庫可能需數分鐘）…")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            print("VACUUM 完成。")
        else:
            print("已跳過 VACUUM（檔案大小可能尚未縮小）。")

        # 清庫後預設建索引；也可用 --build-indexes 強制再跑一次
        if args.build_indexes or args.for_search:
            print("正在建立尋人索引（大庫可能需數分鐘）…")
            created = build_search_indexes_sync(conn)
            print(
                f"索引：新建 {len(created)} 個"
                + (f"（{', '.join(created)}）" if created else "（原本已齊全）")
            )

        if has_exp:
            print("正在重建 player_profile…")
            n_prof = rebuild_player_profiles_sync(conn)
            print(f"player_profile：{n_prof:,} 筆")
    finally:
        conn.close()

    after = _file_size(db_path)
    after_total = after + _file_size(wal) + _file_size(shm)
    print(f"清理後大小：{_fmt_bytes(after)}（含 WAL/SHM 約 {_fmt_bytes(after_total)}）")
    if after < before:
        print(f"回收約 {_fmt_bytes(before - after)}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
