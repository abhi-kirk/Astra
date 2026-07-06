"""
Unit tests for src/agent_executor.py — the mirror → guardrails → review → place path.

The broker is a fake (no MCP/network); Supabase writers are monkeypatched. Verifies:
review precedes place, marketable-limit params are correct, agent_trades is logged with
the mirror link, and NOTHING is placed when disabled / paused / halted / blocked / idempotent.
"""

import pytest

from src import agent_executor as ex
from src import config, memory


class FakeBroker:
    def __init__(self, equity=1000.0, positions=None, quote=None):
        self.equity = equity
        self._positions = positions if positions is not None else {"positions": []}
        self._quote = quote or {"ask_price": 100.0, "bid_price": 99.0, "last_trade_price": 99.5}
        self.reviews: list[dict] = []
        self.places: list[dict] = []
        self.portfolio_reads = 0

    def get_portfolio(self):
        self.portfolio_reads += 1
        return {"total_equity": self.equity, "buying_power": self.equity, "cash": self.equity}

    def get_positions(self):
        return self._positions

    def get_quote(self, symbol):
        return self._quote

    def review_order(self, **p):
        self.reviews.append(p)
        return {}

    def place_order(self, **p):
        self.places.append(p)
        return {"id": "ord-1", "state": "confirmed"}


@pytest.fixture
def wired(monkeypatch, convictions):
    """Monkeypatch every Supabase writer/reader the executor touches. Returns (state, logs)."""
    logs: list[dict] = []
    control = {"paused": False, "halted": False, "baseline_equity": None, "halt_reason": None}
    state = {
        "control": control,
        "opened": [{"id": 11, "ticker": "RKLB", "action": "buy", "suggested_position_pct": 0.06}],
        "closed": [],
        "today": [],
        "last_buy": None,
    }
    monkeypatch.setattr(memory, "get_agent_control", lambda: state["control"])
    monkeypatch.setattr(memory, "get_paper_trades_opened_on", lambda d: state["opened"])
    monkeypatch.setattr(memory, "get_paper_trades_closed_on", lambda d: state["closed"])
    monkeypatch.setattr(memory, "get_agent_trades_today", lambda d: state["today"])
    monkeypatch.setattr(memory, "get_last_agent_buy", lambda t: state["last_buy"])
    monkeypatch.setattr(memory, "save_agent_account_snapshot", lambda s: None)
    monkeypatch.setattr(memory, "set_agent_baseline", lambda e: control.__setitem__("baseline_equity", e))

    def _halt(h, r=None):
        control["halted"] = h
        control["halt_reason"] = r
    monkeypatch.setattr(memory, "set_agent_halted", _halt)

    def _log(**kw):
        logs.append(kw)
        return len(logs)
    monkeypatch.setattr(memory, "log_agent_trade", _log)

    monkeypatch.setattr(ex, "load_convictions", lambda: convictions)
    monkeypatch.setattr(ex, "get_market_data_bulk",
                        lambda tickers: {t: {"current_price": 100.0} for t in tickers})
    monkeypatch.setattr(ex, "_finalize", lambda *a, **k: None)  # no Telegram/DB side effects in tests
    return state, logs


# ---------------------------------------------------------------------------
# Dry-run vs live placement
# ---------------------------------------------------------------------------

def test_dry_run_reviews_but_never_places(wired, monkeypatch):
    monkeypatch.setattr(config, "AGENT_TRADING_ENABLED", False)
    _, logs = wired
    fb = FakeBroker()
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00")
    assert summary["dry_run"] is True
    assert len(fb.reviews) == 1 and len(fb.places) == 0
    assert logs[-1]["status"] == "dry_run"


def test_live_places_marketable_limit(wired, monkeypatch):
    monkeypatch.setattr(config, "AGENT_TRADING_ENABLED", True)
    monkeypatch.setattr(config, "AGENT_POSITION_PCT", 0.20)
    _, logs = wired
    fb = FakeBroker()
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert len(fb.reviews) == 1 and len(fb.places) == 1
    p = fb.places[0]
    assert p["symbol"] == "RKLB" and p["side"] == "buy" and p["type"] == "market"
    assert p["dollar_amount"] == "200.00"       # AGENT_POSITION_PCT (0.20) * $1000 equity
    assert "quantity" not in p and "ref_id" in p
    logged = logs[-1]
    assert logged["status"] == "submitted"
    assert logged["order_id"] == "ord-1"
    assert logged["mirrors_paper_trade_id"] == 11
    assert summary["placed"][0]["ticker"] == "RKLB"


def test_live_sell_mirror_exits_full_position(wired, monkeypatch):
    monkeypatch.setattr(config, "AGENT_TRADING_ENABLED", True)
    state, _ = wired
    state["opened"] = []
    state["closed"] = [{"id": 22, "ticker": "RKLB", "close_reason": "profit_take"}]
    state["last_buy"] = {"run_date": "2026-01-01T00:00:00"}  # long ago → min-hold ok
    fb = FakeBroker(positions={"positions": [{"symbol": "RKLB", "quantity": 5, "average_cost": 90}]})
    ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert len(fb.places) == 1
    p = fb.places[0]
    assert p["side"] == "sell" and p["type"] == "market" and float(p["quantity"]) == 5.0
    assert "dollar_amount" not in p


# ---------------------------------------------------------------------------
# Pause / halt / drawdown short-circuits — no broker activity
# ---------------------------------------------------------------------------

def test_paused_skips_everything(wired, monkeypatch):
    monkeypatch.setattr(config, "AGENT_TRADING_ENABLED", True)
    state, _ = wired
    state["control"]["paused"] = True
    fb = FakeBroker()
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert summary["skipped"] == ["paused"]
    assert fb.portfolio_reads == 0 and fb.places == []


