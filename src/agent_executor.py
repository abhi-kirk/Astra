"""
Autotrader orchestrator — mirror Advisor's paper decisions into real autonomous orders.

Runs as its own workflow, after Advisor's daily run has persisted the paper track. Flow:

    load agent_control → if paused/halted, exit (no execution)
    → snapshot agentic account (equity/cash) → compute drawdown → if breached, HALT + exit
    → read today's paper track (buys opened / positions closed) = mirror source
    → for each mirror: check_agent_guardrails → review_equity_order → place_equity_order
       (marketable limit, fresh ref_id) → log_agent_trade (link mirrors_paper_trade_id)

No LLM in the loop; guardrails are pure code. Nothing places a real order unless
`AGENT_TRADING_ENABLED` is True — otherwise every order runs in dry-run (review only).
The broker is injectable so the whole path is unit-tested with a fake.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from src import config, memory, notify
from src.agent_broker import (
    AgenticBroker,
    BrokerError,
    extract_order,
    extract_portfolio,
    extract_positions,
)
from src.agent_guardrails import check_agent_guardrails
from src.data_layer import load_convictions
from src.observability import RunObserver

logger = logging.getLogger(__name__)

_PLACED_STATES = ("pending", "submitted", "filled")

# Sells mirror only deliberate paper exits. A `blocked` close comes from a buy-side rule
# tripping on the MAIN portfolio (averaging-down cap, theme/position limit) and says
# nothing about the agentic position — never a real-money sell trigger.
# `parabolic_trim` is intentionally excluded: a trim keeps the paper lot open (never
# enters this close-based mirror), and partial real-money sells are a deferred follow-up.
_MIRRORED_CLOSE_REASONS = ("profit_take", "signal_inactive", "thesis_invalidation", "trailing_stop")


def _finalize(summary: dict, num_mirrors: int = 0) -> None:
    """Non-fatal side effects at run exit: Telegram alert + observability metrics."""
    try:
        notify.notify_agent(summary)
    except Exception:
        logger.error("Autotrader notification failed — non-fatal", exc_info=True)
    try:
        placed = summary.get("placed") or []
        obs = RunObserver(summary["run_date"], "autotrader")
        obs.record(
            num_signals=num_mirrors,
            buy_count=sum(1 for o in placed if o.get("side") == "buy"),
            sell_count=sum(1 for o in placed if o.get("side") == "sell"),
        )
        obs.record_service("robinhood_agentic", ok=not summary.get("halted") and not summary.get("aborted"))
        obs.flush()
    except Exception:
        logger.error("Autotrader observability failed — non-fatal", exc_info=True)


def _drawdown_pct(equity: float, baseline: float | None) -> float | None:
    if not baseline or baseline <= 0:
        return None
    return round((equity - baseline) / baseline * 100, 4)


def _build_mirrors(run_date: str) -> list[dict]:
    """Today's paper track → mirror orders. BUYs from paper opens, SELLs from paper closes."""
    mirrors: list[dict] = []
    for pt in memory.get_paper_trades_opened_on(run_date):
        if pt.get("action") != "buy":
            continue
        mirrors.append({
            "ticker": pt["ticker"], "side": "buy",
            "mirrors_paper_trade_id": pt.get("id"),
            "weight": pt.get("suggested_position_pct"),  # brain conviction weight → sleeve sizing
        })
    for pt in memory.get_paper_trades_closed_on(run_date):
        if pt.get("close_reason") not in _MIRRORED_CLOSE_REASONS:
            continue
        mirrors.append({
            "ticker": pt["ticker"], "side": "sell",
            "mirrors_paper_trade_id": pt.get("id"),
        })
    return mirrors


def _already_handled(trades_today: list[dict], ticker: str, side: str) -> bool:
    return any(
        t.get("ticker") == ticker and t.get("side") == side and t.get("status") in _PLACED_STATES
        for t in trades_today
    )


