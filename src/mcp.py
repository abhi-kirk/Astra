"""
MCP server registry for ASTRA's Anthropic API calls.

Each integration is a function that returns a server dict (or None if unconfigured).
build_servers() assembles the active list — only servers with a configured URL/key
are included, so adding a new MCP is one entry here + its config vars.

Usage in agent.py:
    servers = mcp.build_servers()
    if servers:
        message = client.beta.messages.create(..., mcp_servers=servers, betas=mcp.BETA_FLAGS)
        text = mcp.extract_text(message)
    else:
        message = client.messages.create(...)
        text = mcp.extract_text(message)
"""

from __future__ import annotations

import re
from typing import Any

from anthropic.types import TextBlock
from anthropic.types.beta import (
    BetaRequestMCPServerToolConfigurationParam,
    BetaRequestMCPServerURLDefinitionParam,
    BetaTextBlock,
)

from src.config import TAVILY_MAX_SEARCHES, TAVILY_MCP_URL

# Beta flags required for MCP client support
BETA_FLAGS = ["mcp-client-2025-04-04"]


# ---------------------------------------------------------------------------
# Server definitions — one function per integration
# ---------------------------------------------------------------------------

def _tavily() -> BetaRequestMCPServerURLDefinitionParam | None:
    if not TAVILY_MCP_URL:
        return None
    tool_config: BetaRequestMCPServerToolConfigurationParam = {
        "enabled": True,
        "allowed_tools": ["tavily-search"],
    }
    return {"type": "url", "url": TAVILY_MCP_URL, "name": "tavily", "tool_configuration": tool_config}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_servers() -> list[BetaRequestMCPServerURLDefinitionParam]:
    """Return the list of active MCP server dicts for the current config."""
    candidates = [
        _tavily(),
        # _alpha_vantage(),  # Phase 1.5 — add when key is configured
        # _sec_edgar(),      # Phase 1.5 — no auth needed
        # _fmp(),            # Phase 1.5 — add last
    ]
    return [s for s in candidates if s is not None]


def extract_text(message: Any) -> str:
    """
    Pull plain text out of an Anthropic API response.

    Handles both the standard Messages response (TextBlock) and the beta
    MCP response (BetaTextBlock). For MCP responses, strips tool call/response
    XML and any preamble before the first markdown heading.
    """
    content = message.content

    # Beta MCP path — take the last text block (post tool-use)
    beta_blocks = [b.text for b in content if isinstance(b, BetaTextBlock)]
    if beta_blocks:
        raw = beta_blocks[-1]
        clean = re.sub(r"<tool_call>.*?</tool_call>", "", raw, flags=re.DOTALL)
        clean = re.sub(r"<tool_response>.*?</tool_response>", "", clean, flags=re.DOTALL)
        lines = clean.split("\n")
        first_heading = next((i for i, ln in enumerate(lines) if re.match(r"^#{1,3} ", ln)), None)
        if first_heading is not None:
            clean = "\n".join(lines[first_heading:])
        return clean.strip()

    # Standard path
    return next((b.text for b in content if isinstance(b, TextBlock)), "")


def search_context(max_searches: int = TAVILY_MAX_SEARCHES) -> dict[str, Any]:
    """Template vars for the news instruction block in advisor_note.mustache."""
    return {
        "has_search": bool(build_servers()),
        "max_searches": max_searches,
    }
