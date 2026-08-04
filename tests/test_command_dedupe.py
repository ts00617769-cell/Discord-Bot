"""prefix 與 slash/hybrid 都有穩定 invoke ID。"""
from __future__ import annotations

from types import SimpleNamespace

from bot import invoke_dedupe_id


def test_invoke_dedupe_id_prefers_interaction():
    ctx = SimpleNamespace(
        interaction=SimpleNamespace(id=222),
        message=SimpleNamespace(id=111),
    )
    assert invoke_dedupe_id(ctx) == 222


def test_invoke_dedupe_id_uses_message():
    ctx = SimpleNamespace(interaction=None, message=SimpleNamespace(id=111))
    assert invoke_dedupe_id(ctx) == 111
