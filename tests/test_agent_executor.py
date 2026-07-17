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
    def __init__(self, equity=1000.0, positions=None, quote=None,
                 equity_value=None, cash=None, realized=0.0):
        self.equity = equity
        self._positions = positions if positions is not None else {"positions": []}
        # Default equity_value = cost basis of any open positions (i.e. breakeven, P&L-neutral),
        # so position-bearing tests don't accidentally trip the P&L drawdown halt.
        if equity_value is None:
            equity_value = sum(
                float(p.get("quantity") or 0) * float(p.get("average_buy_price") or p.get("average_cost") or 0)
                for p in (self._positions.get("positions") or [])
            )
        self._equity_value = equity_value
        self._cash = cash if cash is not None else equity
        self._realized = realized
        self._quote = quote or {"ask_price": 100.0, "bid_price": 99.0, "last_trade_price": 99.5}
        self.reviews: list[dict] = []
        self.places: list[dict] = []
        self.portfolio_reads = 0

    def get_portfolio(self):
        self.portfolio_reads += 1
        return {"total_value": self.equity, "equity_value": self._equity_value,
                "buying_power": self._cash, "cash": self._cash}

    def get_positions(self):
        return self._positions

    def get_realized_pnl(self):
        return {"total_returns": str(self._realized)}

    def get_quote(self, symbol):
        return self._quote

    def review_order(self, **p):
        # Schema-accurate: review_equity_order has no ref_id param and rejects unknown props.
        if "ref_id" in p:
            from src.agent_broker import BrokerError
            raise BrokerError("review_equity_order rejects unexpected property 'ref_id'")
        self.reviews.append(p)
        return {}

    def place_order(self, **p):
        # place_equity_order is the only call that carries the ref_id idempotency key.
        assert "ref_id" in p, "place_order must receive a ref_id idempotency key"
        self.places.append(p)
        return {"id": "ord-1", "state": "confirmed"}


@pytest.fixture
def wired(monkeypatch, convictions):
    """Monkeypatch every Supabase writer/reader the executor touches. Returns (state, logs)."""
    logs: list[dict] = []
    control = {"paused": False, "halted": False, "halt_reason": None}
    state = {
        "control": control,
        "opened": [{"id": 11, "ticker": "RKLB", "action": "buy", "suggested_position_pct": 0.06}],
        "closed": [],
        "today": [],
        "last_buy": None,
        "prev_snapshot": None,
    }
    monkeypatch.setattr(memory, "get_agent_control", lambda: state["control"])
    monkeypatch.setattr(memory, "get_paper_trades_opened_on", lambda d: state["opened"])
    monkeypatch.setattr(memory, "get_paper_trades_closed_on", lambda d: state["closed"])
    monkeypatch.setattr(memory, "get_agent_trades_today", lambda d: state["today"])
    monkeypatch.setattr(memory, "get_last_agent_buy", lambda t: state["last_buy"])
    monkeypatch.setattr(memory, "get_latest_agent_snapshot", lambda: state["prev_snapshot"])
    monkeypatch.setattr(memory, "save_agent_account_snapshot", lambda s: None)

    def _halt(h, r=None):
        control["halted"] = h
        control["halt_reason"] = r
    monkeypatch.setattr(memory, "set_agent_halted", _halt)

    def _log(**kw):
        logs.append(kw)
        return len(logs)
    monkeypatch.setattr(memory, "log_agent_trade", _log)

    monkeypatch.setattr(ex, "load_convictions", lambda: convictions)
    monkeypatch.setattr(ex, "_finalize", lambda *a, **k: None)  # no Telegram/DB side effects in tests
    return state, logs


# ---------------------------------------------------------------------------
# Dry-run vs live placement
# ---------------------------------------------------------------------------

def test_dry_run_reviews_but_never_places(wired, monkeypatch):
    monkeypatch.setattr(config.agent, "trading_enabled", False)
    _, logs = wired
    fb = FakeBroker()
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00")
    assert summary["dry_run"] is True
    assert len(fb.reviews) == 1 and len(fb.places) == 0
    assert logs[-1]["status"] == "dry_run"


def test_live_places_marketable_limit(wired, monkeypatch):
    monkeypatch.setattr(config.agent, "trading_enabled", True)
    _, logs = wired
    fb = FakeBroker()  # equity/buying_power = $1000
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert len(fb.reviews) == 1 and len(fb.places) == 1
    p = fb.places[0]
    assert p["symbol"] == "RKLB" and p["side"] == "buy" and p["type"] == "market"
    # Sole buy takes the whole run budget: min(cash − 30% reserve, 25% of cash) = min(700, 250).
    assert p["dollar_amount"] == "250.00"
    assert "quantity" not in p and "ref_id" in p
    logged = logs[-1]
    assert logged["status"] == "submitted"
    assert logged["order_id"] == "ord-1"
    assert logged["mirrors_paper_trade_id"] == 11
    assert summary["placed"][0]["ticker"] == "RKLB"


