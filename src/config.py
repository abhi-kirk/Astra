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
ANTHROPIC_API_KEY        = _cfg("ANTHROPIC_API_KEY",        default="")
TAVILY_MCP_URL           = _cfg("TAVILY_MCP_URL",           default="")
ALPHA_VANTAGE_API_KEY    = _cfg("ALPHA_VANTAGE_API_KEY",    default="")
# Community-hosted, no auth. Falls back gracefully if unavailable.
SEC_EDGAR_MCP_URL        = _cfg("SEC_EDGAR_MCP_URL",        default="https://secedgar.caseyjhand.com/mcp")
FMP_API_KEY              = _cfg("FMP_API_KEY",              default="")

# ── AI reasoning ──────────────────────────────────────────────
REASONING_MODEL      = _cfg("REASONING_MODEL",      default="claude-opus-4-8")
# Adaptive thinking counts toward max_tokens — keep headroom (still <16k, so non-streaming is safe).
REASONING_MAX_TOKENS = _cfg("REASONING_MAX_TOKENS", cast=int, default=8000)
ADVISOR_EFFORT       = _cfg("ADVISOR_EFFORT",       default="high")  # output_config.effort: low|medium|high|max
TAVILY_MAX_SEARCHES  = _cfg("TAVILY_MAX_SEARCHES",  cast=int, default=5)
# Alpha Vantage free tier: 25 calls/day, 5 calls/min
# Claude will use tools selectively — cap to avoid blowing the daily limit in one run
ALPHA_VANTAGE_MAX_CALLS  = _cfg("ALPHA_VANTAGE_MAX_CALLS",  cast=int, default=8)
# FMP free tier: 250 calls/day. Analyst data (price targets, grades) only available for
# large/mid caps on the free plan — small caps (RKLB, ASTS) gracefully return nothing.
FMP_MAX_CALLS            = _cfg("FMP_MAX_CALLS",            cast=int, default=10)

# ── Client-side MCP tool loop ─────────────────────────────────
# We run Claude's tool-use loop ourselves (see src/mcp_loop.py) instead of the server-side
# MCP connector, so each tool call is individually bounded and observable.
ADVISOR_TOOL_TIMEOUT     = _cfg("ADVISOR_TOOL_TIMEOUT",     cast=int, default=45)  # per MCP tool call, seconds
ADVISOR_MAX_TOOL_ROUNDS  = _cfg("ADVISOR_MAX_TOOL_ROUNDS",  cast=int, default=8)   # max model↔tool rounds before forcing a note
MCP_TOOL_FAILURE_LIMIT   = _cfg("MCP_TOOL_FAILURE_LIMIT",   cast=int, default=2)   # circuit-breaker: disable a server after N failures

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
RULE_THEME_CAP_EXEMPT       = _cfg("RULE_THEME_CAP_EXEMPT",       default="core_tech")       # theme(s) exempt from the cap
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

# ══════════════════════════════════════════════════════════════════════════════
# Brain (src/brain/) — composite-scoring engine. EVERY tunable is here; the brain
# logic contains no literals. See src/brain/README.md for the formulas these feed.
# ══════════════════════════════════════════════════════════════════════════════

# ── Brain: conviction weights (the anchor — gate + weight multiplier) ──────────
BRAIN_CONVICTION_PREFERRED  = _cfg("BRAIN_CONVICTION_PREFERRED",  cast=float, default=1.0)
BRAIN_CONVICTION_APPROVED   = _cfg("BRAIN_CONVICTION_APPROVED",   cast=float, default=0.7)
BRAIN_CONVICTION_HOLD       = _cfg("BRAIN_CONVICTION_HOLD",       cast=float, default=0.4)

# ── Brain: pillar weights (balanced) — composite S = Σ w·pillar ───────────────
BRAIN_WEIGHT_QUALITY        = _cfg("BRAIN_WEIGHT_QUALITY",        cast=float, default=0.25)
BRAIN_WEIGHT_VALUATION      = _cfg("BRAIN_WEIGHT_VALUATION",      cast=float, default=0.20)
BRAIN_WEIGHT_TREND          = _cfg("BRAIN_WEIGHT_TREND",          cast=float, default=0.20)
BRAIN_WEIGHT_ENTRY          = _cfg("BRAIN_WEIGHT_ENTRY",          cast=float, default=0.20)
BRAIN_WEIGHT_REVISIONS      = _cfg("BRAIN_WEIGHT_REVISIONS",      cast=float, default=0.15)

