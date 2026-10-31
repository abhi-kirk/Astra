"""
Autotrader orchestrator — mirror Advisor's paper decisions into real autonomous orders.

Runs as its own workflow, after Advisor's daily run has persisted the paper track. Flow:

    load agent_control → if paused/halted, exit (no execution)
    → snapshot agentic account (equity/cash) → compute drawdown → if breached, HALT + exit
    → read today's paper track (buys opened / positions closed) = mirror source
    → for each mirror: check_agent_guardrails → review_equity_order → place_equity_order
       (marketable limit, fresh ref_id) → log_agent_trade (link mirrors_paper_trade_id)

No LLM decides a sale. When a new buy cannot be funded, capital rotation
(src/brain/rotation.py) may sell one weaker sleeve holding and buy the next session,
after the cash settles. Nothing places a real order unless
`AGENT_TRADING_ENABLED` is True — otherwise every order runs in dry-run (review only).
The broker is injectable so the whole path is unit-tested with a fake.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from src import config, memory, notify, performance
from src.agent_broker import (
    AgenticBroker,
    BrokerError,
    extract_order,
    extract_portfolio,
    extract_positions,
    extract_realized_pnl,
)
from src.agent_guardrails import business_days_between, check_agent_guardrails
from src.brain.conviction import get_ticker_guidance, is_excluded
from src.brain.hard_rules import check_hard_rules, compute_portfolio_summary
from src.brain.rotation import plan_rotation
from src.data_layer import load_convictions
from src.observability import RunObserver

logger = logging.getLogger(__name__)

_PLACED_STATES = ("pending", "submitted", "filled")

# Full sells mirror only deliberate paper exits. A `blocked` close comes from a buy-side rule
# tripping on the MAIN portfolio (averaging-down cap, theme/position limit) and says
# nothing about the agentic position — never a real-money sell trigger.
# `parabolic_trim` is intentionally absent here: a trim keeps the paper lot open, so it never
# enters this close-based mirror — it flows through the separate trim-event source instead
# (memory.get_paper_trims_on → a partial sell of the agentic holding). See _build_mirrors.
_MIRRORED_CLOSE_REASONS = ("profit_take", "signal_inactive", "thesis_invalidation", "trailing_stop")


def _finalize(summary: dict, num_mirrors: int = 0, sleeve: dict | None = None) -> None:
    """Non-fatal side effects at run exit: persist the run + Telegram alert + observability."""
    try:
        # Persist the full run (placed/blocked/skipped+reasons/failed/halted/aborted) + sleeve
        # allocation math — previously this only reached Telegram/logs and was discarded.
        memory.log_agent_run(summary, sleeve, run_date=summary.get("run_date"))
    except Exception:
        logger.error("Autotrader run persistence failed — non-fatal", exc_info=True)
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


def _cost_basis(positions: dict[str, dict]) -> float:
    """Σ shares·avg_cost over open positions — the deployed cost basis (deposit-immune)."""
    return round(sum(p.get("shares", 0) * p.get("avg_cost", 0) for p in positions.values()), 4)


def _pnl_drawdown_pct(net_pnl: float, capital_base: float | None) -> float | None:
    """Net trading P&L as a % of net contributed capital. capital_base = total_equity −
    net_pnl reconstructs deposits-minus-withdrawals: it's stable under P&L (a gain lifts
    equity and net_pnl equally), rises on a deposit, falls on a withdrawal — so the halt is
    funding-immune with no stored baseline. None until any capital exists."""
    if not capital_base or capital_base <= 0:
        return None
    return round(net_pnl / capital_base * 100, 4)


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
    # Parabolic trims: partial sells that keep the paper lot open, so they never appear as a
    # close. Mirror the ACTION — sell `fraction` of whatever the agentic account holds — not the
    # paper dollars (basis-independent, matching how buys mirror the decision). `_execute_one`
    # sizes the real order off the live holding × fraction.
    for tr in memory.get_paper_trims_on(run_date):
        frac = tr.get("fraction")
        if not frac or frac <= 0 or frac >= 1:
            continue
        mirrors.append({
            "ticker": tr["ticker"], "side": "sell", "trim_fraction": frac,
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


def _position_value(positions: dict[str, dict], ticker: str) -> float:
    """Approximate current value of a held sleeve position, as shares × avg_cost.

    Robinhood's positions payload carries no per-position market value (see
    `agent_broker.extract_positions`), so cost basis is the proxy. It is close enough for a
    size floor — the sleeve's job here is "is this position dust or real", not precise marking —
    and it avoids a quote call per ticker on every run.
    """
    p = positions.get(ticker) or {}
    return (p.get("shares") or 0.0) * (p.get("avg_cost") or 0.0)


def _allocate_buys(buys: list[dict], budget: float, slots: int,
                   positions: dict[str, dict] | None = None) -> None:
    """Size today's buy mirrors together, in WHOLE lots of at least `min_position_usd`.

    The old behaviour split `budget` proportionally across the top `slots` names, so the number
    of funded names was fixed regardless of budget size — an $84 budget became six ~$14 lots and
    fragmented the sleeve (docs/capital_rotation.md §11). Now the budget buys whole positions:

      1. TOP-UPS first. A mirror whose ticker is already held BELOW the minimum is repaired to
         the floor, funded with exactly its shortfall. Repairing the book precedes widening it.
         Only names the brain is *already* signalling a buy on are eligible — the sleeve never
         invents a trade the brain did not authorize, so this stays a pure mirror.
      2. NEW LOTS with what remains: `floor(remaining / min_position)` names, strongest weight
         first, each guaranteed the minimum and sharing any surplus in proportion to weight.
      3. Everything else DEFERS to 0.0 — not lost, just first in line next run, by which point
         the budget has recovered.

    Annotates each mirror in place with `dollars` (and `topup` for reporting).
    """
    positions = positions or {}
    minimum = config.agent.min_position_usd
    slots = max(0, slots)
    for m in buys:
        m["dollars"] = 0.0

    def _weight(m: dict) -> float:
        return m.get("weight") or 0.0

    # Partition: held-below-minimum (repair) vs everything else (new lot / normal add).
    topups, entries = [], []
    for m in buys:
        shortfall = minimum - _position_value(positions, m["ticker"])
        if m["ticker"] in positions and shortfall > 0.01:
            m["topup"] = round(shortfall, 2)
            topups.append(m)
        else:
            entries.append(m)

    remaining = max(0.0, budget)
    used_slots = 0

    # 1. Repair pass — strongest conviction first, cheapest first as the tie-break.
    for m in sorted(topups, key=lambda x: (-_weight(x), x["topup"])):
        if used_slots >= slots:
            break
        if m["topup"] <= remaining:
            m["dollars"] = m["topup"]
            remaining = round(remaining - m["topup"], 2)
            used_slots += 1

    # 2. New lots — only whole ones, strongest weight first.
    free_slots = max(0, slots - used_slots)
    n_lots = min(int(remaining // minimum) if minimum > 0 else free_slots, free_slots)
    funded = sorted(entries, key=_weight, reverse=True)[:n_lots]
    if funded:
        surplus = max(0.0, remaining - minimum * len(funded))
        total_w = sum(_weight(m) for m in funded)
        for m in funded:
            share = (_weight(m) / total_w) if total_w > 0 else 1.0 / len(funded)
            m["dollars"] = round(minimum + surplus * share, 2)


def _feature_map(run_date: str) -> dict[str, dict]:
    """Today's OwnScore and price per ticker. Missing or failed read → empty (no rotation)."""
    try:
        rows = memory.get_decision_features_on(run_date)
    except Exception:
        logger.warning("Rotation: could not read today's scores — no swap this run.", exc_info=True)
        return {}
    return {r["ticker"]: r for r in rows if r.get("ticker")}


