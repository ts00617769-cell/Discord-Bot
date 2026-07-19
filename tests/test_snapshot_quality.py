"""快照品質門檻與 quiz poll 狀態輔助。"""
from services.beanfun_http import FetchResult
from services.error_handler import min_snapshot_players


def test_snapshot_quality_requires_overall_and_player_count(monkeypatch):
    monkeypatch.delenv("SNAPSHOT_MIN_PLAYERS", raising=False)
    min_players = min_snapshot_players()
    assert min_players == 30

    thin = FetchResult(ok=True, players=[{"gc_name": "A"}] * 5, overall_ok=True)
    assert not (thin.overall_ok and len(thin.players) >= min_players)

    no_overall = FetchResult(
        ok=True, players=[{"gc_name": f"P{i}"} for i in range(40)], overall_ok=False
    )
    assert not (no_overall.overall_ok and len(no_overall.players) >= min_players)

    good = FetchResult(
        ok=True,
        players=[{"gc_name": f"P{i}"} for i in range(40)],
        overall_ok=True,
        partial=True,
    )
    assert good.overall_ok and len(good.players) >= min_players


def test_min_snapshot_players_env(monkeypatch):
    monkeypatch.setenv("SNAPSHOT_MIN_PLAYERS", "10")
    assert min_snapshot_players() == 10
