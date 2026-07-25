"""
Market context — pure, deterministic quant signals for the contextual reasoning layer.

Two signals, both computed from data already on hand (the `returns_recent` series retained by
`data_layer.get_market_data`), so they add no fetches:

  • beta_decomposition — split a name's recent drop into market-beta vs idiosyncratic residual.
    "80% of NVDA's 2-week drop is explained by market beta, residual ~0" is the literal encoding
    of *the drop has nothing to do with the stock* — the discriminator behind the macro_dip play.
  • selloff_state — is the broad market (SPY) in a genuine sentiment-driven selloff? Velocity
    (SPY N-day return) + breadth (% of names down), so a −4% panic registers and a slow grind
    doesn't masquerade as one.

Pure functions in, plain dicts out — no I/O, no config mutation. Missing/short history degrades
to None / neutral so the caller (playbook.py) simply emits no proposal rather than crashing.
"""

from __future__ import annotations

from src.brain import params


def _cumret(returns: list[float]) -> float:
    """Compound a list of simple daily returns into a cumulative return (fraction)."""
    acc = 1.0
    for r in returns:
        acc *= (1.0 + r)
    return acc - 1.0


def beta_decomposition(
    ticker_returns: list[float] | None,
    spy_returns: list[float] | None,
) -> dict | None:
    """Decompose a name's recent move into a market-beta component and an idiosyncratic residual.

    `ticker_returns` / `spy_returns` are recent daily-return series (most-recent-last), as retained
    in market_data["returns_recent"]. Returns None when there isn't enough aligned history.

    Output (all return figures in **percent**, i.e. −5.0 = −5%):
      beta                      — OLS slope of the name's returns on SPY's over the estimation window
      r2                        — fit quality (0..1); low r2 → the beta read is unreliable
      name_move_pct             — the name's cumulative return over the drop window
      market_component_pct      — beta · SPY's cumulative return over the drop window
      idiosyncratic_residual_pct— name_move − market_component (the part NOT explained by the market)
      macro_fraction            — market_component / name_move (fraction of the move that is market)
    """
    if not ticker_returns or not spy_returns:
        return None

    window = params.BETA_WINDOW_DAYS
    drop = params.BETA_DROP_WINDOW_DAYS
    # Align by recency (same trading calendar → same length; guard the ragged case).
    n = min(len(ticker_returns), len(spy_returns))
    if n < window + drop:
        return None
    tk_all = ticker_returns[-n:]
    sp_all = spy_returns[-n:]

    # Estimate beta on the PRE-drop baseline (the `window` returns ending just before the drop
    # window), so a recent idiosyncratic move doesn't inflate beta and get mis-attributed to the
    # market — the whole point is to tell a market-driven drop from a company-specific one.
    tk = tk_all[-(window + drop):-drop]
    sp = sp_all[-(window + drop):-drop]

    mean_tk = sum(tk) / len(tk)
    mean_sp = sum(sp) / len(sp)
    cov = sum((a - mean_tk) * (b - mean_sp) for a, b in zip(tk, sp)) / len(tk)
    var_sp = sum((b - mean_sp) ** 2 for b in sp) / len(sp)
    if var_sp <= 0:
        return None
    beta = cov / var_sp

    # r² of the single-factor fit (how much of the name's variance SPY explains).
    var_tk = sum((a - mean_tk) ** 2 for a in tk) / len(tk)
    r2 = (cov * cov) / (var_sp * var_tk) if var_tk > 0 else 0.0

    name_move = _cumret(tk_all[-drop:])
    spy_move = _cumret(sp_all[-drop:])
    market_component = beta * spy_move
    residual = name_move - market_component
    macro_fraction = (market_component / name_move) if name_move != 0 else None

    return {
        "beta": round(beta, 4),
        "r2": round(r2, 4),
        "name_move_pct": round(name_move * 100, 3),
        "market_component_pct": round(market_component * 100, 3),
        "idiosyncratic_residual_pct": round(residual * 100, 3),
        "macro_fraction": round(macro_fraction, 4) if macro_fraction is not None else None,
    }


def selloff_state(
    spy_returns: list[float] | None,
    market_data: dict[str, dict],
) -> dict:
    """Is the broad market in a genuine sentiment-driven selloff right now?

    Velocity: SPY's cumulative return over the last SELLOFF_NDAY days. Breadth: the share of
    screened names whose own N-day return is negative. `is_panic` requires both a sharp SPY drop
    and wide breadth — a broad, fast risk-off move, not a single-name or slow-grind decline.
    """
    nday = params.SELLOFF_NDAY
    spy_nday_return = None
    if spy_returns and len(spy_returns) >= nday:
        spy_nday_return = round(_cumret(spy_returns[-nday:]) * 100, 3)

    downs = 0
    total = 0
    for md in market_data.values():
        rr = md.get("returns_recent")
        if not rr or len(rr) < nday:
            continue
        total += 1
        if _cumret(rr[-nday:]) < 0:
            downs += 1
    breadth_pct_down = round(downs / total * 100, 1) if total else None

    is_panic = bool(
        spy_nday_return is not None
        and breadth_pct_down is not None
        and spy_nday_return <= params.SELLOFF_PANIC_RETURN_PCT
        and breadth_pct_down >= params.SELLOFF_BREADTH_MIN_PCT
    )

    return {
        "spy_nday_return_pct": spy_nday_return,
        "nday": nday,
        "breadth_pct_down": breadth_pct_down,
        "is_panic": is_panic,
    }
