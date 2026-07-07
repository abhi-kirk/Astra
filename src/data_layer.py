"""
Data layer: portfolio positions, cost basis, and market data.

Portfolio state comes from two sources:
  - Supabase portfolio_snapshots table (written by Claude after Robinhood MCP read)
  - data/portfolio_history.csv (local fallback for cost basis when no live snapshot)

Convictions come from Supabase convictions table (single source of truth).
Market/fundamental data comes from yfinance (free, EOD).
"""

# pandas' type stubs are imprecise about Series-vs-scalar, producing false positives
# on the numeric coercions below. Suppress those specific rules for this file only.
# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

from src import config

logger = logging.getLogger(__name__)
from src.db import rows as db_rows

ROOT = Path(__file__).parent.parent
HISTORY_CSV = ROOT / "data" / "portfolio_history.csv"


# ---------------------------------------------------------------------------
# Convictions
# ---------------------------------------------------------------------------

def load_convictions() -> dict:
    """Load convictions from Supabase (single source of truth)."""
    from src.memory import get_latest_convictions
    convictions = get_latest_convictions()
    if not convictions:
        raise RuntimeError("No convictions found in Supabase — run seed.py or update via Claude Code")
    return convictions


def get_cost_basis_from_db() -> dict[str, dict]:
    """Compute per-ticker cost basis from Supabase trades table."""
    from src.db import get_client
    trade_rows = db_rows(get_client().table("trades").select(
        "ticker, trans_code, quantity, price, amount, activity_date"
    ).in_("trans_code", ["Buy", "Sell"]).execute().data)

    buys: dict = defaultdict(lambda: {"qty": 0.0, "amt": 0.0, "count": 0, "last_price": None, "last_date": None})
    sells: dict = defaultdict(lambda: {"qty": 0.0, "amt": 0.0, "count": 0})

    for r in trade_rows:
        t = (r.get("ticker") or "").strip()
        if not t:
            continue
        qty = float(r["quantity"] or 0)
        amt = float(r["amount"] or 0)
        if r["trans_code"] == "Buy":
            buys[t]["qty"] += qty
            buys[t]["amt"] += amt
            buys[t]["count"] += 1
            buys[t]["last_price"] = r.get("price")
            buys[t]["last_date"] = r.get("activity_date")
        else:
            sells[t]["qty"] += qty
            sells[t]["amt"] += amt
            sells[t]["count"] += 1

    result = {}
    for ticker in buys:
        b, s = buys[ticker], sells.get(ticker, {"qty": 0.0, "amt": 0.0, "count": 0})
        net_shares = round(b["qty"] - s["qty"], 6)
        net_cost = round(b["amt"] - s["amt"], 2)
        if net_shares <= 0.01:
            continue
        result[ticker] = {
            "shares": net_shares,
            "avg_cost": round(net_cost / net_shares, 4),
            "total_invested": net_cost,
            "num_buys": b["count"],
            "num_sells": s["count"],
            "last_buy_price": b["last_price"],
            "last_buy_date": b["last_date"],
        }
    return result


