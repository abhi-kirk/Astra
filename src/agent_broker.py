"""
Autotrader execution adapter — the ONLY module that talks to Robinhood for execution.

Deterministic Python MCP client to Robinhood's official Agentic Trading MCP
(`https://agent.robinhood.com/mcp/trading`). No LLM is involved: the executor calls
these thin, synchronous wrappers directly after guardrails pass, so every order is
code-driven. Everything else mocks `AgenticBroker` in tests — nothing else imports the
MCP transport.

Auth: OAuth 2.0 bearer token, obtained once via Robinhood's desktop onboarding (the
agent authorizes a dedicated `agentic_allowed=true` sub-account) and stored encrypted
(same AES-256-GCM scheme as src/robinhood.py). The token is loaded at call time.

    HEADLESS-OAUTH — PENDING VERIFICATION (docs/autonomy.md checklist #3):
    Robinhood has not published the agentic token refresh flow. `_load_access_token`
    supports a stored access token today; `_refresh_access_token` is a marked stub to be
    wired once the refresh endpoint is confirmed during onboarding. Until then, run the
    read-only smoke test (`python -m src.agent_broker --check`) after a fresh bootstrap
    to confirm connectivity, and treat token expiry as an operational item.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from typing import Any

from src import config
from src.crypto import decrypt_json, encrypt_json

logger = logging.getLogger(__name__)


class BrokerError(RuntimeError):
    """Raised when the Agentic MCP returns an error or cannot be reached."""


# ---------------------------------------------------------------------------
# OAuth token store (AES-256-GCM encrypted; SDK-driven refresh)
# ---------------------------------------------------------------------------
# Robinhood's agentic MCP advertises standard OAuth 2.0 metadata:
#   registration_endpoint  → dynamic client registration (DCR) supported
#   grant_types            → authorization_code + refresh_token  (headless renewal)
#   PKCE S256, public client (token_endpoint_auth_method=none), scope "internal"
# The MCP SDK's OAuthClientProvider drives that whole flow; we only supply persistence.

# Explicit 127.0.0.1 (not "localhost") per RFC 8252 §7.3 — avoids the macOS
# localhost→IPv6(::1) vs IPv4(127.0.0.1) ambiguity that can misroute the redirect.
REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/callback"


class EncryptedFileTokenStorage:
    """MCP `TokenStorage` backed by an AES-GCM encrypted file (agent_tokens.enc).

    Persists both the OAuth tokens (access + refresh) and the dynamically-registered
    client info, so headless runs reuse the registration and silently refresh.
    """

    def __init__(self, path: str = config.agent.rh_tokens_file, key_b64: str = config.agent.rh_token_key):
        self._path = Path(path)
        self._key = key_b64
        self._cache: dict = self._read()

    def _read(self) -> dict:
        if self._key and self._path.exists():
            try:
                return decrypt_json(self._path.read_text(), self._key)
            except Exception:
                logger.error("Could not decrypt %s — starting empty", self._path, exc_info=True)
        return {}

    def _write(self) -> None:
        if not self._key:
            raise BrokerError("AGENT_RH_TOKEN_KEY not set — cannot persist OAuth tokens.")
        self._path.write_text(encrypt_json(self._cache, self._key))

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        t = self._cache.get("tokens")
        return OAuthToken(**t) if t else None

    async def set_tokens(self, tokens) -> None:
        self._cache["tokens"] = tokens.model_dump(exclude_none=True, mode="json")
        self._write()

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        c = self._cache.get("client_info")
        return OAuthClientInformationFull(**c) if c else None

    async def set_client_info(self, client_info) -> None:
        self._cache["client_info"] = client_info.model_dump(exclude_none=True, mode="json")
        self._write()


# robinhood_tokens.id — the individual/advisor account persists as row 1 (src/robinhood.py);
# the agentic account is row 2. Reuses the existing, already-GRANTed table (no new schema).
_AGENT_TOKENS_ROW_ID = 2


class SupabaseTokenStorage(EncryptedFileTokenStorage):
    """Token store that persists to Supabase (durable across headless runs), with the encrypted
    file as a bootstrap-seed fallback.

    Robinhood rotates the refresh token on *every* refresh (access tokens live 24h), so the
    rotated token MUST survive to the next run. A GitHub-secret/CI-file-only store can't — the
    file is discarded when the job ends, so every run replays the original bootstrap token until
    Robinhood expires it (~days) and the Autotrader silently dies. Persisting each refresh to
    Supabase keeps the chain alive indefinitely with no re-auth. Mirrors the individual-account
    persistence in src/robinhood.py.
    """

    def _read(self) -> dict:
        blob = self._read_supabase()
        if blob is not None:
            return blob
        return super()._read()  # first run: seed from the decoded file / GitHub secret

    def _write(self) -> None:
        super()._write()          # keep the in-run file coherent
        self._write_supabase()    # ...and persist durably so the next run sees the rotation

    def _read_supabase(self) -> dict | None:
        if not self._key:
            return None
        try:
            from src.db import get_client
            row = (get_client().table("robinhood_tokens").select("encrypted_blob")
                   .eq("id", _AGENT_TOKENS_ROW_ID).single().execute())
            data: dict = row.data  # type: ignore[assignment]
            if data and data.get("encrypted_blob"):
                return decrypt_json(data["encrypted_blob"], self._key)
        except Exception:
            logger.warning("Agentic token load from Supabase failed — falling back to file.", exc_info=True)
        return None

    def _write_supabase(self) -> None:
        try:
            from src.db import get_client
            get_client().table("robinhood_tokens").upsert({
                "id": _AGENT_TOKENS_ROW_ID,
                "encrypted_blob": encrypt_json(self._cache, self._key),
            }).execute()
        except Exception:
            logger.error("Agentic token persist to Supabase failed — next run may need re-auth.", exc_info=True)


def _oauth_provider(storage=None, redirect_handler=None, callback_handler=None):
    """Build the SDK OAuth provider. With only `storage`, it refreshes headlessly using
    the stored refresh token; the handlers are supplied only for the interactive bootstrap."""
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata
    from pydantic import AnyUrl
    metadata = OAuthClientMetadata(
        client_name="ASTRA Autotrader",
        redirect_uris=[AnyUrl(REDIRECT_URI)],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="internal",
        token_endpoint_auth_method="none",
    )
    return OAuthClientProvider(
        server_url=config.agent.rh_mcp_url,
        client_metadata=metadata,
        storage=storage or SupabaseTokenStorage(),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------

def _unwrap(payload: Any) -> Any:
    """RH's agentic MCP wraps every tool result as {"data": <payload>, "guide": <hint>}.
    Return the inner payload; the guide is for LLMs, not us."""
    if isinstance(payload, dict) and "data" in payload and "guide" in payload:
        return payload["data"]
    return payload


def _parse_result(result: Any) -> Any:
    """Turn a CallToolResult into plain data. Prefers structuredContent; falls back to
    JSON in the first text block. Unwraps RH's {data, guide} envelope. Raises on errors."""
    if getattr(result, "isError", False):
        raise BrokerError(_result_text(result) or "Agentic MCP returned an error")
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return _unwrap(structured)
    text = _result_text(result)
    if not text:
        return None
    try:
        return _unwrap(json.loads(text))
    except (ValueError, TypeError):
        return text


