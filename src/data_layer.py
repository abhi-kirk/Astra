"""
Data layer: portfolio positions, cost basis, and market data.

Portfolio state comes from two sources:
  - data/live_portfolio.json  — written by Claude via Robinhood MCP (refreshed each session)
  - data/portfolio_history.csv — historical trades, used for cost basis calculations

Market/fundamental data comes from yfinance (free, EOD).
"""

import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).parent.parent
HISTORY_CSV = ROOT / "data" / "portfolio_history.csv"
LIVE_PORTFOLIO = ROOT / "data" / "live_portfolio.json"
CONVICTIONS = ROOT / "convictions.json"


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------

def load_convictions() -> dict:
    with open(CONVICTIONS) as f:
        return json.load(f)


def get_cost_basis_from_csv() -> dict[str, dict]:
    """
    Compute per-ticker cost basis and estimated open positions from trade history CSV.
    Returns {ticker: {shares, avg_cost, total_invested, num_buys, num_sells}}
    """
    df = pd.read_csv(HISTORY_CSV, engine="python", quotechar='"', on_bad_lines="skip")
    df.columns = [c.strip() for c in df.columns]

    def clean_dollar(val):
        if pd.isna(val):
            return None
        v = str(val).replace("$", "").replace(",", "").replace("(", "").replace(")", "").strip()
        try:
            return float(v)
        except ValueError:
            return None

    df["amount"] = df["Amount"].apply(clean_dollar)
    df["price"] = df["Price"].apply(clean_dollar)
    df["qty"] = pd.to_numeric(df["Quantity"], errors="coerce")
    df["date"] = pd.to_datetime(df["Activity Date"], errors="coerce")

    trades = df[df["Trans Code"].isin(["Buy", "Sell"])].copy()
    result = {}

    for ticker, group in trades.groupby("Instrument"):
        buys = group[group["Trans Code"] == "Buy"]
        sells = group[group["Trans Code"] == "Sell"]

        buy_qty = buys["qty"].sum()
        sell_qty = sells["qty"].sum()
        net_shares = round(buy_qty - sell_qty, 6)

        buy_amt = buys["amount"].sum()
        sell_amt = sells["amount"].sum()
        net_cost = round(buy_amt - sell_amt, 2)

        avg_cost = round(net_cost / net_shares, 4) if net_shares > 0.01 else 0

        last_buy = buys.sort_values("date").iloc[-1] if len(buys) else None

        result[ticker] = {
            "shares": net_shares,
            "avg_cost": avg_cost,
            "total_invested": round(net_cost, 2),
            "num_buys": len(buys),
            "num_sells": len(sells),
            "last_buy_price": last_buy["price"] if last_buy is not None else None,
            "last_buy_date": str(last_buy["date"].date()) if last_buy is not None else None,
        }

    # Return only open positions
    return {t: v for t, v in result.items() if v["shares"] > 0.01}


def get_live_portfolio() -> dict[str, dict] | None:
    """
    Load live portfolio snapshot written by Claude via Robinhood MCP.
    Returns None if no snapshot exists (fall back to CSV).
    """
    if not LIVE_PORTFOLIO.exists():
        return None
    with open(LIVE_PORTFOLIO) as f:
        return json.load(f)


def get_portfolio() -> dict[str, dict]:
    """
    Best available portfolio data. Live MCP snapshot if fresh (<24h), else CSV cost basis.
    """
    live = get_live_portfolio()
    if live:
        ts = live.get("_snapshot_time", "")
        if ts:
            age = datetime.now() - datetime.fromisoformat(ts)
            if age < timedelta(hours=24):
                return live.get("positions", {})

    return get_cost_basis_from_csv()


def save_mcp_portfolio_snapshot(positions: dict):
    """
    Called by Claude after reading Robinhood MCP. Writes live_portfolio.json.
    positions: {ticker: {shares, current_price, market_value, avg_cost, ...}}
    """
    snapshot = {
        "_snapshot_time": datetime.now().isoformat(),
        "_source": "robinhood_mcp",
        "positions": positions,
    }
    LIVE_PORTFOLIO.write_text(json.dumps(snapshot, indent=2))
    print(f"Saved live portfolio snapshot: {len(positions)} positions")


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def get_market_data(ticker: str, period_days: int = 365) -> dict:
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
            "revenue_growth_yoy": info.get("revenueGrowth"),   # decimal e.g. 0.15 = 15%
            "gross_margins": info.get("grossMargins"),          # decimal
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


def get_market_data_bulk(tickers: list[str]) -> dict[str, dict]:
    """Fetch market data for multiple tickers, skipping errors."""
    results = {}
    for ticker in tickers:
        print(f"  Fetching {ticker}...")
        results[ticker] = get_market_data(ticker)
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
