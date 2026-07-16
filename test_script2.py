import asyncio
from unittest.mock import MagicMock, AsyncMock
from cogs.exp_tracker import ExpTracker

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
    tracker.ALERT_CHANNEL_IDS = [1]
    tracker.check_for_transfers = AsyncMock()

    # 半小時監控區間：12:00 -> 12:30
    times = [
        ("2023-10-01 12:30:00",),
        ("2023-10-01 12:20:00",),
        ("2023-10-01 12:10:00",),
        ("2023-10-01 12:00:00",),
    ]

    # 30 分鐘區間的經驗差，換算時速分別約 6000 / 3000 / 12000 億
    records = [
        ("PlayerA", "ServerA", 50, 400_000_000_000, 100_000_000_000),
        ("PlayerB", "ServerB", 50, 250_000_000_000, 100_000_000_000),
        ("PlayerC", "ServerC", 50, 700_000_000_000, 100_000_000_000),
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
    assert "30min" in embed.footer.text
    print("test_check_for_alerts passed!")

asyncio.run(test_check_for_alerts())