def _result_text(result: Any) -> str:
    parts = []
    for block in getattr(result, "content", None) or []:
        t = getattr(block, "text", None)
        if t:
            parts.append(t)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Broker
# ---------------------------------------------------------------------------

class AgenticBroker:
    """Synchronous facade over the async Agentic Trading MCP. One connection per call —
    the run is low-frequency (once daily, a handful of orders) so this stays simple and
    robust rather than holding a long-lived session."""

    def __init__(self, url: str = config.agent.rh_mcp_url, account_number: str = config.agent.account_number):
        self.url = url
        self._account_number = account_number

    # -- transport -------------------------------------------------------
    async def _acall(self, name: str, arguments: dict) -> Any:
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        # Auth = the SDK OAuth provider, refreshing headlessly from the encrypted token store.
        async with streamablehttp_client(self.url, auth=_oauth_provider()) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool(name=name, arguments=arguments)
                return _parse_result(result)

    def call(self, name: str, arguments: dict) -> Any:
        """Invoke one MCP tool synchronously. Overridden/mocked in tests."""
        try:
            return asyncio.run(self._acall(name, arguments))
        except BrokerError:
            raise
        except Exception as exc:  # transport / connection failures
            raise BrokerError(f"Agentic MCP call '{name}' failed: {exc}") from exc

    # -- reads -----------------------------------------------------------
    def get_accounts(self) -> Any:
        return self.call("get_accounts", {})

    def account_number(self) -> str:
        """Configured agentic account, or the first agentic_allowed account discovered."""
        if self._account_number:
            return self._account_number
        accounts = self.get_accounts() or {}
        rows = accounts.get("accounts") if isinstance(accounts, dict) else accounts
        for acct in rows or []:
            if acct.get("agentic_allowed"):
                num = acct.get("account_number") or acct.get("rhs_account_number")
                if num:
                    self._account_number = num
                    return num
        raise BrokerError("No agentic_allowed account found — check the onboarding sub-account.")

    def get_portfolio(self) -> Any:
        return self.call("get_portfolio", {"account_number": self.account_number()})

    def get_positions(self) -> Any:
        return self.call("get_equity_positions", {"account_number": self.account_number()})

    def get_orders(self, **filters) -> Any:
        return self.call("get_equity_orders", {"account_number": self.account_number(), **filters})

    def get_realized_pnl(self) -> Any:
        """All-time realized P&L for the agentic account (equities). Feeds the P&L-based
        drawdown halt. asset_classes is required by the endpoint; the sleeve is equities-only."""
        return self.call("get_realized_pnl", {
            "account_number": self.account_number(), "span": "all", "asset_classes": ["equity"],
        })

    # -- writes (execution) ---------------------------------------------
    def review_order(self, **params) -> Any:
        return self.call("review_equity_order", {"account_number": self.account_number(), **params})

    def place_order(self, **params) -> Any:
        return self.call("place_equity_order", {"account_number": self.account_number(), **params})

    def cancel_order(self, order_id: str) -> Any:
        return self.call("cancel_equity_order",
                         {"account_number": self.account_number(), "order_id": order_id})