def test_place_failure_logs_failed_and_continues(wired, monkeypatch):
    """A broker error on one order must be logged as `failed` and NOT abort the run —
    the next mirror is still attempted. (Regression: a place error used to crash the run.)"""
    from src.agent_broker import BrokerError
    monkeypatch.setattr(config.agent, "trading_enabled", True)
    state, logs = wired
    state["opened"] = [
        {"id": 12, "ticker": "RKLB", "action": "buy"},
        {"id": 14, "ticker": "NVDA", "action": "buy"},
    ]

    class OneFailBroker(FakeBroker):
        def place_order(self, **p):
            if p["symbol"] == "RKLB":
                raise BrokerError("API error 400: investor profile required before second trade")
            return super().place_order(**p)

    fb = OneFailBroker()
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert summary["failed"] == ["buy:RKLB"]                 # RKLB logged failed, didn't crash
    assert [pl["ticker"] for pl in summary["placed"]] == ["NVDA"]  # run continued to NVDA
    assert any(row["status"] == "failed" and "place_error" in row["rule_checks"] for row in logs)


def test_live_sell_mirror_exits_full_position(wired, monkeypatch):
    monkeypatch.setattr(config.agent, "trading_enabled", True)
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
    monkeypatch.setattr(config.agent, "trading_enabled", True)
    state, _ = wired
    state["control"]["paused"] = True
    fb = FakeBroker()
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert summary["skipped"] == ["paused"]
    assert fb.portfolio_reads == 0 and fb.places == []


def test_halted_skips_everything(wired, monkeypatch):
    monkeypatch.setattr(config.agent, "trading_enabled", True)
    state, _ = wired
    state["control"]["halted"] = True
    fb = FakeBroker()
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert summary["skipped"] == ["halted"]
    assert fb.places == []


def test_drawdown_breach_halts_before_orders(wired, monkeypatch):
    monkeypatch.setattr(config.agent, "trading_enabled", True)
    monkeypatch.setattr(config.agent, "drawdown_halt_pct", -15.0)
    state, _ = wired
    # $1000 cost basis, now worth $800 → unrealized −$200 = −20% of deployed capital.
    positions = {"positions": [{"symbol": "AMZN", "quantity": "10", "average_buy_price": "100"}]}
    fb = FakeBroker(equity=800.0, equity_value=800.0, cash=0.0, positions=positions)
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert summary["halted"] is True
    assert fb.places == []
    assert state["control"]["halted"] is True


def test_deposit_does_not_false_trigger_halt(wired, monkeypatch):
    """A cash deposit inflates equity but not P&L — the halt must NOT fire (the whole point
    of the P&L-based metric). Positions are flat (+0), plus $5000 of idle deposited cash."""
    monkeypatch.setattr(config.agent, "trading_enabled", True)
    monkeypatch.setattr(config.agent, "drawdown_halt_pct", -15.0)
    state, _ = wired
    positions = {"positions": [{"symbol": "AMZN", "quantity": "10", "average_buy_price": "100"}]}
    # equity_value == cost basis ($1000, flat), + $5000 deposited cash sitting idle.
    fb = FakeBroker(equity=6000.0, equity_value=1000.0, cash=5000.0, positions=positions)
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert summary["halted"] is False
    assert state["control"]["halted"] is False


def test_realized_loss_counts_after_positions_closed(wired, monkeypatch):
    """Realized losses still halt when flat: capital_base = total_equity − net_pnl
    reconstructs the contributed $1000 from $800 cash + $200 realized loss (no stored baseline)."""
    monkeypatch.setattr(config.agent, "trading_enabled", True)
    monkeypatch.setattr(config.agent, "drawdown_halt_pct", -15.0)
    state, _ = wired
    # Now flat (no positions), but −$200 realized on prior closes → −20% of contributed capital.
    fb = FakeBroker(equity=800.0, equity_value=0.0, cash=800.0, realized=-200.0)
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert summary["halted"] is True
    assert state["control"]["halted"] is True


# ---------------------------------------------------------------------------
# Guardrail block + idempotency
# ---------------------------------------------------------------------------