def _rotation_weight() -> float | None:
    """Playbook weight for a new swap, or None when rotation is switched off.

    A missing play row does not disable it — the config flag is the master switch.
    Disabling the play in the drawer does. A pending buy from a sale already placed
    is completed either way (the caller does not consult this for fills).
    """
    if not config.brain.capital_rotation_enabled:
        return None
    try:
        from src.brain.playbook import load_plays
        plays = (load_plays() or {}).get("plays") or []
    except Exception:
        logger.warning("Rotation: Playbook unreadable — using the published margins.", exc_info=True)
        return 1.0
    play = next((p for p in plays if p.get("name") == "capital_rotation"), None)
    if play is not None and not play.get("enabled", True):
        return None
    try:
        return float((play or {}).get("weight") or 1.0)
    except (TypeError, ValueError):
        return 1.0


def _cooled_tickers(now: datetime) -> set[str]:
    try:
        rows = memory.get_recent_rotation_intents()
    except Exception:
        logger.warning("Rotation: could not read recent swaps — treating none as cooling.", exc_info=True)
        return set()
    cooled: set[str] = set()
    window = config.brain.swap_cooldown_days
    for row in rows:
        if row.get("status") not in ("pending", "filled", "expired"):
            continue
        created = _as_naive(row.get("created_at"))
        if created is None or business_days_between(created, now) > window:
            continue
        for key in ("sell_ticker", "buy_ticker"):
            ticker = row.get(key)
            if isinstance(ticker, str) and ticker:
                cooled.add(ticker)
    return cooled


