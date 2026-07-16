"""
Robinhood live portfolio sync — runs before strategy screening each day.

Flow:
1. Load tokens: Supabase robinhood_tokens (freshest) → tokens.enc (fallback)
2. Refresh access token using refresh_token + device_token
3. Persist updated tokens back to Supabase (handles Robinhood's refresh token rotation)
4. Fetch equity positions from Robinhood REST API
5. Merge num_buys from trades table, write snapshot to Supabase
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

from src import config

_RH_POSITIONS_URL = "https://api.robinhood.com/positions/"
_RH_ACCOUNTS_URL  = "https://api.robinhood.com/accounts/"
_RH_ORDERS_URL    = "https://api.robinhood.com/orders/"
_RH_TOKEN_URL     = "https://api.robinhood.com/oauth2/token/"
_RH_CLIENT_ID     = "c82SH0WZOsabOXGP2sxqcj34FxkvfnWRZBKlBjFS"

# Real-order ingestion bounds: how far back to look, and a page cap so a gap never
# unrolls the whole history. Daily runs mean the first page usually suffices; the
# window covers weekends / a missed run. TSLA is excluded (Tesla blackout, hard rule #1).
_ORDER_LOOKBACK_DAYS = 14
_ORDER_MAX_PAGES     = 5


# ---------------------------------------------------------------------------
# Encryption helpers (AES-256-GCM, same key as tokens.enc — see src/crypto.py)
# ---------------------------------------------------------------------------

def _encrypt(data: dict) -> str:
    """Encrypt a token dict → JSON string suitable for storing in Supabase."""
    from src.crypto import encrypt_json
    return encrypt_json(data, config.robinhood.token_key)


def _decrypt(blob: str) -> dict:
    """Decrypt an encrypted blob string → token dict."""
    from src.crypto import decrypt_json
    return decrypt_json(blob, config.robinhood.token_key)


# ---------------------------------------------------------------------------
# Token loading (Supabase → tokens.enc fallback)
# ---------------------------------------------------------------------------

def _load_tokens_from_supabase() -> dict | None:
    """Try to load the latest tokens from Supabase robinhood_tokens table."""
    try:
        from src.db import get_client
        row = get_client().table("robinhood_tokens").select("encrypted_blob").eq("id", 1).single().execute()
        data: dict = row.data  # type: ignore[assignment]
        return _decrypt(data["encrypted_blob"])
    except Exception:
        return None


def _load_tokens_from_file() -> dict:
    """Decrypt tokens.enc → token dict (fallback when Supabase has nothing)."""
    if not config.robinhood.token_key:
        raise RuntimeError("ROBINHOOD_TOKEN_KEY not set")
    tokens_path = Path(config.robinhood.tokens_file)
    if not tokens_path.exists():
        raise RuntimeError(f"Tokens file not found: {tokens_path}")
    return _decrypt(tokens_path.read_text())


# Keep the old name so existing tests still import it
_decrypt_tokens = _load_tokens_from_file


def _save_tokens_to_supabase(tokens: dict) -> None:
    """Re-encrypt updated tokens and upsert into Supabase."""
    from src.db import get_client
    get_client().table("robinhood_tokens").upsert({
        "id":             1,
        "encrypted_blob": _encrypt(tokens),
        "updated_at":     "now()",
    }).execute()


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

def _refresh_tokens(tokens: dict) -> dict:
    """
    Exchange refresh_token for a fresh access_token (+ possibly new refresh_token).
    Raises on failure — caller falls back to stored access_token.
    """
    resp = requests.post(
        _RH_TOKEN_URL,
        data={
            "grant_type":    "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id":     _RH_CLIENT_ID,
            "device_token":  tokens.get("device_token", ""),
            "expires_in":    "86400",
            "scope":         "internal",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    resp.raise_for_status()
    refreshed = resp.json()
    # Merge into existing tokens so device_token and other fields are preserved
    return {**tokens, **refreshed}


# ---------------------------------------------------------------------------
# Robinhood REST API fetch
# ---------------------------------------------------------------------------

def _fetch_positions(bearer_token: str) -> list[dict]:
    """Fetch all non-zero equity positions, handling pagination."""
    positions: list[dict] = []
    url: str | None = _RH_POSITIONS_URL
    headers = {"Authorization": f"Bearer {bearer_token}", "Accept": "application/json"}
    params  = {"nonzero": "true"}

    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        positions.extend(data.get("results", []))
        url    = data.get("next")
        params = {}

    return positions


# ---------------------------------------------------------------------------
# Portfolio mapping
# ---------------------------------------------------------------------------

def _fetch_buying_power(bearer_token: str) -> float | None:
    """Deployable cash in the Individual account (the paper track's real sizing book). Reads
    the accounts endpoint; prefers `buying_power`, falling back to portfolio/settled cash.
    Returns None on any failure so sizing falls back to the configured book."""
    headers = {"Authorization": f"Bearer {bearer_token}", "Accept": "application/json"}
    try:
        resp = requests.get(_RH_ACCOUNTS_URL, headers=headers, timeout=15)
        resp.raise_for_status()
        results = resp.json().get("results") or []
        if not results:
            return None
        acct = results[0]
        for key in ("buying_power", "portfolio_cash", "cash"):
            val = acct.get(key)
            if val not in (None, ""):
                return float(val)
    except (requests.RequestException, ValueError, TypeError):
        logger.warning("Could not fetch Robinhood buying power — paper sizing will use the fallback book.", exc_info=True)
    return None


def _resolve_symbol(instrument_url: str, bearer: str, cache: dict[str, str]) -> str | None:
    """Map a Robinhood instrument URL → ticker symbol, memoized across an order batch."""
    if not instrument_url:
        return None
    if instrument_url in cache:
        return cache[instrument_url]
    headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json"}
    try:
        resp = requests.get(instrument_url, headers=headers, timeout=15)
        resp.raise_for_status()
        symbol = (resp.json().get("symbol") or "").upper() or None
    except (requests.RequestException, ValueError, TypeError):
        symbol = None
    cache[instrument_url] = symbol or ""
    return symbol


def _fetch_recent_orders(bearer: str) -> list[dict]:
    """Fetch Abhi's real equity orders (main Individual account) over the recent window and
    map filled ones into user_orders rows — exact fill price/time/partials as ground truth.

    Paginates until orders fall outside the lookback window (or the page cap), resolves each
    instrument to a symbol, and drops TSLA (Tesla blackout, hard rule #1). Returns row dicts
    keyed for idempotent upsert on order_id."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=_ORDER_LOOKBACK_DAYS)
    headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json"}
    symbol_cache: dict[str, str] = {}
    rows: list[dict] = []
    url: str | None = _RH_ORDERS_URL
    pages = 0

    while url and pages < _ORDER_MAX_PAGES:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        page_orders = data.get("results", [])
        pages += 1

        reached_cutoff = False
        for o in page_orders:
            created = o.get("created_at")
            if created:
                try:
                    if datetime.fromisoformat(created.replace("Z", "+00:00")) < cutoff:
                        reached_cutoff = True
                        continue
                except (ValueError, TypeError):
                    pass
            if o.get("state") not in ("filled", "partially_filled"):
                continue
            if float(o.get("cumulative_quantity") or 0) <= 0:
                continue
            symbol = _resolve_symbol(o.get("instrument", ""), bearer, symbol_cache)
            if not symbol or symbol == "TSLA":
                continue
            rows.append({
                "order_id":      o.get("id"),
                "ticker":        symbol,
                "side":          o.get("side"),
                "order_type":    o.get("type"),
                "state":         o.get("state"),
                "quantity":      float(o.get("cumulative_quantity") or 0),
                "average_price": float(o["average_price"]) if o.get("average_price") else None,
                "created_at_rh": o.get("created_at"),
                "filled_at":     o.get("last_transaction_at"),
                "executions":    o.get("executions") or [],
                "realized_pnl":  None,   # derivable downstream from the buy/sell fill stream
                "raw":           o,
            })

        # Orders come newest-first; once a page crosses the window we can stop paginating.
        if reached_cutoff:
            break
        url = data.get("next")

    return rows


def _positions_to_portfolio(positions: list[dict]) -> dict[str, dict]:
    portfolio: dict[str, dict] = {}
    for pos in positions:
        ticker = pos.get("symbol", "").upper()
        qty    = float(pos.get("quantity") or 0)
        avg    = float(pos.get("average_buy_price") or 0)
        if ticker and qty > 0:
            portfolio[ticker] = {"shares": qty, "avg_cost": avg, "num_buys": 1}
    return portfolio


def _enrich_num_buys(portfolio: dict[str, dict]) -> None:
    """Merge num_buys from the trades table (in-place)."""
    try:
        from src.data_layer import get_cost_basis_from_db
        db_data = get_cost_basis_from_db()
        for ticker, pos in portfolio.items():
            pos["num_buys"] = db_data.get(ticker, {}).get("num_buys", 1)
    except Exception as exc:
        logger.warning(f"Could not merge num_buys from DB (defaulting to 1): {exc}")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def sync_portfolio_to_supabase() -> dict | None:
    """
    Fetch live Robinhood positions and write a fresh portfolio snapshot to Supabase.
    Returns the portfolio dict on success, None if credentials are missing or sync fails.
    """
    if not config.robinhood.token_key or not config.robinhood.tokens_file:
        logger.warning("Robinhood credentials not configured — skipping live sync.")
        return None
    if not config.robinhood.account_number:
        logger.warning("ROBINHOOD_ACCOUNT_NUMBER not set — skipping live sync.")
        return None

    tokens = _load_tokens_from_supabase()
    if tokens:
        logger.info("Loaded Robinhood tokens from Supabase.")
    else:
        try:
            tokens = _load_tokens_from_file()
            logger.info("Loaded Robinhood tokens from tokens.enc.")
        except Exception:
            logger.error("Could not load tokens — skipping live sync.", exc_info=True)
            return None

    try:
        try:
            tokens = _refresh_tokens(tokens)
            _save_tokens_to_supabase(tokens)
            logger.info("Token refreshed and saved to Supabase.")
        except Exception as exc:
            logger.warning(f"Token refresh failed — using stored access token: {exc}")

        bearer    = tokens["access_token"]
        positions = _fetch_positions(bearer)
        portfolio = _positions_to_portfolio(positions)
        _enrich_num_buys(portfolio)
        buying_power = _fetch_buying_power(bearer)

        from src.data_layer import save_mcp_portfolio_snapshot
        save_mcp_portfolio_snapshot(portfolio, buying_power)
        logger.info(f"Live portfolio synced: {len(portfolio)} positions.")

        # Ingest real order fills (ground-truth human-behavior data). Non-fatal — a failure
        # here must not break the portfolio sync or the run.
        try:
            from src import memory
            memory.upsert_user_orders(_fetch_recent_orders(bearer))
        except Exception:
            logger.error("Order ingestion failed — pipeline continues", exc_info=True)

        from src.exploration import check_graduations
        check_graduations(set(portfolio.keys()))

        return portfolio

    except Exception:
        logger.error("Robinhood sync failed — pipeline will use cached data.", exc_info=True)
        return None
