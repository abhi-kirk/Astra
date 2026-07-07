"""
Unit tests for src/agent_guardrails.py — Autotrader execution guardrails.

All pure functions, no I/O. Covers every guardrail required by docs/autonomy.md
checklist #9: convictions-only, TSLA exclusion, min-hold (+ same-day round-trip /
cash GFV), max-trades/day, max-open-positions, drawdown-halt — plus the shared
hard-rule reuse. No real order path is exercised here; this suite is the gate that
must pass before execution is wired live.
"""

from datetime import datetime

import pytest

from src import config
from src.agent_guardrails import (
    business_days_between,
    check_agent_guardrails,
    check_halt_state,
)

MONDAY = datetime(2026, 7, 6, 10, 0)   # Autotrader launch day (a Monday)
TUESDAY = datetime(2026, 7, 7, 10, 0)


@pytest.fixture
def base_kwargs(convictions, good_market_data, portfolio_summary):
    """A clean, passing BUY for an approved ticker. Tests override single keys."""
    return dict(
        ticker="RKLB",
        side="buy",
        position={},
        market_data=good_market_data,
        convictions=convictions,
        portfolio_summary=portfolio_summary,
        trades_today=[],
        open_position_tickers=set(),
        last_buy=None,
        drawdown_pct=0.0,
        settled_cash=10_000.0,
        estimated_cost=100.0,
        now=MONDAY,
    )


def run(base, **over):
    return check_agent_guardrails(**{**base, **over})


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestPasses:
    def test_clean_buy_passes(self, base_kwargs):
        res = run(base_kwargs)
        assert res.passed
        assert res.block_reason is None
        assert res.checks["convictions_only"] and res.checks["not_excluded"]
        assert res.checks["max_trades_per_day"] and res.checks["hard_rules"]


# ---------------------------------------------------------------------------
# TSLA / hard exclusion
# ---------------------------------------------------------------------------

class TestExclusion:
    def test_tsla_blocked(self, base_kwargs):
        res = run(base_kwargs, ticker="TSLA")
        assert not res.passed
        assert "HARD EXCLUSION" in res.block_reason
        assert res.checks["not_excluded"] is False


# ---------------------------------------------------------------------------
# Convictions-only
# ---------------------------------------------------------------------------

class TestConvictionsOnly:
    def test_unknown_ticker_blocked(self, base_kwargs):
        res = run(base_kwargs, ticker="AMZN")  # not in any theme/holding
        assert not res.passed
        assert "CONVICTIONS ONLY" in res.block_reason
        assert res.checks["convictions_only"] is False

    def test_approved_ticker_allowed(self, base_kwargs):
        res = run(base_kwargs, ticker="ASTS")  # space.approved
        assert res.passed


# ---------------------------------------------------------------------------
# Max trades / day
# ---------------------------------------------------------------------------

class TestMaxTradesPerDay:
    def test_at_cap_blocked(self, base_kwargs, monkeypatch):
        monkeypatch.setattr(config.agent, "max_trades_per_day", 3)
        res = run(base_kwargs, trades_today=[{}, {}, {}])
        assert not res.passed
        assert "MAX TRADES/DAY" in res.block_reason

    def test_under_cap_allowed(self, base_kwargs, monkeypatch):
        monkeypatch.setattr(config.agent, "max_trades_per_day", 3)
        res = run(base_kwargs, trades_today=[{}, {}])
        assert res.passed


# ---------------------------------------------------------------------------
# Max open positions
# ---------------------------------------------------------------------------

class TestMaxOpenPositions:
    def test_new_position_at_cap_blocked(self, base_kwargs, monkeypatch):
        monkeypatch.setattr(config.agent, "max_open_positions", 5)
        five = {"A", "B", "C", "D", "E"}
        res = run(base_kwargs, ticker="ASTS", open_position_tickers=five)
        assert not res.passed
        assert "MAX OPEN POSITIONS" in res.block_reason

    def test_existing_position_not_blocked_by_cap(self, base_kwargs, monkeypatch):
        monkeypatch.setattr(config.agent, "max_open_positions", 5)
        # RKLB already open — adding to it is not opening a new position.
        five = {"RKLB", "B", "C", "D", "E"}
        res = run(base_kwargs, ticker="RKLB", open_position_tickers=five)
        assert res.passed

    def test_under_cap_allowed(self, base_kwargs, monkeypatch):
        monkeypatch.setattr(config.agent, "max_open_positions", 5)
        res = run(base_kwargs, ticker="ASTS", open_position_tickers={"RKLB", "NVDA"})
        assert res.passed


# ---------------------------------------------------------------------------
# Min-hold (sells) + same-day round-trip
# ---------------------------------------------------------------------------