def _sleeve_budget(buying_power: float | None, equity: float | None) -> float:
    """Dollars the sleeve may deploy this run: at most `max_deploy_per_run_pct` of remaining
    cash, and never dipping below the `reserve_floor_pct` cash cushion. The cushion is dry
    powder for higher-conviction dips, not idle money — Robinhood Gold pays ~3.5% APY on it.
    Live `buying_power` means prior fills (this run or earlier today) self-pace deployment."""
    if not buying_power or not equity or buying_power <= 0 or equity <= 0:
        return 0.0
    above_floor = buying_power - config.agent.reserve_floor_pct * equity
    per_run = config.agent.max_deploy_per_run_pct * buying_power
    return round(max(0.0, min(above_floor, per_run)), 2)


def _allocate_buys(buys: list[dict], budget: float, slots: int) -> None:
    """Size today's buy mirrors together (the brain sets weights; the sleeve splits a paced
    budget across them). Rank by conviction weight, keep the top `slots`, and split `budget`
    in proportion to weight — higher conviction gets more (equal split if weights are absent).
    Annotates each mirror in place with `dollars`; deferred/unfunded mirrors get 0.0."""
    ranked = sorted(buys, key=lambda m: m.get("weight") or 0.0, reverse=True)
    funded, deferred = ranked[: max(0, slots)], ranked[max(0, slots):]
    for m in deferred:
        m["dollars"] = 0.0
    total = sum((m.get("weight") or 0.0) for m in funded)
    n = len(funded)
    for m in funded:
        if budget <= 0 or n == 0:
            m["dollars"] = 0.0
            continue
        share = (m.get("weight") or 0.0) / total if total > 0 else 1.0 / n
        m["dollars"] = round(budget * share, 2)


