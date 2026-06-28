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

import base64
import json
import logging
import os
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

from src.config import (
    ROBINHOOD_ACCOUNT_NUMBER,
    ROBINHOOD_TOKEN_KEY,
    ROBINHOOD_TOKENS_FILE,
)

_RH_POSITIONS_URL = "https://api.robinhood.com/positions/"
_RH_TOKEN_URL     = "https://api.robinhood.com/oauth2/token/"
_RH_CLIENT_ID     = "c82SH0WZOsabOXGP2sxqcj34FxkvfnWRZBKlBjFS"


# ---------------------------------------------------------------------------
# Encryption helpers (AES-256-GCM, same key as tokens.enc)
# ---------------------------------------------------------------------------

def _encrypt(data: dict) -> str:
    """Encrypt a token dict → JSON string suitable for storing in Supabase."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = base64.b64decode(ROBINHOOD_TOKEN_KEY)
    iv  = os.urandom(12)
    aesgcm = AESGCM(key)
    ct_with_tag = aesgcm.encrypt(iv, json.dumps(data).encode(), None)
    ct  = ct_with_tag[:-16]
    tag = ct_with_tag[-16:]
    return json.dumps({
        "iv":         base64.b64encode(iv).decode(),
        "tag":        base64.b64encode(tag).decode(),
        "ciphertext": base64.b64encode(ct).decode(),
    })


def _decrypt(blob: str) -> dict:
    """Decrypt an encrypted blob string → token dict."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    enc = json.loads(blob)
    key = base64.b64decode(ROBINHOOD_TOKEN_KEY)
    iv  = base64.b64decode(enc["iv"])
    tag = base64.b64decode(enc["tag"])
    ct  = base64.b64decode(enc["ciphertext"])
    return json.loads(AESGCM(key).decrypt(iv, ct + tag, None))


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
    if not ROBINHOOD_TOKEN_KEY:
        raise RuntimeError("ROBINHOOD_TOKEN_KEY not set")
    tokens_path = Path(ROBINHOOD_TOKENS_FILE)
    if not tokens_path.exists():
        raise RuntimeError(f"Tokens file not found: {tokens_path}")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    enc = json.loads(tokens_path.read_text())
    key = base64.b64decode(ROBINHOOD_TOKEN_KEY)
    iv  = base64.b64decode(enc["iv"])
    tag = base64.b64decode(enc["tag"])
    ct  = base64.b64decode(enc["ciphertext"])
    return json.loads(AESGCM(key).decrypt(iv, ct + tag, None))


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
    if not ROBINHOOD_TOKEN_KEY or not ROBINHOOD_TOKENS_FILE:
        logger.warning("Robinhood credentials not configured — skipping live sync.")
        return None
    if not ROBINHOOD_ACCOUNT_NUMBER:
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

        from src.data_layer import save_mcp_portfolio_snapshot
        save_mcp_portfolio_snapshot(portfolio)
        logger.info(f"Live portfolio synced: {len(portfolio)} positions.")

        from src.exploration import check_graduations
        check_graduations(set(portfolio.keys()))

        return portfolio

    except Exception:
        logger.error("Robinhood sync failed — pipeline will use cached data.", exc_info=True)
        return None