class TestMinHold:
    def test_same_day_roundtrip_blocked(self, base_kwargs, monkeypatch):
        monkeypatch.setattr(config.agent, "min_hold_days", 2)
        res = run(base_kwargs, side="sell",
                  last_buy={"run_date": "2026-07-06T09:00:00"}, now=MONDAY)
        assert not res.passed
        assert "MIN HOLD" in res.block_reason

    def test_one_day_held_blocked(self, base_kwargs, monkeypatch):
        monkeypatch.setattr(config.agent, "min_hold_days", 2)
        res = run(base_kwargs, side="sell",
                  last_buy={"run_date": "2026-07-06T09:00:00"}, now=TUESDAY)
        assert not res.passed
        assert "MIN HOLD" in res.block_reason

    def test_held_long_enough_allowed(self, base_kwargs, monkeypatch):
        monkeypatch.setattr(config.agent, "min_hold_days", 2)
        # Bought Wed Jul 1 → sell Mon Jul 6 = 3 trading days later.
        res = run(base_kwargs, side="sell",
                  last_buy={"run_date": "2026-07-01T09:00:00"}, now=MONDAY)
        assert res.passed
        assert res.checks["min_hold_days"] is True

    def test_no_recorded_buy_allowed(self, base_kwargs):
        res = run(base_kwargs, side="sell", last_buy=None)
        assert res.passed


# ---------------------------------------------------------------------------
# Cash-account settlement / Good-Faith Violation (buys)
# ---------------------------------------------------------------------------

class TestCashGFV:
    def test_unsettled_funds_blocked(self, base_kwargs, monkeypatch):
        monkeypatch.setattr(config.agent, "account_is_cash", True)
        res = run(base_kwargs, settled_cash=50.0, estimated_cost=100.0)
        assert not res.passed
        assert "UNSETTLED FUNDS" in res.block_reason

    def test_settled_funds_allowed(self, base_kwargs, monkeypatch):
        monkeypatch.setattr(config.agent, "account_is_cash", True)
        res = run(base_kwargs, settled_cash=200.0, estimated_cost=100.0)
        assert res.passed

    def test_margin_account_ignores_settlement(self, base_kwargs, monkeypatch):
        monkeypatch.setattr(config.agent, "account_is_cash", False)
        res = run(base_kwargs, settled_cash=0.0, estimated_cost=100.0)
        assert res.passed


# ---------------------------------------------------------------------------
# Drawdown halt
# ---------------------------------------------------------------------------

class TestDrawdownHalt:
    def test_breach_blocks_buy(self, base_kwargs, monkeypatch):
        monkeypatch.setattr(config.agent, "drawdown_halt_pct", -15.0)
        res = run(base_kwargs, drawdown_pct=-15.0)
        assert not res.passed
        assert "DRAWDOWN HALT" in res.block_reason
        assert res.checks["drawdown_halt"] is False

    def test_breach_blocks_sell_too(self, base_kwargs, monkeypatch):
        monkeypatch.setattr(config.agent, "drawdown_halt_pct", -15.0)
        res = run(base_kwargs, side="sell", drawdown_pct=-20.0,
                  last_buy={"run_date": "2026-07-01T09:00:00"})
        assert not res.passed
        assert "DRAWDOWN HALT" in res.block_reason

    def test_within_limit_allowed(self, base_kwargs, monkeypatch):
        monkeypatch.setattr(config.agent, "drawdown_halt_pct", -15.0)
        res = run(base_kwargs, drawdown_pct=-10.0)
        assert res.passed

    def test_check_halt_state_helper(self, monkeypatch):
        monkeypatch.setattr(config.agent, "drawdown_halt_pct", -15.0)
        assert check_halt_state(-16.0)[0] is True
        assert check_halt_state(-14.9)[0] is False
        assert check_halt_state(None)[0] is False


# ---------------------------------------------------------------------------
# Shared hard rules reused from strategy.py
# ---------------------------------------------------------------------------

class TestSharedHardRules:
    def test_do_not_add_blocked(self, base_kwargs):
        res = run(base_kwargs, ticker="SPCE")  # space.do_not_add
        assert not res.passed
        assert "DO NOT ADD" in res.block_reason
        assert res.checks["hard_rules"] is False


# ---------------------------------------------------------------------------
# business_days_between helper
# ---------------------------------------------------------------------------

class TestBusinessDays:
    def test_same_day(self):
        assert business_days_between(MONDAY, MONDAY) == 0

    def test_across_weekend(self):
        # Wed Jul 1 → Mon Jul 6 (Jul 4/5 are Sat/Sun): Thu, Fri, Mon = 3.
        assert business_days_between(datetime(2026, 7, 1), datetime(2026, 7, 6)) == 3

    def test_one_trading_day(self):
        assert business_days_between(datetime(2026, 7, 6), datetime(2026, 7, 7)) == 1

    def test_end_before_start(self):
        assert business_days_between(TUESDAY, MONDAY) == 0