def test_halted_skips_everything(wired, monkeypatch):
    monkeypatch.setattr(config, "AGENT_TRADING_ENABLED", True)
    state, _ = wired
    state["control"]["halted"] = True
    fb = FakeBroker()
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert summary["skipped"] == ["halted"]
    assert fb.places == []


def test_drawdown_breach_halts_before_orders(wired, monkeypatch):
    monkeypatch.setattr(config, "AGENT_TRADING_ENABLED", True)
    monkeypatch.setattr(config, "AGENT_DRAWDOWN_HALT_PCT", -15.0)
    state, _ = wired
    state["control"]["baseline_equity"] = 1000.0
    fb = FakeBroker(equity=800.0)  # -20% drawdown
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert summary["halted"] is True
    assert fb.places == []
    assert state["control"]["halted"] is True


# ---------------------------------------------------------------------------
# Guardrail block + idempotency
# ---------------------------------------------------------------------------

def test_guardrail_block_logs_but_does_not_place(wired, monkeypatch):
    monkeypatch.setattr(config, "AGENT_TRADING_ENABLED", True)
    state, logs = wired
    state["opened"] = [{"id": 1, "ticker": "TSLA", "action": "buy", "suggested_position_pct": 0.04}]
    fb = FakeBroker()
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert fb.places == []
    assert summary["blocked"] == ["buy:TSLA"]
    assert logs[-1]["status"] == "blocked"
    assert "HARD EXCLUSION" in logs[-1]["rule_checks"]["block_reason"]


def test_idempotent_skip_when_already_placed(wired, monkeypatch):
    monkeypatch.setattr(config, "AGENT_TRADING_ENABLED", True)
    state, _ = wired
    state["today"] = [{"ticker": "RKLB", "side": "buy", "status": "submitted"}]
    fb = FakeBroker()
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert fb.places == []
    assert "buy:RKLB" in summary["skipped"]


# ---------------------------------------------------------------------------
# Mirror-source filters: close_reason + exploration
# ---------------------------------------------------------------------------

def test_blocked_close_is_not_mirrored_as_sell(wired, monkeypatch):
    """A `blocked` paper-close comes from a buy-side rule on the MAIN portfolio
    (averaging-down cap, theme/position limit) — it must never sell the agentic position."""
    monkeypatch.setattr(config, "AGENT_TRADING_ENABLED", True)
    state, logs = wired
    state["opened"] = []
    state["closed"] = [{"id": 22, "ticker": "RKLB", "close_reason": "blocked"}]
    fb = FakeBroker(positions={"positions": [{"symbol": "RKLB", "quantity": 5, "average_cost": 90}]})
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert fb.reviews == [] and fb.places == []
    assert summary["placed"] == [] and logs == []


def test_signal_inactive_close_is_mirrored_as_sell(wired, monkeypatch):
    monkeypatch.setattr(config, "AGENT_TRADING_ENABLED", True)
    state, _ = wired
    state["opened"] = []
    state["closed"] = [{"id": 22, "ticker": "RKLB", "close_reason": "signal_inactive"}]
    state["last_buy"] = {"run_date": "2026-01-01T00:00:00"}
    fb = FakeBroker(positions={"positions": [{"symbol": "RKLB", "quantity": 5, "average_cost": 90}]})
    ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert len(fb.places) == 1 and fb.places[0]["side"] == "sell"


def test_exploration_paper_buy_is_not_mirrored(wired, monkeypatch):
    """Exploration candidates are paper-only experiments — never a real-money mirror."""
    monkeypatch.setattr(config, "AGENT_TRADING_ENABLED", True)
    state, logs = wired
    state["opened"] = [{
        "id": 33, "ticker": "RKLB", "action": "buy",
        "signal_data": {"action": "buy", "source": "exploration"},
    }]
    fb = FakeBroker()
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert fb.reviews == [] and fb.places == []
    assert summary["placed"] == [] and logs == []


# ---------------------------------------------------------------------------
# Daily cap counts only placed orders
# ---------------------------------------------------------------------------

def test_blocked_and_dry_run_rows_do_not_consume_daily_cap(wired, monkeypatch):
    """Morning dry-run rehearsals and blocked attempts logged in agent_trades must not
    starve the live run out of its trades/day budget."""
    monkeypatch.setattr(config, "AGENT_TRADING_ENABLED", True)
    monkeypatch.setattr(config, "AGENT_MAX_TRADES_PER_DAY", 3)
    state, _ = wired
    state["today"] = [
        {"ticker": "RKLB", "side": "buy", "status": "dry_run"},
        {"ticker": "NVDA", "side": "buy", "status": "blocked"},
        {"ticker": "ASTS", "side": "buy", "status": "blocked"},
    ]
    fb = FakeBroker()
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert len(fb.places) == 1
    assert summary["placed"][0]["ticker"] == "RKLB"


def test_placed_rows_still_consume_daily_cap(wired, monkeypatch):
    monkeypatch.setattr(config, "AGENT_TRADING_ENABLED", True)
    monkeypatch.setattr(config, "AGENT_MAX_TRADES_PER_DAY", 3)
    state, _ = wired
    state["today"] = [
        {"ticker": "NVDA", "side": "buy", "status": "submitted"},
        {"ticker": "ASTS", "side": "buy", "status": "filled"},
        {"ticker": "GOOGL", "side": "buy", "status": "pending"},
    ]
    fb = FakeBroker()
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert fb.places == []
    assert summary["blocked"] == ["buy:RKLB"]
