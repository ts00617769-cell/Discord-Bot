"""本機單例檔案鎖（bot 啟動占用；cleanup 偵測是否佔用）。"""
from __future__ import annotations

import atexit
import os
import sys
from pathlib import Path

_lock_fp = None


def default_lock_path(base_dir: str | Path | None = None) -> Path:
    root = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent
    return root / ".bot.lock"


def acquire_process_lock(lock_path: str | Path | None = None) -> None:
    """以作業系統檔案鎖阻擋同一台機器的第二個實例；失敗則 sys.exit(1)。"""
    global _lock_fp
    path = Path(lock_path) if lock_path else default_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _lock_fp = open(path, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            _lock_fp.seek(0)
            try:
                msvcrt.locking(_lock_fp.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                print("❌ 偵測到本機已有機器人實例正在執行（檔案鎖）。")
                print("   請先結束舊的 python 程序，否則指令會回覆兩次。")
                print("   可執行: taskkill /IM python.exe /F")
                sys.exit(1)
        else:
            import fcntl

            try:
                fcntl.flock(_lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                print("❌ 偵測到本機已有機器人實例正在執行（檔案鎖）。")
                print("   請先結束舊程序，否則指令會回覆兩次。")
                sys.exit(1)
    except OSError as e:
        print(f"❌ 無法取得本機單例鎖，拒絕啟動: {e}")
        print("   請確認 .bot.lock 可寫入，並結束其他 python 實例後重試。")
        sys.exit(1)

    _lock_fp.seek(0)
    _lock_fp.truncate()
    _lock_fp.write(str(os.getpid()))
    _lock_fp.flush()

    def _release() -> None:
        global _lock_fp
        try:
            if _lock_fp:
                if os.name == "nt":
                    try:
                        import msvcrt

                        _lock_fp.seek(0)
                        msvcrt.locking(_lock_fp.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    try:
                        import fcntl

                        fcntl.flock(_lock_fp.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                _lock_fp.close()
                _lock_fp = None
        except OSError:
            pass

    atexit.register(_release)


def _pid_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def pid_looks_like_bot(pid: int) -> bool:
    """PID 存活且指令列像本專案 bot。"""
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
            return True
        os.kill(pid, 0)
    except OSError:
        return False
    cmd = _pid_cmdline(pid).lower()
    if not cmd:
        return False
    markers = ("bot.py", "dc_bot", "prasia", "discord")
    return any(m in cmd for m in markers)


def is_process_lock_held(lock_path: str | Path | None = None) -> bool:
    """是否真有行程佔用 .bot.lock（與 acquire_process_lock 同一把鎖）。"""
    path = Path(lock_path) if lock_path else default_lock_path()
    if not path.exists():
        return False

    try:
        with open(path, "a+", encoding="utf-8") as fp:
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
            return False
    except OSError:
        pass

    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw.isdigit():
            return False
        return pid_looks_like_bot(int(raw))
    except (OSError, ValueError):
        return False
