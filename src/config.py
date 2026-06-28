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

# ── Strategy: hard rules ───────────────────────────────────────
RULE_MAX_POSITION_PCT       = _cfg("RULE_MAX_POSITION_PCT",       cast=float, default=0.10)  # single name cap
RULE_MAX_THEME_PCT          = _cfg("RULE_MAX_THEME_PCT",          cast=float, default=0.15)  # speculative theme cap
RULE_AVERAGING_DOWN_DRAWDOWN= _cfg("RULE_AVERAGING_DOWN_DRAWDOWN",cast=float, default=0.35)  # max drawdown before avg-down blocked
RULE_AVERAGING_DOWN_MAX_BUYS= _cfg("RULE_AVERAGING_DOWN_MAX_BUYS",cast=int,   default=3)     # max buys before avg-down blocked
RULE_PROFIT_TAKE_PCT        = _cfg("RULE_PROFIT_TAKE_PCT",        cast=float, default=0.60)  # unrealized gain threshold for sell signal

# ── Strategy: quality filter ───────────────────────────────────
QUALITY_MIN_REVENUE_GROWTH  = _cfg("QUALITY_MIN_REVENUE_GROWTH",  cast=float, default=0.10)  # YoY
QUALITY_MIN_GROSS_MARGIN    = _cfg("QUALITY_MIN_GROSS_MARGIN",    cast=float, default=0.30)
QUALITY_MAX_DEBT_EQUITY     = _cfg("QUALITY_MAX_DEBT_EQUITY",     cast=float, default=150.0)
QUALITY_MIN_CHECKS_TO_PASS  = _cfg("QUALITY_MIN_CHECKS_TO_PASS",  cast=int,   default=2)

# ── Strategy: technical signal ────────────────────────────────
TECH_MIN_PCT_BELOW_52W_HIGH = _cfg("TECH_MIN_PCT_BELOW_52W_HIGH", cast=float, default=15.0)  # %
TECH_MAX_RSI                = _cfg("TECH_MAX_RSI",                cast=float, default=40.0)
TECH_SIGNALS_REQUIRED       = _cfg("TECH_SIGNALS_REQUIRED",       cast=int,   default=2)

# ── Strategy: position sizing ─────────────────────────────────
SIZE_PREFERRED_PCT          = _cfg("SIZE_PREFERRED_PCT",          cast=float, default=0.06)
SIZE_APPROVED_PCT           = _cfg("SIZE_APPROVED_PCT",           cast=float, default=0.04)
