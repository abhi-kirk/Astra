"""
Unit tests for the pure decision-log dedup gate in src/memory.py.

A persistent signal (e.g. a profit-take that stays >60% up for weeks) must re-log
weekly, not on every daily run — otherwise `decisions` fills with duplicates that
pollute the advisor's history context and any future outcome stats.
"""

from src import config
from src.data_layer import get_advisor_book
from src.memory import _may_add_paper_lot, should_log_decision


class TestAdvisorBook:
    def test_uses_live_buying_power(self, monkeypatch):
        monkeypatch.setattr("src.memory.get_latest_portfolio_snapshot",
                            lambda: {"positions": {}, "buying_power": 13785.57})
        assert get_advisor_book() == 13785.57

    def test_falls_back_when_no_balance(self, monkeypatch):
        monkeypatch.setattr(config.paper, "portfolio_size", 10_000.0)
        monkeypatch.setattr("src.memory.get_latest_portfolio_snapshot",
                            lambda: {"positions": {}, "buying_power": None})
        assert get_advisor_book() == 10_000.0

    def test_falls_back_when_no_snapshot(self, monkeypatch):
        monkeypatch.setattr(config.paper, "portfolio_size", 10_000.0)
        monkeypatch.setattr("src.memory.get_latest_portfolio_snapshot", lambda: None)
        assert get_advisor_book() == 10_000.0

NOW = "2026-07-06T13:00:00+00:00"


def test_no_prior_decision_logs():
    assert should_log_decision(None, "sell", NOW) is True


def test_different_action_logs():
    last = {"action": "watch", "run_date": "2026-07-05T13:00:00+00:00"}
    assert should_log_decision(last, "buy", NOW) is True


def test_same_action_yesterday_skips():
    last = {"action": "sell", "run_date": "2026-07-05T13:00:00+00:00"}
    assert should_log_decision(last, "sell", NOW) is False


def test_same_action_a_week_ago_relogs():
    last = {"action": "sell", "run_date": "2026-06-29T13:00:00+00:00"}
    assert should_log_decision(last, "sell", NOW) is True


def test_naive_and_z_suffixed_timestamps_are_handled():
    last = {"action": "watch", "run_date": "2026-07-05T13:00:00Z"}
    assert should_log_decision(last, "watch", "2026-07-06T13:00:00") is False


def test_unparseable_timestamp_fails_open_and_logs():
    last = {"action": "sell", "run_date": "not-a-date"}
    assert should_log_decision(last, "sell", NOW) is True


# ---------------------------------------------------------------------------
# Bounded-pyramiding gate (_may_add_paper_lot)
# ---------------------------------------------------------------------------

def _lots(*run_dates):
    return [{"run_date": d} for d in run_dates]


def test_add_allowed_after_cooldown_and_under_cap(monkeypatch):
    monkeypatch.setattr(config.paper, "max_adds_per_ticker", 3)
    monkeypatch.setattr(config.paper, "add_cooldown_days", 5)
    # one open lot, newest 6 days ago → cooldown elapsed, under cap
    assert _may_add_paper_lot(_lots("2026-06-30T13:00:00+00:00"), NOW) is True


def test_add_blocked_within_cooldown(monkeypatch):
    monkeypatch.setattr(config.paper, "max_adds_per_ticker", 3)
    monkeypatch.setattr(config.paper, "add_cooldown_days", 5)
    # newest lot 2 days ago → still cooling down
    assert _may_add_paper_lot(_lots("2026-07-04T13:00:00+00:00"), NOW) is False


def test_add_blocked_at_cap(monkeypatch):
    monkeypatch.setattr(config.paper, "max_adds_per_ticker", 2)
    monkeypatch.setattr(config.paper, "add_cooldown_days", 5)
    # initial + 2 adds = 3 open lots already → at cap even though cooldown elapsed
    lots = _lots("2026-06-01T13:00:00+00:00", "2026-06-10T13:00:00+00:00", "2026-06-20T13:00:00+00:00")
    assert _may_add_paper_lot(lots, NOW) is False


def test_no_pyramiding_when_cap_zero(monkeypatch):
    monkeypatch.setattr(config.paper, "max_adds_per_ticker", 0)
    monkeypatch.setattr(config.paper, "add_cooldown_days", 5)
    assert _may_add_paper_lot(_lots("2026-01-01T13:00:00+00:00"), NOW) is False


def test_unparseable_lot_timestamp_fails_safe(monkeypatch):
    monkeypatch.setattr(config.paper, "max_adds_per_ticker", 3)
    monkeypatch.setattr(config.paper, "add_cooldown_days", 5)
    assert _may_add_paper_lot(_lots("not-a-date"), NOW) is False