# ---------------------------------------------------------------------------
# Response extractors (pure, shape-tolerant)
# ---------------------------------------------------------------------------

def extract_portfolio(portfolio_result: Any) -> dict[str, float]:
    """Pull equity / buying power / cash out of a get_portfolio result (shape-tolerant)."""
    p = portfolio_result if isinstance(portfolio_result, dict) else {}
    if "portfolio" in p and isinstance(p["portfolio"], dict):
        p = p["portfolio"]

    def _num(*keys) -> float | None:
        for k in keys:
            v = p.get(k)
            if v is not None:
                try:
                    return float(v)
                except (ValueError, TypeError):
                    pass
        return None

    equity = _num("total_equity", "equity", "total_value", "total_market_value", "market_value")
    # buying_power may be a nested object: {"buying_power": "1000.00", "unleveraged_buying_power": ...}
    bp_raw = p.get("buying_power")
    if isinstance(bp_raw, dict):
        try:
            buying_power = float(bp_raw.get("buying_power") or bp_raw.get("unleveraged_buying_power") or 0)
        except (ValueError, TypeError):
            buying_power = None
    else:
        buying_power = _num("buying_power", "cash_available_for_trading")
    cash = _num("cash", "settled_cash", "uncleared_deposits")
    total_equity = equity if equity is not None else 0.0
    cash_val = cash if cash is not None else (buying_power or 0.0)
    # Market value of open stock only (excludes cash) — the unrealized-P&L input. RH exposes it
    # directly as `equity_value`; fall back to total_equity − cash for an equities-only account.
    equity_value = _num("equity_value", "market_value")
    if equity_value is None:
        equity_value = max(total_equity - cash_val, 0.0)
    return {
        "total_equity": total_equity,
        "equity_value": equity_value,
        "buying_power": buying_power if buying_power is not None else 0.0,
        "cash": cash_val,
    }