def _as_naive(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _expiry_date(now: datetime) -> str:
    """The last session on which a pending buy may still fill (trading days, not calendar)."""
    cur = now.date()
    left = config.brain.swap_intent_ttl_days
    while left > 0:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            left -= 1
    return cur.isoformat()


def _price(features: dict[str, dict], ticker: str, fallback: float) -> float:
    feat = features.get(ticker) or {}
    raw = (feat.get("features") or {}).get("current_price")
    try:
        price = float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        price = 0.0
    return price or fallback


def _intent_still_due(intent: dict, now: datetime) -> bool:
    created = _as_naive(intent.get("created_at")) or now
    return business_days_between(created, now) <= config.brain.swap_intent_ttl_days


def _prepare_rotation_fill(intent: dict, features: dict[str, dict], convictions: dict,
                           positions: dict[str, dict], now: datetime) -> dict | None:
    """A buy mirror for yesterday's sale, once the challenger still qualifies.

    Returns the mirror with dollars already set when today's budget can pay for the lot.
    Returns None when the idea has expired or no longer clears the original bar — the
    caller marks the intent expired and the cash stays put.
    """
    if not _intent_still_due(intent, now):
        memory.update_rotation_intent(intent["id"], "expired", reason="timed out before the buy qualified")
        logger.info("Rotation: %s intent expired unfilled.", intent.get("buy_ticker"))
        return None
    ticker = intent["buy_ticker"]
    feat = features.get(ticker) or {}
    score = feat.get("score_buy")
    try:
        score_f = float(score) if score is not None else None
    except (TypeError, ValueError):
        score_f = None
    margin = float(intent.get("margin") or 0)
    incumbent = float(intent.get("incumbent_score") or 0)
    still = feat.get("action") == "buy" and score_f is not None and score_f - incumbent >= margin - 1e-9
    if feat and not still:
        memory.update_rotation_intent(intent["id"], "expired", reason="the buy no longer clears the bar")
        logger.info("Rotation: %s no longer qualifies — cash stays unspent.", ticker)
        return None
    if not feat:
        return None  # today's scores are missing; keep the intent and try the next session
    if is_excluded(ticker, convictions):
        memory.update_rotation_intent(intent["id"], "expired", reason="excluded")
        return None
    md = {t: {"current_price": _price(features, t, p.get("avg_cost") or 0)} for t, p in positions.items()}
    port = {t: {"shares": p.get("shares") or 0, "avg_cost": p.get("avg_cost") or 0} for t, p in positions.items()}
    block = check_hard_rules(ticker, {}, md.get(ticker, {}), convictions, compute_portfolio_summary(port, md, convictions))
    if block:
        memory.update_rotation_intent(intent["id"], "expired", reason=block)
        logger.info("Rotation: %s blocked (%s) — cash stays unspent.", ticker, block)
        return None
    return {
        "ticker": ticker, "side": "buy", "weight": 1.0,
        "rotation_intent_id": intent["id"], "rotation_reserved": True,
        "desired_usd": config.agent.min_position_usd,
    }


def _plan_sleeve_rotation(unfunded: list[dict], positions: dict[str, dict], features: dict[str, dict],
                          convictions: dict, budget: float, now: datetime) -> dict | None:
    """A sell mirror, or None. Does not place anything."""
    weight = _rotation_weight()
    if weight is None or not unfunded:
        return None
    cooled = _cooled_tickers(now)
    challengers = []
    for m in unfunded:
        ticker = m["ticker"]
        if ticker in positions:
            continue  # an add to a name already held is the cash cushion's job, not a sale
        feat = features.get(ticker) or {}
        score = feat.get("score_buy")
        guidance = get_ticker_guidance(ticker, convictions)
        challengers.append({
            "ticker": ticker,
            "own_score": score,
            "desired_usd": config.agent.min_position_usd,
            "held": False,
            "hold_only": bool(guidance.get("hold_only")),
            "do_not_add": bool(guidance.get("do_not_add")),
            "excluded": is_excluded(ticker, convictions) is not None,
        })
    incumbents = []
    for ticker, pos in positions.items():
        feat = features.get(ticker) or {}
        guidance = get_ticker_guidance(ticker, convictions)
        last = memory.get_last_agent_buy(ticker)
        value = (pos.get("shares") or 0) * _price(features, ticker, pos.get("avg_cost") or 0)
        incumbents.append({
            "ticker": ticker,
            "own_score": feat.get("score_buy"),
            "value_usd": value,
            "intent": guidance.get("intent"),
            "last_buy_at": (last or {}).get("run_date"),
            "excluded": is_excluded(ticker, convictions) is not None,
        })
    plan = plan_rotation(challengers, incumbents, budget, weight=weight, cooled=cooled, now=now)
    if plan is None:
        return None
    mirror = {
        "ticker": plan.sell_ticker, "side": "sell", "source": "rotation",
        "rotation_for": plan.buy_ticker, "rotation_plan": plan,
    }
    if not plan.full_exit:
        mirror["trim_fraction"] = plan.sell_fraction
    mirror["shortfall"] = round(config.agent.min_position_usd - float(budget or 0), 2)
    logger.info("Rotation: %s", plan.reason)
    return mirror


def _record_rotation(mirror: dict, summary: dict, before: int, dry_run: bool, run_date: str) -> None:
    """Persist the sell→buy link once the order is actually in `placed`.

    A dry-run rehearses the sale and texts it, but does not leave a pending buy that
    the next live session would try to fill without a real sale behind it.
    """
    if len(summary.get("placed") or []) <= before:
        return
    placed = summary["placed"][-1]
    plan = mirror.get("rotation_plan")
    if plan is not None and placed.get("side") == "sell":
        summary["rotation"] = {"sell": plan.sell_ticker, "buy": plan.buy_ticker, "reason": plan.reason}
        if dry_run:
            return
        try:
            memory.save_rotation_intent({
                "book": "autotrader",
                "status": "pending",
                "sell_ticker": plan.sell_ticker,
                "sell_fraction": plan.sell_fraction,
                "sell_trade_id": placed.get("id"),
                "buy_ticker": plan.buy_ticker,
                "expected_proceeds": plan.expected_proceeds,
                "challenger_score": plan.challenger_score,
                "incumbent_score": plan.incumbent_score,
                "margin": plan.margin,
                "shortfall": mirror.get("shortfall"),
                "reason": plan.reason,
                "llm_influenced": False,
                "expires_on": _expiry_date(_as_naive(run_date) or datetime.now()),
            })
        except Exception:
            logger.error("Rotation: the sell was placed but the follow-up buy was not recorded", exc_info=True)
        return
    intent_id = mirror.get("rotation_intent_id")
    if intent_id and placed.get("side") == "buy" and not dry_run:
        try:
            memory.update_rotation_intent(intent_id, "filled", filled_trade_id=placed.get("id"))
        except Exception:
            logger.error("Rotation: the buy was placed but the intent was not closed", exc_info=True)


def run(run_date: str | None = None, broker: AgenticBroker | None = None,
        dry_run: bool | None = None) -> dict:
    """Execute Autotrader for one session. Returns a summary dict."""
    run_date = run_date or datetime.now(timezone.utc).isoformat()
    # Dry-run whenever the master switch is off, unless a caller explicitly forces live.
    if dry_run is None:
        dry_run = not config.agent.trading_enabled
    summary = {"run_date": run_date, "dry_run": dry_run, "placed": [], "blocked": [],
               "block_reasons": {}, "skipped": [], "failed": [], "halted": False}

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

    # Deposit/withdrawal-immune drawdown: measure trading P&L, not equity. net_pnl =
    # realized (all-time) + unrealized (market value of open stock − cost basis). Deposits
    # move cash but never touch realized/unrealized P&L, so no manual baseline reset is ever
    # needed. Realized is a non-fatal read — if it fails, unrealized alone still guards.
    equity = port["total_equity"]
    invested = _cost_basis(positions)
    unrealized = round(port["equity_value"] - invested, 4)
    try:
        realized = extract_realized_pnl(broker.get_realized_pnl())
    except BrokerError:
        logger.warning("Realized-P&L read failed — halt uses unrealized only this run.", exc_info=True)
        realized = 0.0
    net_pnl = round(realized + unrealized, 4)

    # capital_base = net contributed capital (deposits − withdrawals), reconstructed as
    # total_equity − net_pnl so no baseline is stored and funding events self-correct.
    capital_base = round(equity - net_pnl, 4)
    drawdown = _pnl_drawdown_pct(net_pnl, capital_base)

    # Sleeve state for the Telegram footer (equity / P&L / cash reserve). Set here so even a
    # halt/abort alert can carry it; deploy budget is added once the sleeve is sized (below).
    summary["account"] = {
        "equity": equity,
        "net_pnl": net_pnl,
        "net_pnl_pct": round(net_pnl / capital_base * 100, 2) if capital_base else None,
        "drawdown_pct": drawdown,
        "cash": port["cash"],
        "cash_pct": round(port["cash"] / equity * 100, 1) if equity else None,
    }

    # Time-weighted-return index (linked Modified Dietz): deposits/withdrawals since the last
    # snapshot are the change in capital_base (net contributed capital), so funding never fakes
    # a return move. Seeds at 100 on the first snapshot.
    prev = memory.get_latest_agent_snapshot()
    if prev is not None:
        prev_equity = float(prev["total_equity"])
        prev_capital_base = prev_equity - float(prev.get("net_pnl") or 0.0)
        net_flow = capital_base - prev_capital_base
        r = performance.dietz_return(prev_equity, equity, net_flow)
        nav_index = round(performance.chain_nav(float(prev.get("nav_index") or 100.0), r), 6)
    else:
        nav_index = 100.0

    memory.save_agent_account_snapshot({
        "cash": port["cash"], "buying_power": port["buying_power"],
        "market_value": port["equity_value"], "total_equity": equity,
        "positions": positions, "drawdown_pct": drawdown,
        "realized_pnl": realized, "unrealized_pnl": unrealized, "net_pnl": net_pnl,
        "invested_cost_basis": invested, "nav_index": nav_index,
    })

    if drawdown is not None and drawdown <= config.agent.drawdown_halt_pct:
        reason = (
            f"P&L drawdown {drawdown:.1f}% ≤ halt {config.agent.drawdown_halt_pct:.0f}% "
            f"(net P&L ${net_pnl:,.2f} on ${capital_base:,.2f} contributed)"
        )
        memory.set_agent_halted(True, reason)
        logger.error("Autotrader DRAWDOWN HALT — %s. Execution stopped.", reason)
        summary["halted"] = True
        _finalize(summary)
        return summary

    # -- mirror source + shared context ----------------------------------
    mirrors = _build_mirrors(run_date)
    now = datetime.now()
    convictions = load_convictions()
    features = _feature_map(run_date)
    # A sale placed on an earlier session funds its buy here, even when the paper book
    # opened nothing new today (the lot is already open; the sleeve is catching up).
    try:
        pending_intent = memory.get_pending_rotation_intent()
    except Exception:
        logger.warning("Rotation: could not read a pending buy — continuing without it.", exc_info=True)
        pending_intent = None
    if pending_intent:
        fill = _prepare_rotation_fill(pending_intent, features, convictions, positions, now)
        if fill and fill["ticker"] in positions:
            try:
                memory.update_rotation_intent(
                    fill["rotation_intent_id"], "expired", reason="already held",
                )
            except Exception:
                logger.warning("Rotation: could not close an already-held buy.", exc_info=True)
        elif fill:
            # The paper book may already want this name today. One order, tagged so the
            # sale's intent closes when that order is placed.
            existing = next(
                (m for m in mirrors if m.get("side") == "buy" and m.get("ticker") == fill["ticker"]),
                None,
            )
            if existing:
                existing["rotation_intent_id"] = fill["rotation_intent_id"]
                existing["rotation_reserved"] = True
                existing["desired_usd"] = fill["desired_usd"]
            else:
                mirrors.insert(0, fill)
    if not mirrors:
        logger.info("No paper decisions to mirror today — nothing to execute.")
        _finalize(summary)
        return summary

    trades_today = memory.get_agent_trades_today(run_date)

    # Size today's buys together: rank by the brain's conviction weight, deploy a capped slice
    # of remaining cash (never below the reserve floor), in whole lots. A rotation fill is
    # reserved first — the sale was made for that buy — and the surplus is not spent on a
    # second name. Sells don't draw on this budget.
    placed_buys_today = sum(
        1 for t in trades_today if t.get("side") == "buy" and t.get("status") in _PLACED_STATES
    )
    buy_slots = config.agent.max_trades_per_day - placed_buys_today
    sleeve_budget = _sleeve_budget(port["buying_power"], equity)
    summary["account"]["budget"] = sleeve_budget
    reserved = [m for m in mirrors if m.get("rotation_reserved")]
    if reserved and sleeve_budget >= config.agent.min_position_usd:
        lot = config.agent.min_position_usd
        # The sale's proceeds are for this buy. Other buys may use only the cash that was
        # already available before it.
        proceeds = float((pending_intent or {}).get("expected_proceeds") or 0)
        organic = max(0.0, sleeve_budget - proceeds)
        reserved[0]["dollars"] = lot
        others_budget = min(organic, max(0.0, sleeve_budget - lot))
        buy_slots = max(0, buy_slots - 1)
    else:
        others_budget = sleeve_budget
    pending_buys = [
        m for m in mirrors
        if m["side"] == "buy" and not m.get("rotation_reserved")
        and not _already_handled(trades_today, m["ticker"], "buy")
    ]
    _allocate_buys(pending_buys, others_budget, buy_slots, positions)
    # One new swap per session, and never on a day we are already completing one.
    if not reserved:
        unfunded = [m for m in pending_buys if not m.get("dollars")]
        sell = _plan_sleeve_rotation(unfunded, positions, features, convictions, sleeve_budget, now)
        if sell:
            mirrors.append(sell)
    sleeve = {
        "budget": sleeve_budget,
        "buy_slots": buy_slots,
        "buying_power": port["buying_power"],
        "equity": equity,
        "min_position_usd": config.agent.min_position_usd,
        "allocations": [
            {"ticker": m["ticker"], "weight": m.get("weight"), "dollars": m.get("dollars"),
             "topup": m.get("topup")}
            for m in pending_buys
        ],
    }

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
            summary["block_reasons"][f"{side}:{ticker}"] = gr.block_reason
            continue

        before = len(summary["placed"])
        _execute_one(broker, ticker, side, held, estimated_cost, m, gr,
                     run_date, dry_run, summary, trades_today)
        _record_rotation(m, summary, before, dry_run, run_date)

    logger.info("Autotrader run complete — placed=%d blocked=%d failed=%d skipped=%d dry_run=%s",
                len(summary["placed"]), len(summary["blocked"]), len(summary["failed"]), len(summary["skipped"]), dry_run)
    _finalize(summary, num_mirrors=len(mirrors), sleeve=sleeve)
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
        # Parabolic trim → sell only `trim_fraction` of the agentic holding (keep the runner);
        # a full-close mirror carries no fraction and sells the whole sellable quantity.
        trim_fraction = mirror.get("trim_fraction")
        if trim_fraction and 0 < trim_fraction < 1:
            quantity = round(quantity * trim_fraction, 6)
            if quantity <= 0:
                logger.warning("Autotrader: trim of %s rounds to zero shares — skipping.", ticker)
                summary["skipped"].append(f"sell:{ticker}:trim_zero")
                return
        order_params["quantity"] = str(quantity)

    # ref_id is a place_equity_order-only idempotency key. review_equity_order rejects it as an
    # unexpected property, so it must NOT go into the shared order_params (which review also uses)
    # — it is passed to place_order alone, below.
    ref_id = str(uuid.uuid4())
    rule_checks = {**gr.checks, "dollar_amount": dollar_amount, "quantity": quantity}
    if mirror.get("rotation_for"):
        rule_checks["rotation_for"] = mirror["rotation_for"]
    source = mirror.get("source") or "mirror"
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
            source=source, submitted_at=submitted_at, is_open=False,
        )
        logger.info("Autotrader DRY-RUN %s %s %s (id=%s)", side, ticker, size_desc, tid)
        summary["placed"].append({"ticker": ticker, "side": side, "dry_run": True, "id": tid,
                                   "dollars": dollar_amount, "shares": quantity, "status": "dry_run",
                                   "rotation_for": mirror.get("rotation_for")})
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
            source=source, submitted_at=submitted_at, is_open=False,
        )
        summary["failed"].append(f"{side}:{ticker}")
        return
    order_id, raw_state = extract_order(resp)
    status = raw_state if raw_state in _PLACED_STATES else "submitted"
    tid = memory.log_agent_trade(
        ticker=ticker, side=side, run_date=run_date, order_type="market",
        quantity=quantity, dollar_amount=dollar_amount, order_id=order_id, ref_id=ref_id,
        status=status if status in _PLACED_STATES else "submitted", rule_checks=rule_checks,
        mirrors_paper_trade_id=mirror.get("mirrors_paper_trade_id"), source=source,
        submitted_at=submitted_at,
    )
    logger.info("Autotrader PLACED %s %s %s (order_id=%s, id=%s)", side, ticker, size_desc, order_id, tid)
    summary["placed"].append({"ticker": ticker, "side": side, "order_id": order_id, "id": tid,
                              "dollars": dollar_amount, "shares": quantity, "status": status,
                              "rotation_for": mirror.get("rotation_for")})
    trades_today.append({"ticker": ticker, "side": side, "status": "submitted"})


if __name__ == "__main__":
    import argparse

    from src.logger import setup as _setup
    from src.settings import load_and_apply as _apply_settings
    _setup()
    # Dashboard-saved tunables layer over env/code before any guardrail is evaluated.
    # On/off switches AND together, so this can only ever disable trading, never enable it.
    _apply_settings()
    parser = argparse.ArgumentParser(description="ASTRA Autotrader — autonomous execution")
    parser.add_argument("--dry-run", action="store_true", help="review only, never place (forces dry-run)")
    parser.add_argument("--live", action="store_true", help="force live even if AGENT_TRADING_ENABLED is unset")
    args = parser.parse_args()
    forced = True if args.dry_run else (False if args.live else None)
    result = run(dry_run=forced)
    # Surface real failures to CI (non-zero exit) so a broken token doesn't read as success.
    # Also fail when ASTRA wanted to trade and *nothing* got placed — that run is not a success
    # even though the loop completed. A partial failure still exits 0 (some orders did land) and
    # is surfaced through the Telegram alert instead.
    if result.get("aborted") or (result.get("failed") and not result.get("placed")):
        raise SystemExit(1)
