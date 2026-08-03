"""參數解析與機密頻道 ACL 測試。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.ext import commands

from services.command_args import (
    BadArg,
    parse_alert_toggle,
    parse_count_server,
    parse_rank_args,
    parse_rank_filters,
)
from services.error_handler import (
    CHANNEL_DENIED,
    allowed_channel,
    require_allowed_channel,
)


def test_parse_count_server_defaults():
    cs = parse_count_server([])
    assert cs.count == 10
    assert cs.server == "全服"
    assert cs.is_global is True


def test_parse_count_server_with_count():
    cs = parse_count_server(["25", "全服"])
    assert cs.count == 25
    assert cs.is_global is True


def test_parse_count_server_clamps():
    cs = parse_count_server(["999"], max_count=100)
    assert cs.count == 100


def test_parse_count_server_bad_server():
    with pytest.raises(BadArg):
        parse_count_server(["不存在的服"])


def test_parse_rank_args_class_only():
    cs, job = parse_rank_args(["幻影劍士"])
    assert cs.count == 10
    assert cs.is_global is True
    assert job == "幻影劍士"


def test_parse_rank_filters_ok():
    f = parse_rank_filters("咒文+討伐50+等級60")
    assert f.class_filter == "咒文"
    assert f.grade_filter == 50
    assert f.level_filter == 60


def test_parse_rank_filters_bad_grade():
    with pytest.raises(BadArg):
        parse_rank_filters("討伐abc")


def test_parse_alert_toggle_on():
    state, cs = parse_alert_toggle(["開", "40", "萊涅01", "守護者"])
    assert state == "開"
    assert cs.count == 40
    assert cs.server == "萊涅01"
    assert cs.is_global is False
    assert cs.rest == ("守護者",)


def test_parse_alert_toggle_bad_server_message():
    with pytest.raises(BadArg, match="找不到伺服器"):
        parse_alert_toggle(["開", "40", "不存在的服", "守護者"])


def test_parse_alert_toggle_rejects_placeholder_guild():
    with pytest.raises(BadArg, match="伺服器與旅團"):
        parse_alert_toggle(["開", "40", "萊涅01", "未知"])


@pytest.mark.parametrize(
    "args",
    [
        ["開", "40", "全服", "守護者"],
        ["開", "40", "萊涅01"],
    ],
)
def test_parse_alert_toggle_requires_server_and_guild(args):
    with pytest.raises(BadArg):
        parse_alert_toggle(args)


def test_parse_alert_toggle_off():
    state, _ = parse_alert_toggle(["關"])
    assert state == "關"


def test_parse_alert_toggle_bad():
    with pytest.raises(BadArg):
        parse_alert_toggle(["亂輸入"])


@pytest.mark.asyncio
async def test_require_allowed_channel_denies_when_empty(monkeypatch):
    monkeypatch.delenv("ALLOWED_COMMAND_CHANNELS", raising=False)
    ctx = MagicMock()
    ctx.channel = MagicMock()
    ctx.channel.id = 123
    ctx.channel.parent_id = None
    ctx.channel.parent = None
    ctx.send = AsyncMock()
    assert await require_allowed_channel(ctx) is False
    ctx.send.assert_awaited()


@pytest.mark.asyncio
async def test_require_allowed_channel_allows_listed(monkeypatch):
    monkeypatch.setenv("ALLOWED_COMMAND_CHANNELS", "123,456")
    ctx = MagicMock()
    ctx.channel = MagicMock()
    ctx.channel.id = 123
    ctx.channel.parent_id = None
    ctx.channel.parent = None
    ctx.send = AsyncMock()
    assert await require_allowed_channel(ctx) is True
    ctx.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_require_allowed_channel_allows_extra_ids(monkeypatch):
    monkeypatch.setenv("ALLOWED_COMMAND_CHANNELS", "111")
    ctx = MagicMock()
    ctx.channel = MagicMock()
    ctx.channel.id = 999
    ctx.channel.parent_id = None
    ctx.channel.parent = None
    ctx.send = AsyncMock()
    assert await require_allowed_channel(ctx, extra_channel_ids=[999]) is True
    ctx.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowed_channel_extra_env(monkeypatch):
    monkeypatch.setenv("ALLOWED_COMMAND_CHANNELS", "111")
    monkeypatch.setenv("TRANSFER_ALERT_CHANNEL_ID", "999")

    @allowed_channel("TRANSFER_ALERT_CHANNEL_ID")
    async def dummy(ctx):
        return "ok"

    ctx = MagicMock()
    ctx.channel = MagicMock()
    ctx.channel.id = 999
    ctx.channel.parent_id = None
    ctx.channel.parent = None
    ctx.send = AsyncMock()

    checks = dummy.__commands_checks__
    assert await checks[0](ctx) is True
    ctx.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowed_channel_decorator_raises(monkeypatch):
    monkeypatch.delenv("ALLOWED_COMMAND_CHANNELS", raising=False)

    @allowed_channel()
    async def dummy(ctx):
        return "ok"

    ctx = MagicMock()
    ctx.channel = MagicMock()
    ctx.channel.id = 1
    ctx.channel.parent_id = None
    ctx.channel.parent = None
    ctx.send = AsyncMock()

    # discord.py wraps checks; invoke predicate via the check list
    checks = dummy.__commands_checks__
    assert checks
    with pytest.raises(commands.CheckFailure) as exc:
        await checks[0](ctx)
    assert str(exc.value) == CHANNEL_DENIED