def test_guardrail_block_logs_but_does_not_place(wired, monkeypatch):
    monkeypatch.setattr(config.agent, "trading_enabled", True)
    state, logs = wired
    state["opened"] = [{"id": 1, "ticker": "TSLA", "action": "buy", "suggested_position_pct": 0.04}]
    fb = FakeBroker()
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert fb.places == []
    assert summary["blocked"] == ["buy:TSLA"]
    assert logs[-1]["status"] == "blocked"
    assert "HARD EXCLUSION" in logs[-1]["rule_checks"]["block_reason"]


def test_idempotent_skip_when_already_placed(wired, monkeypatch):
    monkeypatch.setattr(config.agent, "trading_enabled", True)
    state, _ = wired
    state["today"] = [{"ticker": "RKLB", "side": "buy", "status": "submitted"}]
    fb = FakeBroker()
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert fb.places == []
    assert "buy:RKLB" in summary["skipped"]


# ---------------------------------------------------------------------------
# Account-read failure (expired token) must be a LOUD abort, not a silent no-op
# ---------------------------------------------------------------------------

def test_account_read_failure_aborts_loudly(wired, monkeypatch):
    """An unreadable agentic account (e.g. expired OAuth token) must set `aborted`, run
    `_finalize` (Telegram alert + failed service health), and never look like success."""
    monkeypatch.setattr(config.agent, "trading_enabled", True)
    finalized: list[dict] = []
    monkeypatch.setattr(ex, "_finalize", lambda summary, *a, **k: finalized.append(summary))

    from src.agent_broker import BrokerError

    class DeadBroker(FakeBroker):
        def get_portfolio(self):
            raise BrokerError("OAuth flow error — token expired")

    summary = ex.run(broker=DeadBroker(), run_date="2026-07-06T13:00:00", dry_run=False)
    assert summary["aborted"] == "account_read_failed"
    assert finalized and finalized[0] is summary  # alert path was exercised


def test_notify_agent_alerts_on_abort(monkeypatch):
    from src import notify
    sent: list[str] = []
    monkeypatch.setattr(notify, "send", lambda text: sent.append(text) or True)
    assert notify.notify_agent({"aborted": "account_read_failed"}) is True
    assert len(sent) == 1 and "bootstrap" in sent[0].lower()


# ---------------------------------------------------------------------------
# Mirror-source filters: close_reason + exploration
# ---------------------------------------------------------------------------

def test_blocked_close_is_not_mirrored_as_sell(wired, monkeypatch):
    """A `blocked` paper-close comes from a buy-side rule on the MAIN portfolio
    (averaging-down cap, theme/position limit) — it must never sell the agentic position."""
    monkeypatch.setattr(config.agent, "trading_enabled", True)
    state, logs = wired
    state["opened"] = []
    state["closed"] = [{"id": 22, "ticker": "RKLB", "close_reason": "blocked"}]
    fb = FakeBroker(positions={"positions": [{"symbol": "RKLB", "quantity": 5, "average_cost": 90}]})
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert fb.reviews == [] and fb.places == []
    assert summary["placed"] == [] and logs == []


def test_signal_inactive_close_is_mirrored_as_sell(wired, monkeypatch):
    monkeypatch.setattr(config.agent, "trading_enabled", True)
    state, _ = wired
    state["opened"] = []
    state["closed"] = [{"id": 22, "ticker": "RKLB", "close_reason": "signal_inactive"}]
    state["last_buy"] = {"run_date": "2026-01-01T00:00:00"}
    fb = FakeBroker(positions={"positions": [{"symbol": "RKLB", "quantity": 5, "average_cost": 90}]})
    ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert len(fb.places) == 1 and fb.places[0]["side"] == "sell"


def test_exploration_paper_buy_reaches_real_money(wired, monkeypatch):
    """The Autotrader mirrors the brain 1:1 — an exploration paper buy reaches real money
    even for a name outside any conviction allowlist (PL). The brain, not the executor,
    decides what to trade."""
    monkeypatch.setattr(config.agent, "trading_enabled", True)
    state, logs = wired
    state["opened"] = [{
        "id": 33, "ticker": "PL", "action": "buy",
        "signal_data": {"action": "buy", "source": "exploration"},
    }]
    fb = FakeBroker()
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert len(fb.places) == 1 and fb.places[0]["symbol"] == "PL"
    assert summary["placed"][0]["ticker"] == "PL"
    assert logs[-1]["mirrors_paper_trade_id"] == 33


# ---------------------------------------------------------------------------
# Sleeve sizing + pacing (Option 1 + reserve floor)
# ---------------------------------------------------------------------------