# ── Brain: decision thresholds on Score_buy = C·S ─────────────────────────────
BRAIN_BUY_THRESHOLD         = _cfg("BRAIN_BUY_THRESHOLD",         cast=float, default=0.50)
BRAIN_WATCH_THRESHOLD       = _cfg("BRAIN_WATCH_THRESHOLD",       cast=float, default=0.35)

# ── Brain: sizing (fractional-Kelly proxy, vol-scaled) ────────────────────────
# target_w = F_GLOBAL · Score_buy · vol_scalar ; vol_scalar = clip(VOL_REF/atr_pct, MIN, MAX)
BRAIN_F_GLOBAL              = _cfg("BRAIN_F_GLOBAL",              cast=float, default=0.10)  # master risk dial
BRAIN_SIZE_MIN_PCT          = _cfg("BRAIN_SIZE_MIN_PCT",          cast=float, default=0.02)  # skip sub-2% dust buys
BRAIN_VOL_REF              = _cfg("BRAIN_VOL_REF",               cast=float, default=0.03)  # reference daily ATR% (3%)
BRAIN_VOL_SCALAR_MIN        = _cfg("BRAIN_VOL_SCALAR_MIN",        cast=float, default=0.5)
BRAIN_VOL_SCALAR_MAX        = _cfg("BRAIN_VOL_SCALAR_MAX",        cast=float, default=1.5)
BRAIN_MAX_NEW_DEPLOY_PCT    = _cfg("BRAIN_MAX_NEW_DEPLOY_PCT",    cast=float, default=0.25)  # cap on new BUYs per run
# (single-name cap = RULE_MAX_POSITION_PCT, theme cap = RULE_MAX_THEME_PCT — reused)

# ── Brain: quality pillar ramps (smooth ramp endpoints) ───────────────────────
BRAIN_Q_REV_LOW             = _cfg("BRAIN_Q_REV_LOW",             cast=float, default=0.0)
BRAIN_Q_REV_HIGH            = _cfg("BRAIN_Q_REV_HIGH",            cast=float, default=0.30)
BRAIN_Q_GM_LOW              = _cfg("BRAIN_Q_GM_LOW",              cast=float, default=0.20)
BRAIN_Q_GM_HIGH             = _cfg("BRAIN_Q_GM_HIGH",             cast=float, default=0.60)
BRAIN_Q_DE_GOOD             = _cfg("BRAIN_Q_DE_GOOD",             cast=float, default=100.0)
BRAIN_Q_DE_BAD              = _cfg("BRAIN_Q_DE_BAD",              cast=float, default=300.0)
BRAIN_Q_CR_LOW              = _cfg("BRAIN_Q_CR_LOW",              cast=float, default=1.0)
BRAIN_Q_CR_HIGH             = _cfg("BRAIN_Q_CR_HIGH",             cast=float, default=1.5)
BRAIN_Q_CATASTROPHIC_CAP    = _cfg("BRAIN_Q_CATASTROPHIC_CAP",    cast=float, default=0.15)  # rev-declining / neg-GM cap

# ── Brain: valuation pillar ───────────────────────────────────────────────────
BRAIN_V_PEG_LOW             = _cfg("BRAIN_V_PEG_LOW",             cast=float, default=1.0)   # PEG≤1 → best
BRAIN_V_PEG_HIGH            = _cfg("BRAIN_V_PEG_HIGH",            cast=float, default=3.0)   # PEG≥3 → 0
BRAIN_V_PE_LOW              = _cfg("BRAIN_V_PE_LOW",              cast=float, default=15.0)  # fwd PE≤15 → best
BRAIN_V_PE_HIGH             = _cfg("BRAIN_V_PE_HIGH",             cast=float, default=40.0)  # fwd PE≥40 → 0
BRAIN_V_NEUTRAL             = _cfg("BRAIN_V_NEUTRAL",             cast=float, default=0.5)   # missing data

# ── Brain: trend pillar ───────────────────────────────────────────────────────
BRAIN_T_REGIME_TERM         = _cfg("BRAIN_T_REGIME_TERM",        cast=float, default=0.5)   # ±contribution of regime
BRAIN_T_MOM_SCALE           = _cfg("BRAIN_T_MOM_SCALE",          cast=float, default=0.25)  # tanh scale for 12-1 mom
BRAIN_T_MA50_SCALE          = _cfg("BRAIN_T_MA50_SCALE",         cast=float, default=15.0)  # tanh scale for price-vs-50MA %

