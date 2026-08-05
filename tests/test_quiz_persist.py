"""Quiz 發布 claim／回滾與開獎 finalize。"""
from __future__ import annotations

import datetime
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import discord
import pytest

from cogs.quiz import QuizSystem, _empty_poll
from db.schema import apply_migrations
from services.timeutil import TAIPEI

QUESTION = {
    "title": "測試題目",
    "options": {"A": "甲", "B": "乙"},
    "results": {"A": "結果甲", "B": "結果乙"},
}


def _cog(bot) -> QuizSystem:
    cog = QuizSystem.__new__(QuizSystem)
    cog.bot = bot
    cog.quiz_data = [QUESTION]
    cog.active_poll = _empty_poll()
    cog.post_time = datetime.time(12, 0, tzinfo=TAIPEI)
    cog.reveal_time = datetime.time(18, 0, tzinfo=TAIPEI)
    return cog


@pytest.mark.asyncio
async def test_quiz_claim_and_rollback_are_consistent(tmp_path):
    db = await aiosqlite.connect(tmp_path / "quiz.db")
    try:
        await apply_migrations(db)
        cog = _cog(SimpleNamespace(db=db))
        assert await cog.claim_quiz_post(123, "2026-08-05", QUESTION) is True
        assert await cog.claim_quiz_post(123, "2026-08-05", QUESTION) is False

        async with db.execute(
            "SELECT is_active, quiz_id FROM active_quiz_status WHERE id=1"
        ) as cursor:
            assert await cursor.fetchone() == (1, QUESTION["title"])
        async with db.execute(
            "SELECT quiz_id FROM quiz_history WHERE quiz_id=?", (QUESTION["title"],)
        ) as cursor:
            assert await cursor.fetchone() == (QUESTION["title"],)

        await cog.rollback_quiz_post("2026-08-05", QUESTION)
        async with db.execute(
            "SELECT is_active FROM active_quiz_status WHERE id=1"
        ) as cursor:
            assert await cursor.fetchone() == (0,)
        async with db.execute(
            "SELECT 1 FROM quiz_history WHERE quiz_id=?", (QUESTION["title"],)
        ) as cursor:
            assert await cursor.fetchone() is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_auto_post_rolls_back_claim_when_discord_send_fails():
    bot = MagicMock()
    cog = _cog(bot)
    channel = MagicMock(id=123)
    response = MagicMock(status=500)
    channel.send = AsyncMock(side_effect=discord.HTTPException(response, "fail"))
    order: list[str] = []

    async def claim(*_args):
        order.append("claim")
        return True

    async def rollback(*_args):
        order.append("rollback")

    cog.get_unrepeated_quiz = AsyncMock(return_value=QUESTION)
    cog.claim_quiz_post = AsyncMock(side_effect=claim)
    cog.rollback_quiz_post = AsyncMock(side_effect=rollback)

    with patch(
        "cogs.quiz.now_taipei",
        return_value=datetime.datetime(2026, 8, 5, 12, 0, tzinfo=TAIPEI),
    ), patch("cogs.quiz.parse_env_channel_id", return_value=123), patch(
        "cogs.quiz.resolve_bot_channel",
        new_callable=AsyncMock,
        return_value=channel,
    ):
        await QuizSystem.auto_post_quiz.coro(cog)

    assert order == ["claim", "rollback"]
    assert cog.active_poll["is_active"] is False


@pytest.mark.asyncio
async def test_finalize_reveal_retries_then_clears_memory():
    cog = _cog(MagicMock())
    cog._start_poll(123, "2026-08-05", QUESTION)
    cog.clear_active_status = AsyncMock(
        side_effect=[
            sqlite3.DatabaseError("busy"),
            sqlite3.DatabaseError("busy"),
            None,
        ]
    )
    cog.bot.db.rollback = AsyncMock()

    with patch("cogs.quiz.asyncio.sleep", new_callable=AsyncMock) as sleep:
        await cog._finalize_reveal()

    assert cog.clear_active_status.await_count == 3
    assert sleep.await_count == 2
    assert cog.active_poll["is_active"] is False


@pytest.mark.asyncio
async def test_check_active_quiz_resume_restores_poll(tmp_path):
    db = await aiosqlite.connect(tmp_path / "resume.db")
    try:
        await apply_migrations(db)
        await db.execute(
            """
            INSERT INTO active_quiz_status (id, is_active, quiz_id, channel_id, date_str)
            VALUES (1, 1, ?, 99, '2026-08-05')
            ON CONFLICT(id) DO UPDATE SET
              is_active=excluded.is_active,
              quiz_id=excluded.quiz_id,
              channel_id=excluded.channel_id,
              date_str=excluded.date_str
            """,
            (QUESTION["title"],),
        )
        await db.execute(
            "INSERT INTO quiz_votes (user_id, user_name, choice) VALUES (1, 'Alice', 'A')"
        )
        await db.commit()

        bot = MagicMock()
        bot.db = db
        bot.add_view = MagicMock()
        cog = _cog(bot)
        await cog.check_active_quiz_resume()

        assert cog.active_poll["is_active"] is True
        assert cog.active_poll["channel_id"] == 99
        assert cog.active_poll["data"]["title"] == QUESTION["title"]
        assert list(cog.active_poll["votes"].values())[0]["choice"] == "A"
        bot.add_view.assert_called_once()
    finally:
        await db.close()
