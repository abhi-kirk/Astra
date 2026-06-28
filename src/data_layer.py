"""
Data layer: portfolio positions, cost basis, and market data.

Portfolio state comes from two sources:
  - Supabase portfolio_snapshots table (written by Claude after Robinhood MCP read)
  - data/portfolio_history.csv (local fallback for cost basis when no live snapshot)

Convictions come from Supabase convictions table (single source of truth).
Market/fundamental data comes from yfinance (free, EOD).
"""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

from src.config import MARKET_DATA_PERIOD_DAYS, MARKET_DATA_WORKERS

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
    rows = get_client().table("trades").select(
        "ticker, trans_code, quantity, price, amount, activity_date"
    ).in_("trans_code", ["Buy", "Sell"]).execute().data or []

    buys: dict = defaultdict(lambda: {"qty": 0.0, "amt": 0.0, "count": 0, "last_price": None, "last_date": None})
    sells: dict = defaultdict(lambda: {"qty": 0.0, "amt": 0.0, "count": 0})

    for r in rows:
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
    Priority: Supabase MCP snapshot (<24h) → Supabase trades table → local CSV.
    """
    from src.memory import get_latest_portfolio_snapshot
    try:
        snapshot = get_latest_portfolio_snapshot()
        if snapshot:
            ts = datetime.fromisoformat(snapshot["snapshot_time"].replace("Z", ""))
            if datetime.now() - ts < timedelta(hours=24):
                return snapshot["positions"]
    except Exception:
        pass

    try:
        return get_cost_basis_from_db()
    except Exception:
        pass

    return get_cost_basis_from_csv()


def save_mcp_portfolio_snapshot(positions: dict):
    """Called by Claude after reading Robinhood MCP. Persists to Supabase."""
    from src.memory import save_portfolio_snapshot
    save_portfolio_snapshot(positions)
    print(f"Saved portfolio snapshot to Supabase: {len(positions)} positions")


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def get_market_data(ticker: str, period_days: int = MARKET_DATA_PERIOD_DAYS) -> dict:
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

        info = tk.info or {}

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
    period_days: int = MARKET_DATA_PERIOD_DAYS,
) -> dict[str, dict]:
    """Fetch market data for multiple tickers concurrently."""
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=MARKET_DATA_WORKERS) as pool:
        futures = {pool.submit(get_market_data, t, period_days): t for t in tickers}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                results[t] = fut.result()
                status = "error" if "error" in results[t] else "✓"
                print(f"  {status} {t}")
            except Exception as e:
                results[t] = {"ticker": t, "error": str(e)}
                print(f"  ✗ {t}: {e}")
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