def run(run_date: str | None = None, broker: AgenticBroker | None = None,
        dry_run: bool | None = None) -> dict:
    """Execute Autotrader for one session. Returns a summary dict."""
    run_date = run_date or datetime.now(timezone.utc).isoformat()
    # Dry-run whenever the master switch is off, unless a caller explicitly forces live.
    if dry_run is None:
        dry_run = not config.agent.trading_enabled
    summary = {"run_date": run_date, "dry_run": dry_run, "placed": [], "blocked": [], "skipped": [], "failed": [], "halted": False}

    control = memory.get_agent_control()
    if control.get("paused"):
        logger.warning("Autotrader is PAUSED (agent_control.paused) — no execution this run.")
        summary["skipped"].append("paused")
        return summary
    if control.get("halted"):
        logger.warning("Autotrader is HALTED (%s) — manual reset required.", control.get("halt_reason"))
        summary["skipped"].append("halted")
        return summary

    broker = broker or AgenticBroker()

    # -- account snapshot + drawdown halt --------------------------------
    try:
        port = extract_portfolio(broker.get_portfolio())
        positions = extract_positions(broker.get_positions())
    except BrokerError:
        logger.error("Could not read agentic account — aborting Autotrader run.", exc_info=True)
        summary["skipped"].append("account_read_failed")
        summary["aborted"] = "account_read_failed"  # hard failure — alert + non-zero exit
        _finalize(summary)
        return summary

    equity = port["total_equity"]
    baseline = control.get("baseline_equity")
    if not baseline and equity > 0:
        memory.set_agent_baseline(equity)
        baseline = equity
        logger.info("Autotrader drawdown baseline set to $%.2f", equity)
    drawdown = _drawdown_pct(equity, baseline)

    memory.save_agent_account_snapshot({
        "cash": port["cash"], "buying_power": port["buying_power"],
        "market_value": port["total_equity"], "total_equity": equity,
        "positions": positions, "baseline_equity": baseline, "drawdown_pct": drawdown,
    })

    if drawdown is not None and drawdown <= config.agent.drawdown_halt_pct:
        reason = f"Drawdown {drawdown:.1f}% ≤ halt {config.agent.drawdown_halt_pct:.0f}%"
        memory.set_agent_halted(True, reason)
        logger.error("Autotrader DRAWDOWN HALT — %s. Execution stopped.", reason)
        summary["halted"] = True
        _finalize(summary)
        return summary

    # -- mirror source + shared context ----------------------------------
    mirrors = _build_mirrors(run_date)
    if not mirrors:
        logger.info("No paper decisions to mirror today — nothing to execute.")
        _finalize(summary)
        return summary

    convictions = load_convictions()
    trades_today = memory.get_agent_trades_today(run_date)
    now = datetime.now()

    # Size today's buys together: rank by the brain's conviction weight, deploy a capped slice
    # of remaining cash (never below the reserve floor), split in proportion to weight. Sells
    # don't draw on this budget. `_allocate_buys` annotates each buy mirror with `dollars`.
    placed_buys_today = sum(
        1 for t in trades_today if t.get("side") == "buy" and t.get("status") in _PLACED_STATES
    )
    buy_slots = config.agent.max_trades_per_day - placed_buys_today
    pending_buys = [
        m for m in mirrors
        if m["side"] == "buy" and not _already_handled(trades_today, m["ticker"], "buy")
    ]
    _allocate_buys(pending_buys, _sleeve_budget(port["buying_power"], equity), buy_slots)

    for m in mirrors:
        ticker, side = m["ticker"], m["side"]
        if _already_handled(trades_today, ticker, side):
            logger.info("Autotrader: %s %s already handled today — skipping (idempotent).", side, ticker)
            summary["skipped"].append(f"{side}:{ticker}")
            continue

        held = positions.get(ticker, {})

        if side == "buy":
            estimated_cost = m.get("dollars") or None
            if not estimated_cost:
                logger.info("Autotrader: buy %s not funded this run (budget/rank) — skipping.", ticker)
                summary["skipped"].append(f"buy:{ticker}:unfunded")
                continue
        else:
            if not held.get("shares"):
                logger.info("Autotrader: no agentic %s position to sell — skipping.", ticker)
                summary["skipped"].append(f"sell:{ticker}:not_held")
                continue
            estimated_cost = None

        # The daily cap counts only orders actually sent to the broker — blocked and
        # dry-run rows in agent_trades must not starve real trades out of the budget.
        placed_today = [t for t in trades_today if t.get("status") in _PLACED_STATES]
        gr = check_agent_guardrails(
            ticker=ticker, side=side, convictions=convictions,
            trades_today=placed_today,
            last_buy=memory.get_last_agent_buy(ticker), drawdown_pct=drawdown,
            settled_cash=port["buying_power"], estimated_cost=estimated_cost, now=now,
        )
        if not gr.passed:
            logger.warning("Autotrader BLOCKED %s %s — %s", side, ticker, gr.block_reason)
            memory.log_agent_trade(
                ticker=ticker, side=side, run_date=run_date, status="blocked",
                rule_checks={**gr.checks, "block_reason": gr.block_reason},
                mirrors_paper_trade_id=m.get("mirrors_paper_trade_id"), is_open=False,
            )
            summary["blocked"].append(f"{side}:{ticker}")
            continue

        _execute_one(broker, ticker, side, held, estimated_cost, m, gr,
                     run_date, dry_run, summary, trades_today)

    logger.info("Autotrader run complete — placed=%d blocked=%d failed=%d skipped=%d dry_run=%s",
                len(summary["placed"]), len(summary["blocked"]), len(summary["failed"]), len(summary["skipped"]), dry_run)
    _finalize(summary, num_mirrors=len(mirrors))
    return summary


