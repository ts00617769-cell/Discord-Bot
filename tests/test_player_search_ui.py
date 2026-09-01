from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from cogs.player_search import PlayerSearchFeedbackView


@pytest.mark.asyncio
async def test_player_search_feedback_view_interaction_check():
    view = PlayerSearchFeedbackView(author_id=123)

    # Matching author
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user.id = 123
    interaction.response.send_message = AsyncMock()

    result = await view.interaction_check(interaction)
    assert result is True
    interaction.response.send_message.assert_not_called()

    # Different author
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user.id = 456
    interaction.response.send_message = AsyncMock()

    result = await view.interaction_check(interaction)
    assert result is False
    interaction.response.send_message.assert_called_once_with(
        "只有發起查詢的玩家可以回報。", ephemeral=True
    )

@pytest.mark.asyncio
async def test_player_search_feedback_view_handle_feedback():
    view = PlayerSearchFeedbackView(author_id=123)

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user.id = 123
    interaction.response.edit_message = AsyncMock()
    interaction.followup.send = AsyncMock()

    await view._handle_feedback(interaction, "正確")

    for child in view.children:
        assert child.disabled is True

    interaction.response.edit_message.assert_called_once_with(view=view)
    interaction.followup.send.assert_called_once_with(
        "感謝您的回報！已記錄此結果。", ephemeral=True
    )