def extract_order(place_result: Any) -> tuple[str | None, str]:
    """Pull (order_id, state) from a place_equity_order result, tolerant of nesting.
    The order object carries a top-level `id` + `state` (e.g. 'filled'), but the place
    envelope may nest it under `order`, `orders[0]`, or `data`. Falls back to
    (None, 'submitted') so a successful place is never mis-logged as un-placed."""
    if not isinstance(place_result, dict):
        return None, "submitted"
    order = place_result
    if isinstance(place_result.get("orders"), list) and place_result["orders"]:
        order = place_result["orders"][0]
    else:
        for key in ("order", "data", "result"):
            if isinstance(place_result.get(key), dict):
                order = place_result[key]
                break
    order_id = order.get("id") or order.get("order_id") if isinstance(order, dict) else None
    state = (order.get("state") or order.get("status") or "submitted") if isinstance(order, dict) else "submitted"
    return order_id, state


def extract_positions(positions_result: Any) -> dict[str, dict]:
    """Turn a get_equity_positions result into {ticker: {shares, avg_cost}} (tolerant)."""
    rows: Any = positions_result
    if isinstance(rows, dict):
        rows = rows.get("positions") or rows.get("results") or []
    out: dict[str, dict] = {}
    for pos in rows or []:
        if not isinstance(pos, dict):
            continue
        sym = (pos.get("symbol") or pos.get("ticker") or "").upper()
        try:
            qty = float(pos.get("quantity") or 0)
            avg = float(pos.get("average_buy_price") or pos.get("average_cost") or 0)
            # Guide: sell only shares_available_for_sells (settled), not raw quantity — avoids GFV.
            sellable = float(pos.get("shares_available_for_sells") or qty)
        except (ValueError, TypeError):
            continue
        if sym and qty > 0:
            out[sym] = {"shares": qty, "sellable": sellable, "avg_cost": avg, "num_buys": 1}
    return out


def extract_realized_pnl(pnl_result: Any) -> float:
    """All-time realized P&L ($) from a get_realized_pnl result. The window total lives at
    `total_returns` (a string). Shape-tolerant; a window with no closing trades returns 0.0."""
    p = pnl_result if isinstance(pnl_result, dict) else {}
    if "data" in p and isinstance(p["data"], dict):
        p = p["data"]
    for k in ("total_returns", "total_realized_gain", "realized_gain"):
        v = p.get(k)
        if v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
    return 0.0


# ---------------------------------------------------------------------------
# One-time interactive OAuth bootstrap (docs/autonomy.md checklist #1)
# ---------------------------------------------------------------------------

def _wait_for_redirect(port: int, timeout: float = 300.0) -> tuple[str | None, str | None]:
    """Capture (code, state) from the OAuth loopback redirect.

    Threaded + resilient: browsers fire preconnect/favicon requests to the loopback
    before (and after) the real navigation, so we keep serving and only finish on the
    request that actually carries `code` — those spurious hits get a 204."""
    import http.server
    import socketserver
    import threading
    import urllib.parse

    holder: dict = {}
    done = threading.Event()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in params:
                holder["code"] = params["code"][0]
                holder["state"] = params.get("state", [None])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h2>ASTRA Autotrader authorized. You can close this tab.</h2>")
                done.set()
            else:
                self.send_response(204)
                self.end_headers()

        def log_message(self, *a):  # silence
            pass

    class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    try:
        srv = _Server((REDIRECT_HOST, port), _Handler)
    except OSError as exc:
        raise BrokerError(
            f"Could not bind the OAuth callback on {REDIRECT_HOST}:{port} ({exc}). "
            f"A stale run may hold it — `lsof -nP -i :{port}` then kill the PID, and re-run --bootstrap."
        ) from exc

    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    got = done.wait(timeout)
    srv.shutdown()
    srv.server_close()
    if not got:
        return None, None
    return holder.get("code"), holder.get("state")


