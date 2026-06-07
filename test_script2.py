import asyncio
from unittest.mock import MagicMock, AsyncMock
from cogs.exp_tracker import ExpTracker
import datetime

class DummyCursor:
    def __init__(self, records):
        self.records = records
    async def fetchall(self):
        return self.records

class DummyDBContextManager:
    def __init__(self, cursor):
        self.cursor = cursor
    async def __aenter__(self):
        return self.cursor
    async def __aexit__(self, exc_type, exc, tb):
        pass

class DummyBot:
    def __init__(self):
        self.session = MagicMock()
        self.db = MagicMock()
        self.channel = MagicMock()
        self.channel.send = AsyncMock()

    def get_channel(self, id):
        return self.channel

bot = DummyBot()

tracker = ExpTracker(bot)

async def test_check_for_alerts():
    tracker.alerts_enabled = True
    tracker.alert_count = 2
    tracker.alert_server = "全服"

    times = [("2023-10-01 12:10:00",), ("2023-10-01 12:00:00",)]

    records = [
        ("PlayerA", "ServerA", 50, 200_000_000_000, 100_000_000_000), # 100 billion diff -> hourly 600b
        ("PlayerB", "ServerB", 50, 150_000_000_000, 100_000_000_000), # 50 billion diff -> hourly 300b
        ("PlayerC", "ServerC", 50, 300_000_000_000, 100_000_000_000), # 200 billion diff -> hourly 1200b
    ]

    def mock_execute(sql, params=None):
        if "GROUP BY record_time" in sql:
            return DummyDBContextManager(DummyCursor(times))
        return DummyDBContextManager(DummyCursor(records))

    bot.db.execute = mock_execute

    await tracker.check_for_alerts("dummy")

    bot.channel.send.assert_called()
    call_args = bot.channel.send.call_args
    embed = call_args.kwargs['embed']
    desc = embed.description

    assert "PlayerC" in desc
    assert "PlayerA" in desc
    assert "PlayerB" not in desc # because alert_count = 2 and C > A > B
    assert "Top 2" in embed.title
    print("test_check_for_alerts passed!")

asyncio.run(test_check_for_alerts())
