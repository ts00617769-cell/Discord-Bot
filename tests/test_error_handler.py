"""頻道白名單與環境變數解析。"""
from __future__ import annotations

from cogs.error_handler import (
    get_allowed_command_channels,
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