# ── Brain: entry-timing pillar (pullback measured in ATR units) ───────────────
BRAIN_E_PULLBACK_PEAK_LOW   = _cfg("BRAIN_E_PULLBACK_PEAK_LOW",   cast=float, default=1.0)   # ATRs: start of sweet spot
BRAIN_E_PULLBACK_PEAK_HIGH  = _cfg("BRAIN_E_PULLBACK_PEAK_HIGH",  cast=float, default=3.0)   # ATRs: end of sweet spot
BRAIN_E_PULLBACK_MAX        = _cfg("BRAIN_E_PULLBACK_MAX",        cast=float, default=5.0)   # ATRs: broken beyond this
BRAIN_E_MA_PROXIMITY_PCT    = _cfg("BRAIN_E_MA_PROXIMITY_PCT",    cast=float, default=8.0)   # near rising 50-MA within %
BRAIN_E_RSI_LOW             = _cfg("BRAIN_E_RSI_LOW",             cast=float, default=40.0)  # uptrend healthy-pullback low
BRAIN_E_RSI_HIGH            = _cfg("BRAIN_E_RSI_HIGH",            cast=float, default=55.0)  # uptrend healthy-pullback high
BRAIN_E_RSI_OVERBOUGHT      = _cfg("BRAIN_E_RSI_OVERBOUGHT",      cast=float, default=70.0)
BRAIN_E_RSI_OVERSOLD        = _cfg("BRAIN_E_RSI_OVERSOLD",        cast=float, default=35.0)  # downtrend deep-value reward

# ── Brain: revisions pillar (soft, best-effort — never forces a sell) ─────────
BRAIN_R_NORM                = _cfg("BRAIN_R_NORM",                cast=float, default=3.0)   # net revisions → ±1 scale

# ── Brain: exit stack ─────────────────────────────────────────────────────────
BRAIN_EXIT_REV_DECLINE      = _cfg("BRAIN_EXIT_REV_DECLINE",      cast=float, default=-0.10) # rev growth below → thesis broken
BRAIN_EXIT_GM_COLLAPSE      = _cfg("BRAIN_EXIT_GM_COLLAPSE",      cast=float, default=0.0)   # gross margin below → thesis broken
BRAIN_TRAIL_ATR_MULT        = _cfg("BRAIN_TRAIL_ATR_MULT",        cast=float, default=3.0)   # Chandelier k
BRAIN_TRIM_GAIN_PCT         = _cfg("BRAIN_TRIM_GAIN_PCT",         cast=float, default=0.50)  # parabolic: min unrealized gain
BRAIN_TRIM_RSI              = _cfg("BRAIN_TRIM_RSI",              cast=float, default=70.0)  # parabolic: overbought
BRAIN_TRIM_MA_EXT_ATR_MULT  = _cfg("BRAIN_TRIM_MA_EXT_ATR_MULT",  cast=float, default=4.0)   # parabolic: ATRs above 50-MA
BRAIN_TRIM_FRACTION         = _cfg("BRAIN_TRIM_FRACTION",         cast=float, default=0.3333)  # trim ~⅓, keep runner

# ── Brain: data-layer technical windows ───────────────────────────────────────
BRAIN_ATR_PERIOD            = _cfg("BRAIN_ATR_PERIOD",            cast=int,   default=14)
BRAIN_SWING_HIGH_LOOKBACK   = _cfg("BRAIN_SWING_HIGH_LOOKBACK",   cast=int,   default=22)   # Chandelier/pullback high window
BRAIN_MOM_LOOKBACK_DAYS     = _cfg("BRAIN_MOM_LOOKBACK_DAYS",     cast=int,   default=252)  # 12-month
BRAIN_MOM_SKIP_DAYS         = _cfg("BRAIN_MOM_SKIP_DAYS",         cast=int,   default=21)   # skip most-recent month (12-1)

# ── Robinhood live portfolio sync ─────────────────────────────
ROBINHOOD_ACCOUNT_NUMBER = _cfg("ROBINHOOD_ACCOUNT_NUMBER", default="")   # primary account (read-only)
ROBINHOOD_TOKEN_KEY      = _cfg("ROBINHOOD_TOKEN_KEY",      default="")   # base64 AES-256 key for tokens.enc
ROBINHOOD_TOKENS_FILE    = _cfg("ROBINHOOD_TOKENS_FILE",    default="tokens.enc")

