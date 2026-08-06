"""頻道白名單、環境變數解析與指令錯誤上報。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.error_handler import (
    get_allowed_command_channels,
    handle_command_error,
    is_allowed_command_channel,
    parse_env_channel_ids,
    parse_env_float,
)


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
