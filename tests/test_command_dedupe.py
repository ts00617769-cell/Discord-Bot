"""prefix 與 slash/hybrid 都有穩定 invoke ID；失敗時釋放 claim。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot import invoke_dedupe_id, release_command_claim_on_failure


def test_invoke_dedupe_id_prefers_interaction():
    ctx = SimpleNamespace(
        interaction=SimpleNamespace(id=222),
        message=SimpleNamespace(id=111),
    )
    assert invoke_dedupe_id(ctx) == 222


def test_invoke_dedupe_id_uses_message():
    ctx = SimpleNamespace(interaction=None, message=SimpleNamespace(id=111))
    assert invoke_dedupe_id(ctx) == 111


@pytest.mark.asyncio
async def test_release_command_claim_on_failure():
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    ctx = SimpleNamespace(
        _cmd_dedupe_claimed=True,
        _cmd_dedupe_id=999,
        command_failed=True,
        bot=SimpleNamespace(db=db),
    )
    await release_command_claim_on_failure(ctx)
    db.execute.assert_awaited()
    assert db.execute.await_args.args[0].startswith("DELETE FROM cmd_dedupe")
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_release_command_claim_skipped_on_success():
    db = MagicMock()
    db.execute = AsyncMock()
    ctx = SimpleNamespace(
        _cmd_dedupe_claimed=True,
        _cmd_dedupe_id=999,
        command_failed=False,
        bot=SimpleNamespace(db=db),
    )
    await release_command_claim_on_failure(ctx)
    db.execute.assert_not_awaited()
