"""
MCP server registry for ASTRA's Claude tool loop.

Each integration is a function returning a `ServerSpec` (or None if unconfigured).
ASTRA runs Claude's tool-use loop **client-side** (see src/mcp_loop.py): we open the
MCP connection ourselves, list its tools, expose them to Claude as ordinary tool
schemas, and dispatch each call with our own per-tool timeout + circuit breaker. This
replaced the server-side `mcp_servers=` connector, which ran the loop opaquely on
Anthropic's infra with no per-tool timeout — one slow server stalled the whole call.

Usage:
    specs = mcp.advisor_specs()
    text, usage, tool_log = mcp_loop.run_agentic_sync(prompt, specs, ...)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from anthropic.types import TextBlock

from src.config import (
    ALPHA_VANTAGE_API_KEY,
    ALPHA_VANTAGE_MAX_CALLS,
    FMP_API_KEY,
    FMP_MAX_CALLS,
    SEC_EDGAR_MCP_URL,
    TAVILY_MAX_SEARCHES,
    TAVILY_MCP_URL,
)


@dataclass(frozen=True)
class ServerSpec:
    """A remote HTTP MCP endpoint we connect to client-side."""
    name: str
    url: str
    allowed_tools: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Server definitions — one function per integration
# ---------------------------------------------------------------------------

def _tavily() -> ServerSpec | None:
    if not TAVILY_MCP_URL:
        return None
    return ServerSpec(name="tavily", url=TAVILY_MCP_URL, allowed_tools=["tavily_search"])


def _alpha_vantage() -> ServerSpec | None:
    if not ALPHA_VANTAGE_API_KEY:
        return None
    # Free tier: 25 calls/day, 5 calls/min — the prompt instructs Claude to stay within limits.
    # Auth: API key passed as URL query param (legacy method; OAuth requires interactive flow).
    url = f"https://mcp.alphavantage.co/mcp?apikey={ALPHA_VANTAGE_API_KEY}"
    return ServerSpec(
        name="alpha_vantage", url=url,
        allowed_tools=["NEWS_SENTIMENT", "EARNINGS_CALENDAR", "COMPANY_OVERVIEW"],
    )


def _fmp() -> ServerSpec | None:
    # Dormant — kept for a future follow-up once the client-side path is proven (see CLAUDE.md).
    if not FMP_API_KEY:
        return None
    url = f"https://financialmodelingprep.com/mcp?apikey={FMP_API_KEY}"
    return ServerSpec(name="fmp", url=url, allowed_tools=["analyst", "calendar"])


def _sec_edgar() -> ServerSpec | None:
    # Dormant — kept for a future follow-up once the client-side path is proven (see CLAUDE.md).
    if not SEC_EDGAR_MCP_URL:
        return None
    return ServerSpec(
        name="sec_edgar", url=SEC_EDGAR_MCP_URL,
        allowed_tools=["secedgar_get_insider_transactions"],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def advisor_specs() -> list[ServerSpec]:
    """MCP servers exposed to the daily advisor loop — reliable providers only (Tavily + AV).

    SEC EDGAR (community-hosted, no SLA) and FMP (intermittent, empty for small caps) are
    intentionally excluded for now; their definitions are kept dormant above for a later
    follow-up once the client-side per-tool timeout has proven stable.
    """
    return [s for s in (_tavily(), _alpha_vantage()) if s is not None]


def exploration_specs() -> list[ServerSpec]:
    """MCP servers exposed to the weekly exploration loop (same reliable set)."""
    return [s for s in (_tavily(), _alpha_vantage()) if s is not None]


def extract_text(message: Any) -> str:
    """Join all text blocks of a standard Anthropic Messages response.

    The client-side loop's final turn is clean markdown (no inlined tool XML), so this is
    a straight concatenation of the ``text`` blocks — no beta-connector stripping needed.
    """
    parts = [b.text for b in getattr(message, "content", []) if isinstance(b, TextBlock)]
    return "\n".join(p for p in parts if p).strip()


def search_context(
    max_searches: int = TAVILY_MAX_SEARCHES,
    servers: list[ServerSpec] | None = None,
) -> dict[str, Any]:
    """Template vars for the mustache prompt tool-use block.

    Pass `servers` to match exactly what the loop will connect to — the template must only
    advertise tools that are actually available. Defaults to `advisor_specs()`.
    """
    if servers is None:
        servers = advisor_specs()
    names = {s.name for s in servers}
    return {
        "has_search":        bool(servers),
        "has_tavily":        "tavily" in names,
        "has_alpha_vantage": "alpha_vantage" in names,
        "has_sec_edgar":     "sec_edgar" in names,
        "has_fmp":           "fmp" in names,
        "max_searches":      max_searches,
        "max_av_calls":      ALPHA_VANTAGE_MAX_CALLS,
        "max_fmp_calls":     FMP_MAX_CALLS,
    }
