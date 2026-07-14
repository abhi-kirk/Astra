"""
Autotrader guardrails — the strategy boundary for autonomous real-money execution.

Pure functions only: every check takes injected data and returns a decision, so the
whole module is deterministic and unit-testable with no I/O. The executor
(`src/agent_executor.py`) gathers the live context (account snapshot, today's orders,
open positions, last buy) and passes it here; nothing in this file touches Supabase,
Robinhood, or the network.

Robinhood's native layer is the *money* boundary (account isolation + balance cap);
this module is ASTRA's *strategy* boundary — identical for paper and real so the two
tracks can never diverge. It reuses the shared hard rules in `src/strategy.py`
(TSLA exclusion, do-not-add, averaging-down, position/theme limits) and layers the
per-order agentic guardrails on top.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from src import config
from src.strategy import is_excluded


@dataclass
class GuardrailResult:
    passed: bool
    block_reason: str | None = None
    checks: dict[str, bool] = field(default_factory=dict)  # per-rule pass/fail → agent_trades.rule_checks


def _parse_dt(value) -> datetime | None:
    """Best-effort parse of an ISO timestamp (str or datetime) → naive datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def business_days_between(start: datetime, end: datetime) -> int:
    """Count of weekdays strictly after `start`'s day, up to and including `end`'s day.

    Approximates NYSE trading days (ignores market holidays — slightly permissive on
    holiday weeks, never stricter than intended). Same calendar day → 0.
    """
    start_d = start.date()
    end_d = end.date()
    if end_d <= start_d:
        return 0
    days = 0
    cur = start_d
    while cur < end_d:
        cur += timedelta(days=1)
        if cur.weekday() < 5:  # Mon–Fri
            days += 1
    return days


def check_halt_state(drawdown_pct: float | None) -> tuple[bool, str | None]:
    """Account-level drawdown halt. Returns (halted, reason)."""
    if drawdown_pct is not None and drawdown_pct <= config.agent.drawdown_halt_pct:
        return True, (
            f"DRAWDOWN HALT: account down {drawdown_pct:.1f}% "
            f"(limit {config.agent.drawdown_halt_pct:.0f}%)"
        )
    return False, None


def check_agent_guardrails(
    *,
    ticker: str,
    side: str,                          # "buy" | "sell"
    convictions: dict,                  # for the TSLA fail-safe lookup only
    trades_today: list[dict],           # today's agent_trades rows (anti-runaway cap)
    last_buy: dict | None = None,       # most recent agent BUY row for this ticker (min-hold)
    drawdown_pct: float | None = None,  # current account drawdown %
    settled_cash: float | None = None,  # settled cash available (cash-account GFV, buys)
    estimated_cost: float | None = None,  # notional of this buy
    now: datetime | None = None,
) -> GuardrailResult:
    """Unattended-execution safety for one prospective Autotrader order. The shared brain
    (src/brain/) owns all strategy — convictions, quality, entry/exit, sizing intent — and
    the Autotrader mirrors its decisions, so these checks carry only what a human-in-the-loop
    track has no need for (Abhi is his own circuit breaker): a drawdown kill switch, an
    anti-runaway trade cap, cash-account settlement (GFV), and a sell min-hold, plus a
    redundant TSLA fail-safe at the real-money boundary. Fail-closed: the first failed check
    blocks the order and names it. `checks` records each rule for agent_trades.rule_checks."""
    now = now or datetime.now()
    checks: dict[str, bool] = {}

    def block(reason: str) -> GuardrailResult:
        return GuardrailResult(passed=False, block_reason=reason, checks=checks)

    # 1. Drawdown halt (account-level) — kill switch, blocks everything.
    halted, halt_reason = check_halt_state(drawdown_pct)
    checks["drawdown_halt"] = not halted
    if halted:
        return block(halt_reason or "DRAWDOWN HALT")

    # 2. TSLA fail-safe — a redundant hard line at the money boundary; the brain excludes it upstream.
    excl = is_excluded(ticker, convictions)
    checks["not_excluded"] = excl is None
    if excl:
        return block(f"HARD EXCLUSION: {excl}")

    # 3. Anti-runaway cap — bound total agentic orders per calendar day.
    under_daily_cap = len(trades_today) < config.agent.max_trades_per_day
    checks["max_trades_per_day"] = under_daily_cap
    if not under_daily_cap:
        return block(
            f"MAX TRADES/DAY: {len(trades_today)} orders already placed today "
            f"(limit {config.agent.max_trades_per_day})"
        )

    if side == "buy":
        # 4. Cash-account settlement — no buying with unsettled proceeds (Good-Faith Violation).
        if config.agent.account_is_cash and settled_cash is not None and estimated_cost is not None:
            funded = estimated_cost <= settled_cash + 1e-6
            checks["settled_funds"] = funded
            if not funded:
                return block(
                    f"UNSETTLED FUNDS: buy ${estimated_cost:,.2f} exceeds settled cash "
                    f"${settled_cash:,.2f} (cash account — would risk a Good-Faith Violation)"
                )

    elif side == "sell":
        # 5. Min-hold — no selling a lot younger than AGENT_MIN_HOLD_DAYS trading days,
        #    and never a same-day round-trip (day-trade / GFV avoidance).
        last_dt = _parse_dt(last_buy.get("run_date")) if last_buy else None
        if last_dt is not None:
            held_days = business_days_between(last_dt, now)
            ok = held_days >= config.agent.min_hold_days
            checks["min_hold_days"] = ok
            if not ok:
                return block(
                    f"MIN HOLD: bought {held_days} trading day(s) ago "
                    f"(min {config.agent.min_hold_days}) — no day-trades / same-day round-trips"
                )
        else:
            checks["min_hold_days"] = True  # no recorded agentic buy → nothing to restrict

    return GuardrailResult(passed=True, block_reason=None, checks=checks)
