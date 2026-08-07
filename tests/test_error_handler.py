"""頻道白名單、環境變數解析與指令錯誤上報。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ext import commands

import services.error_handler as error_handler
from services.error_handler import (
    CANNOT_SEND,
    bot_can_send_in_channel,
    get_allowed_command_channels,
    handle_command_error,
    is_allowed_command_channel,
    is_forbidden_error,
    parse_env_channel_ids,
    parse_env_float,
)


@pytest.fixture(autouse=True)
def _clear_permission_report_cooldown():
    error_handler._perm_report_last.clear()


def test_parse_env_channel_ids_skips_junk(monkeypatch):
    monkeypatch.setenv("ALLOWED_COMMAND_CHANNELS", "123, abc, 456,,")
    assert parse_env_channel_ids(env_name="ALLOWED_COMMAND_CHANNELS") == [123, 456]


def test_fail_closed_when_allowlist_empty(monkeypatch):
    monkeypatch.delenv("ALLOWED_COMMAND_CHANNELS", raising=False)
    assert get_allowed_command_channels() == []
    assert is_allowed_command_channel(999) is False


def test_allowlist_permits_listed_channel(monkeypatch):
    monkeypatch.setenv("ALLOWED_COMMAND_CHANNELS", "111,222")
    assert is_allowed_command_channel(111) is True
    assert is_allowed_command_channel(333) is False


def test_parse_env_float_fallback(monkeypatch):
    monkeypatch.setenv("EXP_ALERT_THRESHOLD", "not-a-number")
    assert parse_env_float("EXP_ALERT_THRESHOLD", 42.0) == 42.0
    monkeypatch.setenv("EXP_ALERT_THRESHOLD", "1.5")
    assert parse_env_float("EXP_ALERT_THRESHOLD", 42.0) == 1.5


@pytest.mark.asyncio
async def test_handle_command_error_reports_via_discord_send():
    ctx = MagicMock()
    ctx.send = AsyncMock()
    ctx.channel.id = 42
    ctx.command = SimpleNamespace(name="尋人", qualified_name="尋人")
    bot = MagicMock()
    error = RuntimeError("boom")

    with patch(
        "services.discord_send.send_text_to_channels",
        new_callable=AsyncMock,
        return_value={99},
    ) as send:
        handled = await handle_command_error(
            ctx, error, log_channel_id=99, bot=bot
        )

    assert handled is True
    ctx.send.assert_awaited()
    send.assert_awaited_once()
    assert send.await_args.args[1] == [99]
    assert "系統報錯" in send.await_args.args[2]
    assert "尋人" in send.await_args.args[2]


def _forbidden(
    message: str = "Missing Access", *, code: int = 50001
) -> discord.Forbidden:
    response = SimpleNamespace(status=403, reason="Forbidden")
    return discord.Forbidden(response, {"code": code, "message": message})


def test_is_forbidden_error_walks_cause_chain():
    try:
        try:
            raise _forbidden()
        except discord.Forbidden as e:
            raise commands.CommandInvokeError(e) from e
    except commands.CommandInvokeError as wrapped:
        assert is_forbidden_error(wrapped) is True
    assert is_forbidden_error(RuntimeError("boom")) is False
    assert is_forbidden_error(_forbidden("Missing Permissions", code=50013)) is False


def _perms(**overrides):
    values = {
        "view_channel": True,
        "send_messages": True,
        "send_messages_in_threads": True,
        "manage_threads": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_bot_can_send_in_channel_detects_missing_permission():
    allowed = MagicMock(spec=discord.TextChannel)
    allowed.permissions_for.return_value = _perms()
    assert bot_can_send_in_channel(allowed, MagicMock()) is True

    muted = MagicMock(spec=discord.TextChannel)
    muted.permissions_for.return_value = _perms(send_messages=False)
    assert bot_can_send_in_channel(muted, MagicMock()) is False

    hidden = MagicMock(spec=discord.TextChannel)
    hidden.permissions_for.return_value = _perms(view_channel=False)
    assert bot_can_send_in_channel(hidden, MagicMock()) is False


def test_bot_can_send_in_channel_rejects_unjoined_private_thread():
    thread = MagicMock(spec=discord.Thread)
    thread.permissions_for.return_value = _perms()
    thread.is_private.return_value = True
    thread.me = None
    thread.locked = False
    thread.archived = False

    assert bot_can_send_in_channel(thread, MagicMock()) is False


def test_bot_can_send_in_channel_allows_when_undeterminable():
    assert bot_can_send_in_channel(SimpleNamespace(id=1), MagicMock()) is True
    assert bot_can_send_in_channel(MagicMock(spec=discord.TextChannel), None) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        commands.CommandInvokeError(_forbidden()),
        commands.CheckFailure(CANNOT_SEND),
    ],
)
async def test_missing_permission_reports_summary_without_traceback(error):
    ctx = MagicMock()
    ctx.send = AsyncMock()
    ctx.channel.id = 4242
    ctx.channel.name = "測速區"
    ctx.command = SimpleNamespace(name="警報", qualified_name="警報")

    with patch(
        "services.discord_send.send_text_to_channels",
        new_callable=AsyncMock,
        return_value={99},
    ) as send:
        handled = await handle_command_error(
            ctx, error, log_channel_id=99, bot=MagicMock()
        )

    assert handled is True
    ctx.send.assert_not_awaited()
    send.assert_awaited_once()
    report = send.await_args.args[2]
    assert "權限不足" in report
    assert "警報" in report
    assert "Traceback" not in report


@pytest.mark.asyncio
async def test_missing_permission_report_is_rate_limited():
    ctx = MagicMock()
    ctx.send = AsyncMock()
    ctx.channel.id = 777
    ctx.channel.name = "討論串"
    ctx.command = SimpleNamespace(name="警報", qualified_name="警報")

    with patch(
        "services.discord_send.send_text_to_channels",
        new_callable=AsyncMock,
        return_value={99},
    ) as send:
        for _ in range(3):
            await handle_command_error(
                ctx,
                commands.CommandInvokeError(_forbidden()),
                log_channel_id=99,
                bot=MagicMock(),
            )

    assert send.await_count == 1
