"""共用指令參數解析（數量、伺服器、職業篩選）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from discord.ext import commands

from game_data import SERVER_MAP
from services.exp_snapshots import normalize_guild

GLOBAL_ALIASES = frozenset({"全服", "全部", "global", ""})


class BadArg(commands.UserInputError):
    """使用者參數無法解析時拋出（由 WarRoom / on_command_error 回覆）。"""


@dataclass(frozen=True)
class CountServer:
    count: int
    server: str  # 正規化後：「全服」或 SERVER_MAP key
    is_global: bool
    rest: tuple[str, ...] = ()


@dataclass(frozen=True)
class RankFilters:
    class_filter: str = ""
    grade_filter: int = 0
    level_filter: int = 0


def clamp_count(value: int, *, default: int = 10, lo: int = 1, hi: int = 100) -> int:
    return max(lo, min(hi, value))


def normalize_server(raw: str, *, allow_global: bool = True) -> str:
    """回傳「全服」或 SERVER_MAP 內名稱；無效則拋 BadArg。"""
    name = (raw or "").strip()
    if allow_global and (not name or name in GLOBAL_ALIASES):
        return "全服"
    if name in SERVER_MAP:
        return name
    if allow_global and name in GLOBAL_ALIASES:
        return "全服"
    valid = "、".join(SERVER_MAP.keys())
    hint = f"{valid} 或 全服" if allow_global else valid
    raise BadArg(f"❌ 找不到伺服器「{name}」。支援：{hint}")


def parse_count_server(
    args: Sequence[str],
    *,
    default_count: int = 10,
    max_count: int = 100,
    default_server: str = "全服",
    join_server_rest: bool = True,
) -> CountServer:
    """解析 `[數量] [伺服器…]`。

    - 若首參數為純數字 → 當作數量
    - 其餘合併為伺服器名（join_server_rest=True）或保留為 rest
    """
    parts = [a for a in args if str(a).strip()]
    count = default_count
    if parts and parts[0].isdigit():
        count = clamp_count(int(parts.pop(0)), default=default_count, hi=max_count)

    if join_server_rest:
        server_raw = "".join(parts) if parts else default_server
        rest: tuple[str, ...] = ()
    else:
        # 不合併：第一個非數字 token 若是伺服器就吃掉，其餘留給呼叫端
        server_raw = default_server
        rest = tuple(parts)
        if parts:
            if parts[0] in SERVER_MAP or parts[0] in GLOBAL_ALIASES:
                server_raw = parts[0]
                rest = tuple(parts[1:])
            else:
                # 拼錯伺服器時給出「找不到伺服器」，勿把 typo 當成旅團名
                normalize_server(parts[0], allow_global=True)

    server = normalize_server(server_raw, allow_global=True)
    return CountServer(
        count=count,
        server=server,
        is_global=server == "全服",
        rest=rest,
    )


def parse_rank_args(args: Sequence[str], *, default_count: int = 10) -> tuple[CountServer, Optional[str]]:
    """解析排名指令：`[數量] [伺服器] [職業…]`。

    伺服器 token 必須完整對應 SERVER_MAP／全服別名；其餘串成職業字串。
    """
    parts = [a for a in args if str(a).strip()]
    count = default_count
    if parts and parts[0].isdigit():
        count = clamp_count(int(parts.pop(0)), default=default_count)

    target_server = "全服"
    class_parts: list[str] = []
    for arg in parts:
        if arg in SERVER_MAP or arg in GLOBAL_ALIASES:
            target_server = "全服" if arg in GLOBAL_ALIASES else arg
        else:
            class_parts.append(arg)

    server = normalize_server(target_server, allow_global=True)
    target_class = "".join(class_parts) if class_parts else None
    return (
        CountServer(count=count, server=server, is_global=server == "全服"),
        target_class,
    )


def parse_rank_filters(target_class: Optional[str]) -> RankFilters:
    """解析職業字串內的 `職業X` / `討伐N` / `等級N` / `lv.N` 篩選。

    壞數字會拋 BadArg，不再 silently pass。
    """
    if not target_class:
        return RankFilters()

    class_filter = ""
    grade_filter = 0
    level_filter = 0
    for f in target_class.split("+"):
        f = f.strip()
        if not f:
            continue
        if f.startswith("職業"):
            class_filter = f[2:]
            continue
        if f.startswith("討伐"):
            raw = f[2:]
            try:
                grade_filter = int(raw)
            except ValueError as e:
                raise BadArg(f"❌ 討伐篩選無效：「{f}」（需為整數，例：討伐50）") from e
            continue
        low = f.lower()
        if f.startswith("等級"):
            raw = f[2:]
            try:
                level_filter = int(raw)
            except ValueError as e:
                raise BadArg(f"❌ 等級篩選無效：「{f}」（需為整數，例：等級60）") from e
            continue
        if low.startswith("lv."):
            raw = f[3:]
            try:
                level_filter = int(raw)
            except ValueError as e:
                raise BadArg(f"❌ 等級篩選無效：「{f}」（需為整數，例：lv.60）") from e
            continue
        if low.startswith("lv"):
            raw = f[2:]
            try:
                level_filter = int(raw)
            except ValueError as e:
                raise BadArg(f"❌ 等級篩選無效：「{f}」（需為整數，例：lv60）") from e
            continue
        if (
            not class_filter
            and not f.startswith("討伐")
            and not f.startswith("等級")
            and not low.startswith("lv")
        ):
            class_filter = f

    return RankFilters(
        class_filter=class_filter,
        grade_filter=grade_filter,
        level_filter=level_filter,
    )


def parse_alert_toggle(args: Sequence[str]) -> tuple[Optional[str], CountServer]:
    """解析 `!警報`：回傳 (state|None, CountServer)。

    開啟語法為 ``開 [數量] [伺服器] [旅團名稱]``；旅團名稱保留在
    ``CountServer.rest``。警報必須同時指定單一伺服器與旅團。
    """
    parts = [a for a in args if str(a).strip()]
    if not parts:
        return None, CountServer(count=30, server="全服", is_global=True)

    state = parts.pop(0)
    if state in ("關", "off"):
        return "關", CountServer(count=30, server="全服", is_global=True)
    if state in ("開", "on"):
        cs = parse_count_server(
            parts,
            default_count=30,
            max_count=100,
            join_server_rest=False,
        )
        guild = normalize_guild(" ".join(cs.rest))
        if cs.is_global or not guild:
            raise BadArg(
                "❌ 開啟警報時必須指定伺服器與旅團，"
                "例如：`!警報 開 50 萊涅01 旅團名稱`"
            )
        return "開", CountServer(
            count=cs.count,
            server=cs.server,
            is_global=False,
            rest=(guild,),
        )
    raise BadArg(
        "❌ 請輸入 `開` / `關`，例如：`!警報 開 50 萊涅01 旅團名稱`"
    )
