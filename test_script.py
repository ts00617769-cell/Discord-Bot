import asyncio
from unittest.mock import MagicMock, AsyncMock
from cogs.exp_tracker import ExpTracker

class DummyBot:
    def __init__(self):
        self.session = MagicMock()
        self.db = MagicMock()

bot = DummyBot()
tracker = ExpTracker(bot)

async def test_toggle_alerts():
    ctx = MagicMock()
    ctx.send = AsyncMock()

    # Need to call tracker.toggle_alerts.callback(tracker, ctx, ...) since it's wrapped in @commands.command
    await tracker.toggle_alerts.callback(tracker, ctx)
    ctx.send.assert_called()

    await tracker.toggle_alerts.callback(tracker, ctx, "開")
    assert tracker.alerts_enabled == True
    assert tracker.alert_count == 50
    assert tracker.alert_server == "全服"

    await tracker.toggle_alerts.callback(tracker, ctx, "關")
    assert tracker.alerts_enabled == False

    await tracker.toggle_alerts.callback(tracker, ctx, "開", "30", "戴摩爾克04")
    assert tracker.alerts_enabled == True
    assert tracker.alert_count == 30
    assert tracker.alert_server == "戴摩爾克04"

    print("All tests passed!")

asyncio.run(test_toggle_alerts())