# ── Autotrader: autonomous agentic trading (Phase 2) ─────────────
# Real-money autonomous execution in a dedicated Robinhood Agentic account, mirroring the
# paper track and filtered by code-enforced guardrails. Master switch defaults False — no
# real order is placed until this is explicitly enabled AND guardrail tests pass. See
# docs/autonomy.md.
# Robust truthy parse — an unset GitHub secret injects "" (empty), which would crash a
# strict bool cast; empty/unset/anything-not-truthy means disabled.
AGENT_TRADING_ENABLED    = _cfg("AGENT_TRADING_ENABLED", default="false").strip().lower() in ("true", "1", "yes", "on")
AGENT_ACCOUNT_NUMBER     = _cfg("AGENT_ACCOUNT_NUMBER",     default="")   # agentic_allowed=true account
AGENT_MAX_TRADES_PER_DAY = _cfg("AGENT_MAX_TRADES_PER_DAY", cast=int,   default=3)
AGENT_MIN_HOLD_DAYS      = _cfg("AGENT_MIN_HOLD_DAYS",      cast=int,   default=2)     # trading days before a sell is allowed
AGENT_DRAWDOWN_HALT_PCT  = _cfg("AGENT_DRAWDOWN_HALT_PCT",  cast=float, default=-15.0) # halt if account draws down past this %
AGENT_MAX_OPEN_POSITIONS = _cfg("AGENT_MAX_OPEN_POSITIONS", cast=int,   default=5)
# Per-buy size as a fraction of agentic sleeve equity. The sleeve is small (~$1k), so
# mirroring the paper track's 4–6% would leave ~75% idle cash — 20% × 5 max positions
# deploys the sleeve fully while keeping 5-name diversification.
AGENT_POSITION_PCT       = _cfg("AGENT_POSITION_PCT",       cast=float, default=0.20)
# Market orders with dollar sizing — the fractional-share fit for a small (~$1k) account
# (Robinhood allows fractional shares only on market orders, not limit).
AGENT_ORDER_TYPE         = _cfg("AGENT_ORDER_TYPE",         default="market")
# Cash account: T+1 settlement + Good-Faith-Violation aware (block selling unsettled / same-day round-trips)
AGENT_ACCOUNT_IS_CASH    = _cfg("AGENT_ACCOUNT_IS_CASH",    cast=bool,  default=True)
# Robinhood Agentic MCP (official execution endpoint) + its own encrypted OAuth token store
AGENT_RH_MCP_URL         = _cfg("AGENT_RH_MCP_URL",         default="https://agent.robinhood.com/mcp/trading")
AGENT_RH_TOKEN_KEY       = _cfg("AGENT_RH_TOKEN_KEY",       default="")                 # base64 AES-256 key for the agentic OAuth blob
AGENT_RH_TOKENS_FILE     = _cfg("AGENT_RH_TOKENS_FILE",     default="agent_tokens.enc")

# ── Exploration (weekly discovery run) ───────────────────────
EXPLORATION_MODEL         = _cfg("EXPLORATION_MODEL",         default="claude-opus-4-8")
EXPLORATION_MAX_TOKENS    = _cfg("EXPLORATION_MAX_TOKENS",    cast=int, default=8000)
EXPLORATION_EFFORT        = _cfg("EXPLORATION_EFFORT",        default="medium")  # output_config.effort
EXPLORATION_MAX_SEARCHES  = _cfg("EXPLORATION_MAX_SEARCHES",  cast=int, default=2)   # per theme
EXPLORATION_MAX_AV_CALLS  = _cfg("EXPLORATION_MAX_AV_CALLS",  cast=int, default=8)
EXPLORATION_MAX_FMP_CALLS = _cfg("EXPLORATION_MAX_FMP_CALLS", cast=int, default=15)

# ── Claude API call timeouts ──────────────────────────────────
# Wall-clock backstop for the whole client-side tool loop. Each tool call is bounded by
# ADVISOR_TOOL_TIMEOUT, so this should now rarely fire.
ADVISOR_TIMEOUT     = _cfg("ADVISOR_TIMEOUT",     cast=int, default=300)  # 5 min backstop
EXPLORATION_TIMEOUT = _cfg("EXPLORATION_TIMEOUT", cast=int, default=300)  # 5 min backstop

# ── Strategy: position sizing ─────────────────────────────────
SIZE_PREFERRED_PCT          = _cfg("SIZE_PREFERRED_PCT",          cast=float, default=0.06)
SIZE_APPROVED_PCT           = _cfg("SIZE_APPROVED_PCT",           cast=float, default=0.04)

# ── Notifications (Telegram) ──────────────────────────────────
# Bot token from @BotFather; chat id of the destination chat. Both unset = no-op.
TELEGRAM_BOT_TOKEN = _cfg("TELEGRAM_BOT_TOKEN", default="")
TELEGRAM_CHAT_ID   = _cfg("TELEGRAM_CHAT_ID",   default="")
