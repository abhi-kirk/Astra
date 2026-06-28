"""
Centralised configuration. All env vars and tunable constants live here.
Other modules import from this file — never read os.environ directly.

In GitHub Actions: values come from exported secrets (no .env file needed).
Locally: starlette.config reads the .env file at project root.
"""

from pathlib import Path

from starlette.config import Config

_cfg = Config(Path(__file__).parent.parent / ".env")

# ── Supabase ───────────────────────────────────────────────────
SUPABASE_URL         = _cfg("SUPABASE_URL",         default="")
SUPABASE_SERVICE_KEY = _cfg("SUPABASE_SERVICE_KEY", default="")
SUPABASE_ANON_KEY    = _cfg("SUPABASE_ANON_KEY",    default="")

# ── Anthropic / MCP ───────────────────────────────────────────
ANTHROPIC_API_KEY = _cfg("ANTHROPIC_API_KEY", default="")
TAVILY_MCP_URL    = _cfg("TAVILY_MCP_URL",    default="")

# ── AI reasoning ──────────────────────────────────────────────
REASONING_MODEL      = _cfg("REASONING_MODEL",      default="claude-sonnet-4-6")
REASONING_MAX_TOKENS = _cfg("REASONING_MAX_TOKENS", cast=int, default=3000)
TAVILY_MAX_SEARCHES  = _cfg("TAVILY_MAX_SEARCHES",  cast=int, default=5)

# ── Paper trading ─────────────────────────────────────────────
PAPER_PORTFOLIO_SIZE       = _cfg("PAPER_PORTFOLIO_SIZE",       cast=float, default=10_000.0)
PAPER_MAX_POSITION_PCT     = _cfg("PAPER_MAX_POSITION_PCT",     cast=float, default=0.10)
PAPER_DEFAULT_POSITION_PCT = _cfg("PAPER_DEFAULT_POSITION_PCT", cast=float, default=0.04)

# ── Market data ───────────────────────────────────────────────
MARKET_DATA_WORKERS     = _cfg("MARKET_DATA_WORKERS",     cast=int, default=8)
MARKET_DATA_PERIOD_DAYS = _cfg("MARKET_DATA_PERIOD_DAYS", cast=int, default=365)