def get_cost_basis_from_csv() -> dict[str, dict]:
    """Local CSV fallback — only used in dev when Supabase is unavailable."""
    if not HISTORY_CSV.exists():
        raise FileNotFoundError(f"CSV not found and Supabase unavailable: {HISTORY_CSV}")

    def clean_dollar(val):
        if pd.isna(val):
            return None
        v = str(val).replace("$", "").replace(",", "").replace("(", "").replace(")", "").strip()
        try:
            return float(v)
        except ValueError:
            return None

    df = pd.read_csv(HISTORY_CSV, engine="python", quotechar='"', on_bad_lines="skip")
    df.columns = [c.strip() for c in df.columns]
    df["amount"] = df["Amount"].apply(clean_dollar)
    df["price"] = df["Price"].apply(clean_dollar)
    df["qty"] = pd.to_numeric(df["Quantity"], errors="coerce")
    df["date"] = pd.to_datetime(df["Activity Date"], errors="coerce")

    trades = df[df["Trans Code"].isin(["Buy", "Sell"])].copy()
    result = {}
    for ticker, group in trades.groupby("Instrument"):
        buys = group[group["Trans Code"] == "Buy"]
        sells = group[group["Trans Code"] == "Sell"]
        net_shares = round(buys["qty"].sum() - sells["qty"].sum(), 6)
        net_cost = round(buys["amount"].sum() - sells["amount"].sum(), 2)
        if net_shares <= 0.01:
            continue
        last_buy = buys.sort_values("date").iloc[-1] if len(buys) else None
        result[ticker] = {
            "shares": net_shares,
            "avg_cost": round(net_cost / net_shares, 4) if net_shares > 0 else 0,
            "total_invested": net_cost,
            "num_buys": len(buys),
            "num_sells": len(sells),
            "last_buy_price": last_buy["price"] if last_buy is not None else None,
            "last_buy_date": str(last_buy["date"].date()) if last_buy is not None else None,
        }
    return result


def get_portfolio() -> dict[str, dict]:
    """
    Best available portfolio data.
    Priority: Supabase portfolio snapshot (any age) → Supabase trades table → local CSV.

    The trades table avg_cost is unreliable for tickers that had corporate actions
    (reverse splits, restructuring) after the CSV seed — the snapshot from the
    Robinhood API is always preferred even if stale, since daily runs keep it fresh.
    """
    from src.memory import get_latest_portfolio_snapshot
    try:
        snapshot = get_latest_portfolio_snapshot()
        if snapshot:
            ts  = datetime.fromisoformat(snapshot["snapshot_time"].replace("Z", ""))
            age = datetime.now() - ts
            if age > timedelta(hours=24):
                logger.warning(
                    f"Portfolio snapshot is {age.days}d {age.seconds // 3600}h old — "
                    "using it anyway (trades-table avg_cost unreliable for split/restructured tickers)"
                )
            return snapshot["positions"]
    except Exception:
        pass

    logger.warning(
        "No portfolio snapshot found — falling back to trades table. "
        "avg_cost will be wrong for any ticker that had a corporate action after the CSV seed."
    )
    try:
        return get_cost_basis_from_db()
    except Exception:
        pass

    return get_cost_basis_from_csv()


