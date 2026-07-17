"""
Performance-measurement helpers for the equity-vs-benchmark charts.

Pure functions (no I/O) so the same methodology is shared by the run-time writers and the
unit tests. Two tracks feed the dashboard charts:

- **Autotrader** — a real cash account that takes deposits. We report a time-weighted return
  (TWR) via geometrically-linked Modified Dietz, which strips out deposit/withdrawal timing so
  the curve is comparable to a market index (GIPS-standard rationale).
- **Paper book** — a notional fixed-capital account with *no* external cash flows, so its
  NAV index is simply ``total_equity / nav0 * 100``. `reconcile_notional_book` advances that
  book one run forward.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def dietz_return(begin_value: float, end_value: float, net_flow: float,
                 flow_weight: float = 0.5) -> float:
    """Modified Dietz period return: ``(V1 − V0 − CF) / (V0 + w·CF)``.

    `net_flow` (CF) is external cash in (+) / out (−) during the period; `flow_weight` (w) is
    the fraction of the period the flow was invested — 0.5 (mid-period) since we only have
    per-run snapshots, not exact intra-period deposit dates. Returns 0.0 if the weighted
    capital base is zero (nothing invested → no return to measure)."""
    denom = begin_value + flow_weight * net_flow
    if denom == 0:
        return 0.0
    return (end_value - begin_value - net_flow) / denom


def chain_nav(prev_nav: float, period_return: float) -> float:
    """Geometrically link one period's return onto a NAV index (seed 100)."""
    return prev_nav * (1.0 + period_return)


def reconcile_notional_book(
    prev_holdings: list[dict],
    open_lots: list[dict],
    prev_cash: float,
    current_prices: dict[str, float],
    nav0: float,
    max_position_pct: float,
    default_pct: float,
) -> dict:
    """Advance the notional paper book one run forward, marked to `current_prices`.

    The book is a fixed-capital account (starts at `nav0`) whose lots mirror ASTRA's real
    `paper_trades` open lots but sized off `nav0` instead of Abhi's live Individual-account
    book — an isolated "how good are the picks" portfolio. Because the real book cannot be
    replayed (partial trims mutate the lot row in place with no recorded trim price), we carry
    the notional ledger forward each run and reconcile it against today's live open lots:

    - **New lot** (open lot id absent from the carry): deploy ``min(pct, max_position_pct) ×
      nav0`` capped at available cash; buy `notional_shares` at `price_at_signal`.
    - **Trim** (live `virtual_shares` < carried `virtual_shares_ref`): scale `notional_shares`
      by the same ratio; realize the sold portion at today's price (the exact trim price is
      not stored — marking at the run's EOD price matches our snapshot model).
    - **Full close** (carried lot id no longer open): realize all `notional_shares` at today's
      price back into cash.

    `open_lots` are rows from `memory.get_open_paper_trades()` (need `id`, `ticker`,
    `virtual_shares`, `price_at_signal`, `suggested_position_pct`). A ticker missing a current
    price falls back to its entry price (no phantom move) and is logged. Returns a dict with
    the new `holdings`, `cash`, `market_value`, `invested_cost`, `unrealized`, and
    `realized_delta` (realized P&L booked this run)."""
    cash = prev_cash
    realized_delta = 0.0
    prev_by_id = {h["lot_id"]: h for h in prev_holdings}
    open_by_id = {lot["id"]: lot for lot in open_lots}

    def _price(ticker: str, fallback: float) -> float:
        px = current_prices.get(ticker)
        if px is None:
            logger.warning("Paper book: no current price for %s — holding at entry.", ticker)
            return fallback
        return px

    # Full closes — carried lots that are no longer open.
    for lot_id, h in prev_by_id.items():
        if lot_id not in open_by_id:
            px = _price(h["ticker"], h["entry_price"])
            cash += h["notional_shares"] * px
            realized_delta += (px - h["entry_price"]) * h["notional_shares"]

    new_holdings: list[dict] = []
    for lot in open_lots:
        lot_id = lot["id"]
        ticker = lot["ticker"]
        h = prev_by_id.get(lot_id)
        if h is not None:
            # Existing lot — detect a trim by the shrunk share count.
            ref = h["virtual_shares_ref"] or 0
            live = lot["virtual_shares"] or 0
            notional = h["notional_shares"]
            if ref and live < ref:
                ratio = live / ref
                sold = notional * (1.0 - ratio)
                px = _price(ticker, h["entry_price"])
                cash += sold * px
                realized_delta += (px - h["entry_price"]) * sold
                notional *= ratio
            new_holdings.append({
                "lot_id": lot_id, "ticker": ticker,
                "notional_shares": notional, "entry_price": h["entry_price"],
                "virtual_shares_ref": live,
            })
        else:
            # New lot — deploy off nav0, capped by the per-name limit and available cash.
            entry = lot.get("price_at_signal") or 0
            pct = lot.get("suggested_position_pct") or default_pct
            deploy = max(0.0, min(pct * nav0, max_position_pct * nav0, cash))
            notional = deploy / entry if entry else 0.0
            cash -= deploy
            new_holdings.append({
                "lot_id": lot_id, "ticker": ticker,
                "notional_shares": notional, "entry_price": entry,
                "virtual_shares_ref": lot["virtual_shares"],
            })

    market_value = 0.0
    invested_cost = 0.0
    unrealized = 0.0
    for h in new_holdings:
        px = _price(h["ticker"], h["entry_price"])
        market_value += h["notional_shares"] * px
        invested_cost += h["notional_shares"] * h["entry_price"]
        unrealized += (px - h["entry_price"]) * h["notional_shares"]

    return {
        "holdings": new_holdings,
        "cash": cash,
        "market_value": market_value,
        "invested_cost": invested_cost,
        "unrealized": unrealized,
        "realized_delta": realized_delta,
    }