async def _abootstrap(storage: EncryptedFileTokenStorage) -> None:
    import asyncio as _asyncio
    import webbrowser

    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def redirect_handler(url: str) -> None:
        print("\n→ Opening your browser to authorize ASTRA Autotrader with Robinhood.")
        print("  Select the **Agentic** account when prompted.\n  If the browser did not open, paste this URL:\n")
        print(f"  {url}\n")
        webbrowser.open(url)

    async def callback_handler() -> tuple[str, str | None]:
        code, state = await _asyncio.get_event_loop().run_in_executor(None, _wait_for_redirect, REDIRECT_PORT)
        if not code:
            raise BrokerError("No authorization code received on the loopback callback.")
        return code, state

    provider = _oauth_provider(storage=storage, redirect_handler=redirect_handler, callback_handler=callback_handler)
    async with streamablehttp_client(config.agent.rh_mcp_url, auth=provider) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()  # triggers the OAuth dance, then persists tokens


def _bootstrap() -> int:
    """Interactive: register + authorize + persist the agentic OAuth tokens, then print
    the values to store as GitHub secrets. Run on a machine with a browser (Abhi's laptop)."""
    from src.logger import setup as _setup
    _setup()

    key = config.agent.rh_token_key or os.environ.get("AGENT_RH_TOKEN_KEY")
    generated = False
    if not key:
        key = base64.b64encode(os.urandom(32)).decode()
        generated = True
        # Print the key BEFORE the interactive flow so a mid-flow failure never loses it.
        print("\n" + "=" * 68)
        print("🔑 SAVE THIS NOW (the encryption key for your agentic tokens):")
        print(f"AGENT_RH_TOKEN_KEY = {key}")
        print("=" * 68 + "\n")

    # Start clean: a stale file from a prior failed run is encrypted with a lost key.
    stale = Path(config.agent.rh_tokens_file)
    if stale.exists():
        stale.unlink()

    # Bootstrap writes to both the local file and Supabase, so the first cloud run reads the
    # durable copy and every subsequent refresh persists there too.
    storage = SupabaseTokenStorage(key_b64=key)
    asyncio.run(_abootstrap(storage))

    enc_b64 = base64.b64encode(Path(config.agent.rh_tokens_file).read_bytes()).decode()
    print("\n" + "=" * 68)
    print("✅ Authorized. Agentic OAuth tokens saved to", config.agent.rh_tokens_file)
    print("Store these as GitHub repo secrets for the headless Autotrader workflow:")
    print("=" * 68)
    if generated:
        print(f"AGENT_RH_TOKEN_KEY   = {key}")
    else:
        print("AGENT_RH_TOKEN_KEY   = (unchanged — the value already in your env/secrets)")
    print(f"AGENT_RH_TOKENS_ENC  = {enc_b64}")
    print("AGENT_ACCOUNT_NUMBER = 742995442   # the 'Agentic' cash account")
    print("=" * 68)
    print("The workflow decodes AGENT_RH_TOKENS_ENC → agent_tokens.enc and refreshes it")
    print("automatically. Keep the key secret; anyone with both can trade the account.")
    return 0


# ---------------------------------------------------------------------------
# Read-only smoke test (no orders) — docs/autonomy.md checklist #3
# ---------------------------------------------------------------------------

def _smoke() -> int:
    from src.logger import setup as _setup
    _setup()
    broker = AgenticBroker()
    logger.info("Agentic MCP read-only smoke test — %s", broker.url)
    logger.info("accounts: %s", json.dumps(broker.get_accounts(), default=str)[:800])
    logger.info("account_number: %s", broker.account_number())
    logger.info("portfolio: %s", json.dumps(broker.get_portfolio(), default=str)[:800])
    logger.info("positions: %s", json.dumps(broker.get_positions(), default=str)[:800])
    logger.info("Smoke test complete — no orders placed.")
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Robinhood Agentic MCP broker")
    parser.add_argument("--bootstrap", action="store_true",
                        help="one-time interactive OAuth (opens a browser; run on your laptop)")
    parser.add_argument("--check", action="store_true",
                        help="read-only connectivity smoke test (no orders)")
    args = parser.parse_args()
    if args.bootstrap:
        raise SystemExit(_bootstrap())
    if args.check:
        raise SystemExit(_smoke())
    parser.print_help()