def save_mcp_portfolio_snapshot(positions: dict):
    """Called by Claude after reading Robinhood MCP. Persists to Supabase."""
    from src.memory import save_portfolio_snapshot
    save_portfolio_snapshot(positions)
    logger.info(f"Saved portfolio snapshot to Supabase: {len(positions)} positions")


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def get_market_data(ticker: str, period_days: int = config.market_data.period_days) -> dict:
    """
    Fetch price history, technical indicators, and key fundamentals via yfinance.
    Returns a flat dict suitable for strategy screening.
    """
    try:
        tk = yf.Ticker(ticker)
        hist = tk.history(period=f"{period_days}d")

        if hist.empty:
            return {"ticker": ticker, "error": "no_data"}

        closes = hist["Close"].dropna()
        if closes.empty:
            return {"ticker": ticker, "error": "no_close_data"}

        current_price = float(closes.iloc[-1])
        high_52w = float(closes.max())
        low_52w = float(closes.min())
        pct_below_52w_high = round((high_52w - current_price) / high_52w * 100, 2)

        rsi = _calc_rsi(hist["Close"])
        ma_50 = float(hist["Close"].tail(50).mean())
        ma_200 = float(hist["Close"].tail(200).mean()) if len(hist) >= 200 else None
        avg_vol_30d = float(hist["Volume"].tail(30).mean())
        atr_14 = _calc_atr(hist, config.brain.atr_period)
        recent_swing_high = float(hist["High"].tail(config.brain.swing_high_lookback).max())
        mom_12_1 = _calc_momentum(closes, config.brain.mom_lookback_days, config.brain.mom_skip_days)

        info = tk.info or {}
        revisions = _get_revisions(tk)

        return {
            "ticker": ticker,
            "current_price": current_price,
            "high_52w": high_52w,
            "low_52w": low_52w,
            "pct_below_52w_high": pct_below_52w_high,
            "rsi_14": rsi,
            "ma_50": round(ma_50, 2),
            "ma_200": round(ma_200, 2) if ma_200 else None,
            "price_vs_ma50_pct": round((current_price - ma_50) / ma_50 * 100, 2),
            "avg_volume_30d": int(avg_vol_30d),
            # Brain technicals
            "atr_14": round(atr_14, 4) if atr_14 else None,
            "recent_swing_high": round(recent_swing_high, 2),
            "mom_12_1": round(mom_12_1, 4) if mom_12_1 is not None else None,
            "revisions": revisions,
            # Fundamentals
            "market_cap": info.get("marketCap"),
            "pe_ratio": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "peg_ratio": info.get("pegRatio"),
            "revenue_growth_yoy": info.get("revenueGrowth"),
            "gross_margins": info.get("grossMargins"),
            "operating_margins": info.get("operatingMargins"),
            "debt_to_equity": info.get("debtToEquity"),
            "current_ratio": info.get("currentRatio"),
            "free_cashflow": info.get("freeCashflow"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "short_name": info.get("shortName"),
            "fetched_at": datetime.now().isoformat(),
        }

    except Exception as e:
        return {"ticker": ticker, "error": str(e)}


def get_market_data_bulk(
    tickers: list[str],
    period_days: int = config.market_data.period_days,
) -> dict[str, dict]:
    """Fetch market data for multiple tickers concurrently."""
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=config.market_data.workers) as pool:
        futures = {pool.submit(get_market_data, t, period_days): t for t in tickers}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                results[t] = fut.result()
                if "error" in results[t]:
                    logger.warning(f"Market data error for {t}: {results[t]['error']}")
                else:
                    logger.debug(f"Market data fetched: {t}")
            except Exception as e:
                results[t] = {"ticker": t, "error": str(e)}
                logger.error(f"Market data fetch failed for {t}: {e}")
    return results


def _calc_rsi(prices: pd.Series, period: int = 14) -> float | None:
    """Wilder RSI."""
    if len(prices) < period + 1:
        return None
    delta = prices.diff().dropna()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 2)


def _calc_atr(hist: pd.DataFrame, period: int) -> float | None:
    """Wilder Average True Range over `period`. Needs High/Low/Close."""
    if len(hist) < period + 1:
        return None
    high, low, close = hist["High"], hist["Low"], hist["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    return float(atr.iloc[-1])


def _calc_momentum(closes: pd.Series, lookback: int, skip: int) -> float | None:
    """12-1 style momentum: cumulative return over the lookback window ending `skip`
    days ago (skips the most recent month to avoid short-term reversal). Uses the
    oldest available bar as the base when full history is short (graceful degradation)."""
    if len(closes) <= skip + 20:
        return None
    base = float(closes.iloc[-min(lookback, len(closes))])
    recent = float(closes.iloc[-1 - skip])
    if base <= 0:
        return None
    return recent / base - 1.0


def _get_revisions(tk: "yf.Ticker") -> dict | None:
    """Compact analyst EPS-estimate revision counts (last 30d) from yfinance — free,
    best-effort. Returns {"up": n, "down": n} or None when there is no coverage
    (small caps / ADRs). Abstracted so an alternate source can slot in later."""
    try:
        er = tk.get_eps_revisions()
    except Exception:
        return None
    if er is None or getattr(er, "empty", True):
        return None
    try:
        row = er.loc["0y"] if "0y" in er.index else er.iloc[0]
        up = int(row.get("upLast30days", 0) or 0)
        down = int(row.get("downLast30days", 0) or 0)
    except Exception:
        return None
    if up == 0 and down == 0:
        return None
    return {"up": up, "down": down}