class TestSleeveBudget:
    def test_per_run_cap_binds(self, monkeypatch):
        monkeypatch.setattr(config.agent, "reserve_floor_pct", 0.30)
        monkeypatch.setattr(config.agent, "max_deploy_per_run_pct", 0.25)
        # min(1000 − 300 reserve, 250 per-run) = 250.
        assert ex._sleeve_budget(1000.0, 1000.0) == 250.0

    def test_reserve_floor_binds(self, monkeypatch):
        monkeypatch.setattr(config.agent, "reserve_floor_pct", 0.30)
        monkeypatch.setattr(config.agent, "max_deploy_per_run_pct", 0.25)
        # Cash $350 on a $1000 sleeve: floor allows only $50, below the $87.50 per-run cap.
        assert ex._sleeve_budget(350.0, 1000.0) == 50.0

    def test_below_floor_deploys_nothing(self, monkeypatch):
        monkeypatch.setattr(config.agent, "reserve_floor_pct", 0.30)
        assert ex._sleeve_budget(200.0, 1000.0) == 0.0

    def test_zero_inputs_safe(self):
        assert ex._sleeve_budget(0.0, 1000.0) == 0.0
        assert ex._sleeve_budget(None, None) == 0.0


class TestAllocateBuys:
    def test_split_proportional_to_weight(self):
        buys = [{"ticker": "NVDA", "weight": 0.08}, {"ticker": "PL", "weight": 0.04}]
        ex._allocate_buys(buys, budget=300.0, slots=6)
        by = {b["ticker"]: b["dollars"] for b in buys}
        assert by["NVDA"] == 200.0 and by["PL"] == 100.0  # 2:1 ordering preserved

    def test_slots_defer_lowest_conviction(self):
        buys = [{"ticker": "NVDA", "weight": 0.08}, {"ticker": "PL", "weight": 0.04}]
        ex._allocate_buys(buys, budget=300.0, slots=1)
        by = {b["ticker"]: b["dollars"] for b in buys}
        assert by["NVDA"] == 300.0 and by["PL"] == 0.0  # only the top-ranked name funded

    def test_equal_split_when_weights_absent(self):
        buys = [{"ticker": "A"}, {"ticker": "B"}]
        ex._allocate_buys(buys, budget=200.0, slots=6)
        assert all(b["dollars"] == 100.0 for b in buys)

    def test_zero_budget_funds_nothing(self):
        buys = [{"ticker": "A", "weight": 0.05}]
        ex._allocate_buys(buys, budget=0.0, slots=6)
        assert buys[0]["dollars"] == 0.0


def test_higher_conviction_buy_gets_more_dollars(wired, monkeypatch):
    """End-to-end: two same-day buys split the paced budget by conviction weight."""
    monkeypatch.setattr(config.agent, "trading_enabled", True)
    state, _ = wired
    state["opened"] = [
        {"id": 41, "ticker": "NVDA", "action": "buy", "suggested_position_pct": 0.08},
        {"id": 42, "ticker": "PL", "action": "buy", "suggested_position_pct": 0.04},
    ]
    fb = FakeBroker()  # $1000 → run budget $250, split 2:1
    ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    dollars = {p["symbol"]: float(p["dollar_amount"]) for p in fb.places}
    assert dollars["NVDA"] > dollars["PL"]
    assert dollars["NVDA"] == 166.67 and dollars["PL"] == 83.33


# ---------------------------------------------------------------------------
# Daily cap counts only placed orders
# ---------------------------------------------------------------------------

def test_blocked_and_dry_run_rows_do_not_consume_daily_cap(wired, monkeypatch):
    """Morning dry-run rehearsals and blocked attempts logged in agent_trades must not
    starve the live run out of its trades/day budget."""
    monkeypatch.setattr(config.agent, "trading_enabled", True)
    monkeypatch.setattr(config.agent, "max_trades_per_day", 3)
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
    """Placed orders consume the daily trade budget, so the sleeve funds no further buys —
    RKLB gets no run allocation and is skipped (unfunded), never sent to the broker."""
    monkeypatch.setattr(config.agent, "trading_enabled", True)
    monkeypatch.setattr(config.agent, "max_trades_per_day", 3)
    state, _ = wired
    state["today"] = [
        {"ticker": "NVDA", "side": "buy", "status": "submitted"},
        {"ticker": "ASTS", "side": "buy", "status": "filled"},
        {"ticker": "GOOGL", "side": "buy", "status": "pending"},
    ]
    fb = FakeBroker()
    summary = ex.run(broker=fb, run_date="2026-07-06T13:00:00", dry_run=False)
    assert fb.places == []
    assert summary["skipped"] == ["buy:RKLB:unfunded"]
