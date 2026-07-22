"""
SEC EDGAR XBRL fundamentals — free, keyless small-cap financials.

Fills the quality-filter gap where yfinance is patchy and FMP's free tier paywalls: US
small-caps / recent IPOs (RKLB, ASTS, CHPT, CRON). Source is the official
``data.sec.gov`` companyfacts API — one HTTP call returns every XBRL-tagged fact across a
filer's entire history, so a weekly refresh of the whole book is a few dozen calls (no
250/day FMP ceiling, no 25/day Alpha Vantage ceiling). This is the *reliable* SEC REST
API, not the community-hosted SEC MCP we shelved for flakiness (see docs/research.md).

Design (see docs/research.md → "Small-cap fundamentals — free via SEC EDGAR XBRL"):
  - Fundamentals change ~4×/yr, so decouple them from the daily hot path: refresh a
    Supabase ``fundamentals`` cache weekly (``refresh_fundamentals``), read the cache in
    the daily run (``get_cached_fundamentals``).
  - Normalized to yfinance semantics so the quality filter is source-agnostic: margins as
    fractions, ``debt_to_equity`` percentage-scaled (×100), growth as a fraction.
  - Not covered — fall through to yfinance: ADRs / foreign private issuers (file 20-F,
    often no US-GAAP XBRL; absent from the ticker map) and ETFs (no financials).

CLI:  python -m src.fundamentals --refresh AAPL RKLB ASTS      # refresh named tickers
      python -m src.fundamentals --refresh                     # refresh held ∪ conviction set
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

import requests

from src import config

logger = logging.getLogger(__name__)

SEC_BASE = "https://data.sec.gov"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_HTTP_TIMEOUT = 15

# XBRL us-gaap tag candidates — filers tag the same line item under different concepts, and
# migrate between them over time, so we merge across all of these (see _merged_rows). Ordered
# most-specific→generic; on a period reported under several, the earlier-listed tag wins.
_REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
]
_GROSS_PROFIT_TAGS = ["GrossProfit"]
_OPERATING_INCOME_TAGS = ["OperatingIncomeLoss"]
_NET_INCOME_TAGS = ["NetIncomeLoss", "ProfitLoss"]
_CURRENT_ASSETS_TAGS = ["AssetsCurrent"]
_CURRENT_LIAB_TAGS = ["LiabilitiesCurrent"]
_EQUITY_TAGS = ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]
_LT_DEBT_TAGS = ["LongTermDebtNoncurrent", "LongTermDebt"]
_CURRENT_DEBT_TAGS = ["LongTermDebtCurrent", "DebtCurrent"]


def _headers() -> dict[str, str]:
    # SEC fair-use requires a descriptive User-Agent with a contact; a generic UA gets 403s.
    return {"User-Agent": config.services.sec_user_agent, "Accept-Encoding": "gzip, deflate"}


# ---------------------------------------------------------------------------
# CIK resolution
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_cik_map() -> dict[str, str]:
    """Map upper-cased ticker → zero-padded 10-digit CIK (SEC's company_tickers.json).

    Cached for the process lifetime. ADRs / foreign private issuers are absent here — that
    absence is exactly how ``resolve_cik`` reports "not an SEC XBRL filer, use yfinance".
    """
    resp = requests.get(TICKERS_URL, headers=_headers(), timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    out: dict[str, str] = {}
    for row in resp.json().values():
        ticker = str(row.get("ticker", "")).upper()
        cik = row.get("cik_str")
        if ticker and cik is not None:
            out[ticker] = f"{int(cik):010d}"
    return out


def resolve_cik(ticker: str) -> str | None:
    """Zero-padded CIK for a ticker, or None if it isn't a US XBRL filer (ADR/ETF/unknown)."""
    try:
        return get_cik_map().get(ticker.upper())
    except Exception as e:  # network / parse — treat as "unresolved", caller falls back
        logger.warning(f"SEC ticker→CIK map fetch failed: {e}")
        return None


# ---------------------------------------------------------------------------
# companyfacts fetch + XBRL extraction
# ---------------------------------------------------------------------------

def fetch_company_facts(cik: str) -> dict[str, Any]:
    """Raw companyfacts JSON for a zero-padded CIK (all facts, all history, one call)."""
    url = f"{SEC_BASE}/api/xbrl/companyfacts/CIK{cik}.json"
    resp = requests.get(url, headers=_headers(), timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _usgaap(facts: dict[str, Any]) -> dict[str, Any]:
    return facts.get("facts", {}).get("us-gaap", {})


def _merged_rows(gaap: dict[str, Any], tags: list[str]) -> list[dict[str, Any]]:
    """USD rows merged across all candidate tags, deduped by (start, end).

    Filers migrate the same line item between us-gaap concepts over time (e.g. NVDA/GOOGL
    moved revenue from ``RevenueFromContractWithCustomerExcludingAssessedTax`` to the generic
    ``Revenues``), so picking the first tag with *any* data freezes the series at the year the
    filer switched tags. Merging every candidate and deduping by period keeps the full history;
    on a period reported under multiple tags, the earlier-listed (more specific) tag wins.
    """
    by_key: dict[tuple[Any, Any], dict[str, Any]] = {}
    for tag in reversed(tags):  # reverse so index-0 (most-preferred) overwrites last
        for r in gaap.get(tag, {}).get("units", {}).get("USD", []) or []:
            by_key[(r.get("start"), r.get("end"))] = r
    return list(by_key.values())


def _quarterly_series(gaap: dict[str, Any], tags: list[str]) -> list[tuple[str, float]]:
    """Ascending [(period_end, value)] of pure-quarterly (≈3-month) duration facts.

    XBRL duration facts carry ``start``/``end``; a 10-Q also reports year-to-date spans, so
    we keep only ≈85–100-day windows to isolate single quarters, then dedup by end date
    (last write wins — restated/most-recent filing). Q4 is not filed as a 10-Q, so a true
    trailing-twelve-months sum from these will span the four *reported* quarters.
    """
    rows = _merged_rows(gaap, tags)
    by_end: dict[str, float] = {}
    for r in rows:
        start, end, val = r.get("start"), r.get("end"), r.get("val")
        if not start or not end or val is None:
            continue
        try:
            days = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days
        except ValueError:
            continue
        if 85 <= days <= 100:  # single quarter
            by_end[end] = float(val)
    return sorted(by_end.items())


def _annual_series(gaap: dict[str, Any], tags: list[str]) -> list[tuple[str, float]]:
    """Ascending [(period_end, value)] of full-year (≈365-day) duration facts."""
    rows = _merged_rows(gaap, tags)
    by_end: dict[str, float] = {}
    for r in rows:
        start, end, val = r.get("start"), r.get("end"), r.get("val")
        if not start or not end or val is None:
            continue
        try:
            days = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days
        except ValueError:
            continue
        if 350 <= days <= 380:
            by_end[end] = float(val)
    return sorted(by_end.items())


def _instant_latest(gaap: dict[str, Any], tags: list[str]) -> float | None:
    """Most-recent instantaneous (balance-sheet) value across the given tags."""
    rows = _merged_rows(gaap, tags)
    dated = [(r["end"], float(r["val"])) for r in rows if r.get("end") and r.get("val") is not None]
    if not dated:
        return None
    return max(dated, key=lambda x: x[0])[1]


def _ttm(series: list[tuple[str, float]]) -> tuple[float | None, float | None]:
    """(current TTM, prior TTM) sums from a quarterly series; None when <4 / <8 quarters."""
    vals = [v for _, v in series]
    ttm = sum(vals[-4:]) if len(vals) >= 4 else None
    prior = sum(vals[-8:-4]) if len(vals) >= 8 else None
    return ttm, prior


def _matched_ttm_ratio(
    numer: list[tuple[str, float]],
    denom: list[tuple[str, float]],
    quarters: int = 4,
) -> float | None:
    """TTM numer/denom over the last `quarters` denom periods, requiring a numer for each.

    Both sums must cover *identical* periods, else the ratio is meaningless. This guards the
    common case where a filer stopped reporting a line item years ago (Amazon/Google file no
    ``GrossProfit`` post-2009): its last-4 numerator quarters would otherwise be a decade older
    than revenue, yielding a nonsense margin. Unmatched → None (yfinance fills, or it's a
    line the filer genuinely doesn't report).
    """
    if len(denom) < quarters:
        return None
    num_by_end = dict(numer)
    recent = denom[-quarters:]
    if not all(end in num_by_end for end, _ in recent):
        return None
    den_sum = sum(v for _, v in recent)
    return sum(num_by_end[end] for end, _ in recent) / den_sum if den_sum else None


def extract_fundamentals(facts: dict[str, Any]) -> dict[str, Any]:
    """Normalize companyfacts JSON → the quality-filter's fundamentals subset.

    Fields mirror ``data_layer.get_market_data`` and yfinance semantics exactly:
    margins are fractions, ``debt_to_equity`` is percentage-scaled, growth is a fraction.
    Any field SEC doesn't cover cleanly is left None so the yfinance merge can fill it.
    """
    gaap = _usgaap(facts)

    rev_q = _quarterly_series(gaap, _REVENUE_TAGS)
    gp_q = _quarterly_series(gaap, _GROSS_PROFIT_TAGS)
    op_q = _quarterly_series(gaap, _OPERATING_INCOME_TAGS)

    rev_ttm, rev_prior = _ttm(rev_q)

    # Fall back to annual revenue for YoY growth when we lack 8 clean quarters (young filers).
    revenue_growth_yoy: float | None = None
    if rev_ttm is not None and rev_prior:
        revenue_growth_yoy = rev_ttm / rev_prior - 1
    else:
        rev_a = _annual_series(gaap, _REVENUE_TAGS)
        if len(rev_a) >= 2 and rev_a[-2][1]:
            revenue_growth_yoy = rev_a[-1][1] / rev_a[-2][1] - 1

    # Margins over period-matched quarters only (see _matched_ttm_ratio).
    gross_margins = _matched_ttm_ratio(gp_q, rev_q)
    operating_margins = _matched_ttm_ratio(op_q, rev_q)

    cur_assets = _instant_latest(gaap, _CURRENT_ASSETS_TAGS)
    cur_liab = _instant_latest(gaap, _CURRENT_LIAB_TAGS)
    current_ratio = cur_assets / cur_liab if cur_assets is not None and cur_liab else None

    equity = _instant_latest(gaap, _EQUITY_TAGS)
    lt_debt = _instant_latest(gaap, _LT_DEBT_TAGS) or 0.0
    cur_debt = _instant_latest(gaap, _CURRENT_DEBT_TAGS) or 0.0
    total_debt = lt_debt + cur_debt
    # yfinance debtToEquity is percentage-scaled; match it (×100). Skip if equity ≤ 0 (negative
    # book value makes the ratio meaningless — leave None, yfinance fallback decides).
    debt_to_equity = (total_debt / equity * 100) if equity and equity > 0 else None

    return {
        "revenue_ttm": rev_ttm,
        "revenue_growth_yoy": revenue_growth_yoy,
        "gross_margins": gross_margins,
        "operating_margins": operating_margins,
        "current_ratio": current_ratio,
        "debt_to_equity": debt_to_equity,
        "period_end": rev_q[-1][0] if rev_q else None,
    }


def fetch_sec_fundamentals(ticker: str) -> dict[str, Any] | None:
    """Normalized SEC fundamentals for one ticker, or None if not covered / on error.

    None is the "fall through to yfinance" signal — ADRs/ETFs (no CIK) and any network or
    parse failure all return None so the caller degrades gracefully.
    """
    cik = resolve_cik(ticker)
    if not cik:
        logger.info(f"{ticker}: no SEC CIK (ADR/ETF/unknown) — skipping SEC fundamentals")
        return None
    try:
        facts = fetch_company_facts(cik)
    except Exception as e:
        logger.warning(f"{ticker}: SEC companyfacts fetch failed: {e}")
        return None
    out = extract_fundamentals(facts)
    out["ticker"] = ticker.upper()
    out["cik"] = cik
    out["source"] = "sec_edgar"
    out["fetched_at"] = datetime.now(timezone.utc).isoformat()
    return out


# ---------------------------------------------------------------------------
# Supabase cache: read (daily hot path) + refresh (weekly)
# ---------------------------------------------------------------------------

# Columns the daily merge overlays onto yfinance market data.
_MERGE_FIELDS = ("revenue_growth_yoy", "gross_margins", "operating_margins",
                 "current_ratio", "debt_to_equity")


def _period_is_stale(period_end: str | None) -> bool:
    """True when the latest reported quarter is older than the reporting-period bound."""
    if not period_end:
        return True
    try:
        end = datetime.fromisoformat(period_end).replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    age_days = (datetime.now(timezone.utc) - end).days
    return age_days > config.market_data.fundamentals_max_period_age_days


def get_cached_fundamentals(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Read cached SEC fundamentals for `tickers`, dropping rows older than the age bound.

    Returns {TICKER: row}. Empty (never raises) when the table/cache is unavailable — the
    daily run must never break because the optional cache is missing.
    """
    if not tickers:
        return {}
    try:
        from src.db import get_client
        from src.db import rows as db_rows
        syms = sorted({t.upper() for t in tickers})
        data = db_rows(get_client().table("fundamentals").select("*").in_("ticker", syms).execute().data)
    except Exception as e:
        logger.warning(f"fundamentals cache read failed: {e}")
        return {}

    max_age = config.market_data.fundamentals_max_age_days
    now = datetime.now(timezone.utc)
    fresh: dict[str, dict[str, Any]] = {}
    for row in data:
        ts = row.get("fetched_at")
        try:
            age_days = (now - datetime.fromisoformat(str(ts))).days if ts else 10**6
        except ValueError:
            age_days = 10**6
        if age_days <= max_age:
            fresh[str(row["ticker"]).upper()] = row
    return fresh


def refresh_fundamentals(tickers: list[str]) -> dict[str, Any]:
    """Fetch SEC fundamentals for `tickers` and upsert into the Supabase cache.

    Returns a summary {refreshed, skipped, errors}. Skipped = not an SEC filer (ADR/ETF).
    """
    from src.db import get_client
    client = get_client()
    refreshed, skipped, errors = [], [], []
    payload = []
    for t in tickers:
        row = fetch_sec_fundamentals(t)
        if row is None:
            skipped.append(t.upper())
            continue
        # Only persist rows that carry at least one usable metric.
        if all(row.get(f) is None for f in _MERGE_FIELDS):
            skipped.append(t.upper())
            continue
        # Reject rows whose latest reported quarter is stale — the figures are freshly fetched
        # but describe a years-old filing (sparse-XBRL / pre-revenue names). Better to fall
        # through to yfinance than overlay stale revenue growth / a near-zero-equity d/e.
        if _period_is_stale(row.get("period_end")):
            skipped.append(t.upper())
            continue
        payload.append(row)
        refreshed.append(t.upper())

    if payload:
        try:
            client.table("fundamentals").upsert(payload, on_conflict="ticker").execute()
        except Exception as e:
            logger.error(f"fundamentals upsert failed: {e}")
            errors.append(str(e))

    summary = {"refreshed": refreshed, "skipped": skipped, "errors": errors}
    logger.info(f"fundamentals refresh: {len(refreshed)} ok, {len(skipped)} skipped, {len(errors)} err")
    return summary


def merge_into_market_data(
    market: dict[str, dict[str, Any]],
    cached: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Overlay cached SEC fundamentals onto yfinance market-data rows (SEC wins where present).

    Mutates and returns `market`. A stamped ``fundamentals_source`` field marks which tickers
    were overlaid. No-op for tickers absent from the cache — yfinance values stand.
    """
    if cached is None:
        cached = get_cached_fundamentals(list(market.keys()))
    for ticker, md in market.items():
        row = cached.get(ticker.upper())
        if not row or md.get("error"):
            continue
        overlaid = False
        for field in _MERGE_FIELDS:
            val = row.get(field)
            if val is not None:
                md[field] = val
                overlaid = True
        if overlaid:
            md["fundamentals_source"] = "sec_edgar"
    return market


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _refresh_target_tickers() -> list[str]:
    """Held ∪ buyable-conviction set — the same universe the daily run screens."""
    from src.brain.conviction import buyable_tickers
    from src.data_layer import get_portfolio, load_convictions
    held = set(get_portfolio().keys())
    convictions = load_convictions()
    return sorted(held | set(buyable_tickers(convictions)))


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = sys.argv[1:]
    if "--refresh" in args:
        names = [a for a in args if not a.startswith("--")]
        targets = [n.upper() for n in names] if names else _refresh_target_tickers()
        print(f"Refreshing SEC fundamentals for {len(targets)} tickers: {', '.join(targets)}")
        result = refresh_fundamentals(targets)
        print(f"  refreshed: {result['refreshed']}")
        print(f"  skipped (ADR/ETF/no-data): {result['skipped']}")
        if result["errors"]:
            print(f"  errors: {result['errors']}")
    else:
        print(__doc__)