def _execute_one(broker, ticker, side, held, estimated_cost, mirror, gr,
                 run_date, dry_run, summary, trades_today) -> None:
    """Build a market order (buy → dollar_amount, sell → full quantity), review, then
    place (or stop at review in dry-run), and log the agent_trades row.

    Market + dollar sizing is the fractional-share fit for the small (~$1k) agentic
    account — Robinhood permits fractional shares only on market orders.
    """
    # Sizing → order params.
    quantity: float | None = None
    dollar_amount: float | None = None
    order_params: dict = dict(
        symbol=ticker, side=side, type="market",
        market_hours="regular_hours", time_in_force="gfd",
    )
    if side == "buy":
        if not estimated_cost or estimated_cost <= 0:
            logger.warning("Autotrader: non-positive buy budget for %s — skipping.", ticker)
            summary["skipped"].append(f"buy:{ticker}:bad_size")
            return
        dollar_amount = round(estimated_cost, 2)
        order_params["dollar_amount"] = f"{dollar_amount:.2f}"
    else:
        # Sell only settled shares (shares_available_for_sells) to avoid a Good-Faith Violation.
        quantity = held.get("sellable") or held.get("shares")
        if not quantity or quantity <= 0:
            logger.warning("Autotrader: no sellable shares for %s — skipping.", ticker)
            summary["skipped"].append(f"sell:{ticker}:bad_qty")
            return
        order_params["quantity"] = str(quantity)

    # ref_id is a place_equity_order-only idempotency key. review_equity_order rejects it as an
    # unexpected property, so it must NOT go into the shared order_params (which review also uses)
    # — it is passed to place_order alone, below.
    ref_id = str(uuid.uuid4())
    rule_checks = {**gr.checks, "dollar_amount": dollar_amount, "quantity": quantity}
    submitted_at = datetime.now(timezone.utc).isoformat()
    size_desc = f"${dollar_amount:.2f}" if side == "buy" else f"x{quantity}"

    # Review is always safe (no order) and surfaces pre-trade alerts. Dry-run stops here.
    try:
        broker.review_order(**order_params)
    except BrokerError:
        logger.error("Autotrader: review failed for %s %s — not placing.", side, ticker, exc_info=True)
        summary["skipped"].append(f"{side}:{ticker}:review_failed")
        return

    if dry_run:
        tid = memory.log_agent_trade(
            ticker=ticker, side=side, run_date=run_date, order_type="market",
            quantity=quantity, dollar_amount=dollar_amount, ref_id=ref_id, status="dry_run",
            rule_checks=rule_checks, mirrors_paper_trade_id=mirror.get("mirrors_paper_trade_id"),
            submitted_at=submitted_at, is_open=False,
        )
        logger.info("Autotrader DRY-RUN %s %s %s (id=%s)", side, ticker, size_desc, tid)
        summary["placed"].append({"ticker": ticker, "side": side, "dry_run": True, "id": tid})
        trades_today.append({"ticker": ticker, "side": side, "status": "dry_run"})
        return

    # Live placement — ref_id (idempotency) is accepted only here, not by review. A place
    # failure (e.g. a Robinhood account gate, a transient broker error) is logged as `failed`
    # and the run continues to the next mirror — one bad order must not abort the whole run.
    try:
        resp = broker.place_order(ref_id=ref_id, **order_params)
    except BrokerError as e:
        logger.error("Autotrader: place failed for %s %s — %s", side, ticker, e, exc_info=True)
        memory.log_agent_trade(
            ticker=ticker, side=side, run_date=run_date, order_type="market",
            quantity=quantity, dollar_amount=dollar_amount, ref_id=ref_id, status="failed",
            rule_checks={**rule_checks, "place_error": str(e)},
            mirrors_paper_trade_id=mirror.get("mirrors_paper_trade_id"),
            submitted_at=submitted_at, is_open=False,
        )
        summary["failed"].append(f"{side}:{ticker}")
        return
    order_id, raw_state = extract_order(resp)
    status = raw_state if raw_state in _PLACED_STATES else "submitted"
    tid = memory.log_agent_trade(
        ticker=ticker, side=side, run_date=run_date, order_type="market",
        quantity=quantity, dollar_amount=dollar_amount, order_id=order_id, ref_id=ref_id,
        status=status if status in _PLACED_STATES else "submitted", rule_checks=rule_checks,
        mirrors_paper_trade_id=mirror.get("mirrors_paper_trade_id"), submitted_at=submitted_at,
    )
    logger.info("Autotrader PLACED %s %s %s (order_id=%s, id=%s)", side, ticker, size_desc, order_id, tid)
    summary["placed"].append({"ticker": ticker, "side": side, "order_id": order_id, "id": tid})
    trades_today.append({"ticker": ticker, "side": side, "status": "submitted"})


if __name__ == "__main__":
    import argparse

    from src.logger import setup as _setup
    _setup()
    parser = argparse.ArgumentParser(description="ASTRA Autotrader — autonomous execution")
    parser.add_argument("--dry-run", action="store_true", help="review only, never place (forces dry-run)")
    parser.add_argument("--live", action="store_true", help="force live even if AGENT_TRADING_ENABLED is unset")
    args = parser.parse_args()
    forced = True if args.dry_run else (False if args.live else None)
    result = run(dry_run=forced)
    if result.get("aborted"):
        # Surface the failure to CI (non-zero exit) so a broken token doesn't read as success.
        raise SystemExit(1)
