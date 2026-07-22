"""
Centralised configuration. All env vars and tunable constants live here.
Other modules import from this file — never read os.environ directly.

Config is grouped into section dataclasses (one per concern), each instantiated as a
lowercase module-level singleton. Access is dotted, e.g. `config.brain.buy_threshold`,
`config.agent.trading_enabled`. Dataclasses are mutable so tests can monkeypatch a single
field in place.

In GitHub Actions: values come from exported secrets (no .env file needed).
Locally: starlette.config reads the .env file at project root.
"""

from dataclasses import dataclass
from pathlib import Path

from starlette.config import Config

_cfg = Config(Path(__file__).parent.parent / ".env")


# ── Supabase ───────────────────────────────────────────────────────────────────
@dataclass
class SupabaseConfig:
    url: str = _cfg("SUPABASE_URL", default="")
    service_key: str = _cfg("SUPABASE_SERVICE_KEY", default="")  # full access — backend writes only
    anon_key: str = _cfg("SUPABASE_ANON_KEY", default="")        # public read — safe for the dashboard JS


# ── External services (Anthropic + MCP data providers) ─────────────────────────
@dataclass
class ServicesConfig:
    anthropic_api_key: str = _cfg("ANTHROPIC_API_KEY", default="")            # LLM for advisor + exploration reasoning
    tavily_mcp_url: str = _cfg("TAVILY_MCP_URL", default="")                  # hosted web-search MCP endpoint
    alpha_vantage_api_key: str = _cfg("ALPHA_VANTAGE_API_KEY", default="")    # news sentiment / earnings / company-overview MCP
    # Community-hosted, no auth. Falls back gracefully if unavailable.
    sec_edgar_mcp_url: str = _cfg("SEC_EDGAR_MCP_URL", default="https://secedgar.caseyjhand.com/mcp")  # SEC filings MCP
    fmp_api_key: str = _cfg("FMP_API_KEY", default="")                        # analyst price-targets / grades MCP (large/mid caps only)


# ── AI reasoning ───────────────────────────────────────────────────────────────
@dataclass
class ReasoningConfig:
    model: str = _cfg("REASONING_MODEL", default="claude-opus-4-8")  # advisor narrative-reasoning model
    # Adaptive thinking counts toward max_tokens — keep headroom (still <16k, so non-streaming is safe).
    max_tokens: int = _cfg("REASONING_MAX_TOKENS", cast=int, default=8000)
    advisor_effort: str = _cfg("ADVISOR_EFFORT", default="high")  # output_config.effort: low|medium|high|max
    tavily_max_searches: int = _cfg("TAVILY_MAX_SEARCHES", cast=int, default=5)  # web searches per advisor run
    # Alpha Vantage free tier: 25 calls/day, 5 calls/min. Claude uses tools selectively —
    # cap to avoid blowing the daily limit in one run.
    alpha_vantage_max_calls: int = _cfg("ALPHA_VANTAGE_MAX_CALLS", cast=int, default=8)
    # FMP free tier: 250 calls/day. Analyst data (price targets, grades) only available for
    # large/mid caps on the free plan — small caps (RKLB, ASTS) gracefully return nothing.
    fmp_max_calls: int = _cfg("FMP_MAX_CALLS", cast=int, default=10)


# ── Client-side MCP tool loop ──────────────────────────────────────────────────
# We run Claude's tool-use loop ourselves (see src/mcp_loop.py) instead of the server-side
# MCP connector, so each tool call is individually bounded and observable.
@dataclass
class ToolLoopConfig:
    advisor_tool_timeout: int = _cfg("ADVISOR_TOOL_TIMEOUT", cast=int, default=45)     # per MCP tool call, seconds
    advisor_max_tool_rounds: int = _cfg("ADVISOR_MAX_TOOL_ROUNDS", cast=int, default=8)  # max model↔tool rounds before forcing a note
    mcp_tool_failure_limit: int = _cfg("MCP_TOOL_FAILURE_LIMIT", cast=int, default=2)   # circuit-breaker: disable a server after N failures


# ── Paper trading ──────────────────────────────────────────────────────────────
@dataclass
class PaperConfig:
    portfolio_size: float = _cfg("PAPER_PORTFOLIO_SIZE", cast=float, default=10_000.0)       # fallback sizing book when no live Individual-account balance is on record
    max_position_pct: float = _cfg("PAPER_MAX_POSITION_PCT", cast=float, default=0.10)        # hard per-name cap (fraction of book)
    default_position_pct: float = _cfg("PAPER_DEFAULT_POSITION_PCT", cast=float, default=0.04)  # fallback buy size when no explicit sizing
    # Bounded pyramiding (adds): a persisting BUY on a held name opens an additional paper lot,
    # up to N adds and no more often than the cooldown. Position-size + averaging-down hard rules
    # still gate over-concentration upstream (a buy over cap is `blocked`, never reaches here).
    max_adds_per_ticker: int = _cfg("PAPER_MAX_ADDS_PER_TICKER", cast=int, default=3)  # additional lots beyond the initial (0 = no pyramiding)
    add_cooldown_days: int = _cfg("PAPER_ADD_COOLDOWN_DAYS", cast=int, default=5)      # min calendar days between adds on the same name


# ── Market data ────────────────────────────────────────────────────────────────
@dataclass
class MarketDataConfig:
    workers: int = _cfg("MARKET_DATA_WORKERS", cast=int, default=8)          # yfinance fetch thread-pool size
    period_days: int = _cfg("MARKET_DATA_PERIOD_DAYS", cast=int, default=365)  # trailing price-history window pulled per ticker


# ── Strategy: hard rules ───────────────────────────────────────────────────────
@dataclass
class RulesConfig:
    max_position_pct: float = _cfg("RULE_MAX_POSITION_PCT", cast=float, default=0.10)             # single-name cap (fraction of book)
    max_theme_pct: float = _cfg("RULE_MAX_THEME_PCT", cast=float, default=0.15)                   # speculative-theme cap (fraction of book)
    theme_cap_exempt: str = _cfg("RULE_THEME_CAP_EXEMPT", default="core_tech")                    # theme(s) exempt from the cap
    averaging_down_drawdown: float = _cfg("RULE_AVERAGING_DOWN_DRAWDOWN", cast=float, default=0.35)  # drawdown (fraction, 0.35 = 35%) past which avg-down is blocked
    averaging_down_max_buys: int = _cfg("RULE_AVERAGING_DOWN_MAX_BUYS", cast=int, default=3)         # max additional buys before avg-down is blocked


# ── Strategy: quality filter ───────────────────────────────────────────────────
# Vets NEW exploration candidates (src/strategy.quality_filter). Live position scoring
# uses the brain quality pillar instead — these thresholds don't feed the brain.
@dataclass
class QualityConfig:
    min_revenue_growth: float = _cfg("QUALITY_MIN_REVENUE_GROWTH", cast=float, default=0.10)  # YoY; must exceed to pass the growth check
    min_gross_margin: float = _cfg("QUALITY_MIN_GROSS_MARGIN", cast=float, default=0.30)      # must exceed to pass the margin check
    max_debt_equity: float = _cfg("QUALITY_MAX_DEBT_EQUITY", cast=float, default=150.0)       # D/E as a percent (150 = 1.5×); below → passes leverage check
    min_checks_to_pass: int = _cfg("QUALITY_MIN_CHECKS_TO_PASS", cast=int, default=2)         # checks that must pass (of rev/GM/D/E/FCF) to clear


# ── Strategy: technical signal ─────────────────────────────────────────────────
# Dip-entry vetting for exploration candidates (src/strategy.technical_signal). Live
# entry timing uses the brain entry pillar (ATR-based, regime-aware) instead.
@dataclass
class TechConfig:
    min_pct_below_52w_high: float = _cfg("TECH_MIN_PCT_BELOW_52W_HIGH", cast=float, default=15.0)  # % below 52w high to count as a dip signal
    max_rsi: float = _cfg("TECH_MAX_RSI", cast=float, default=40.0)                                # RSI below this counts as an oversold signal
    signals_required: int = _cfg("TECH_SIGNALS_REQUIRED", cast=int, default=2)                     # oversold signals needed (of dip-depth + RSI)


# ══════════════════════════════════════════════════════════════════════════════
# Brain (src/brain/) — composite-scoring engine. EVERY tunable is here; the brain
# logic contains no literals. See src/brain/README.md for the formulas these feed.
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class BrainConfig:
    # ── Conviction weights (the anchor C — both the BUY gate and the score multiplier;
    # a ticker must map to one of these statuses to be buyable at all) ─────────
    conviction_preferred: float = _cfg("BRAIN_CONVICTION_PREFERRED", cast=float, default=1.0)  # (legacy bucket path) named holding / core high-conviction
    conviction_approved: float = _cfg("BRAIN_CONVICTION_APPROVED", cast=float, default=0.7)    # (legacy bucket path) approved theme, not a named holding
    conviction_hold: float = _cfg("BRAIN_CONVICTION_HOLD", cast=float, default=0.4)            # hold-only — no fresh buys without a catalyst
    # Conviction-primary: C is derived from the THEME conviction label (industry-level, per the
    # "conviction is industry-level" decision), NOT the preferred/approved bucket — H (thesis-health)
    # does the per-name differentiation the buckets used to. hold_only/do_not_add stay behavioral gates.
    conviction_very_high: float = _cfg("BRAIN_CONVICTION_VERY_HIGH", cast=float, default=1.0)
    conviction_high: float = _cfg("BRAIN_CONVICTION_HIGH", cast=float, default=0.8)   # < very_high, so a very_high theme leads
    conviction_medium: float = _cfg("BRAIN_CONVICTION_MEDIUM", cast=float, default=0.6)
    conviction_low: float = _cfg("BRAIN_CONVICTION_LOW", cast=float, default=0.4)

    # ── Pillar weights — composite S = Σ w·pillar; the five weights sum to 1.0 ──
    weight_quality: float = _cfg("BRAIN_WEIGHT_QUALITY", cast=float, default=0.25)        # fundamentals (growth, margins, leverage, liquidity)
    weight_valuation: float = _cfg("BRAIN_WEIGHT_VALUATION", cast=float, default=0.20)    # PEG / forward-PE (best-effort, neutral when missing)
    weight_trend: float = _cfg("BRAIN_WEIGHT_TREND", cast=float, default=0.20)            # regime + 12-1 momentum + price-vs-50MA
    weight_entry: float = _cfg("BRAIN_WEIGHT_ENTRY", cast=float, default=0.20)            # pullback timing (ATR depth, MA proximity, RSI)
    weight_revisions: float = _cfg("BRAIN_WEIGHT_REVISIONS", cast=float, default=0.15)    # analyst estimate revisions (soft signal)

    # ── Decision thresholds on the gate score ──────────────────────────────────
    buy_threshold: float = _cfg("BRAIN_BUY_THRESHOLD", cast=float, default=0.45)      # gate ≥ this → BUY signal (aggressive: was 0.50)
    watch_threshold: float = _cfg("BRAIN_WATCH_THRESHOLD", cast=float, default=0.30)  # watch ≤ gate < buy → WATCH; below → no action

    # ── Conviction-primary gate (docs/conviction_primary.md) ───────────────────
    # When on, the buy/watch/hold GATE = conviction × thesis-health (OwnScore = C·H), NOT the
    # market composite C·S — the market (trend/entry/valuation/revisions) never votes on whether
    # to own, only on sizing/pacing downstream. Default off; flip to prototype/adopt Stage 1.
    conviction_primary: bool = _cfg("BRAIN_CONVICTION_PRIMARY", cast=bool, default=True)
    thesis_neutral: float = _cfg("BRAIN_THESIS_NEUTRAL", cast=float, default=0.5)  # H fallback when fundamentals are absent (ETFs/ADRs) — conviction then carries the gate
    # Selective buy bar for the C·H gate (higher than the legacy C·S bars — fewer, stronger buys).
    cp_buy_threshold: float = _cfg("BRAIN_CP_BUY_THRESHOLD", cast=float, default=0.60)    # C·H ≥ this → BUY
    cp_watch_threshold: float = _cfg("BRAIN_CP_WATCH_THRESHOLD", cast=float, default=0.35)  # C·H ≥ this → WATCH

    # ── Sizing (fractional-Kelly proxy, vol-scaled) ────────────────────────────
    # target_w = f_global · Score_buy · vol_scalar ; vol_scalar = clip(vol_ref/atr_pct, min, max)
    f_global: float = _cfg("BRAIN_F_GLOBAL", cast=float, default=0.10)              # master risk dial (fraction) scaling every target weight
    size_min_pct: float = _cfg("BRAIN_SIZE_MIN_PCT", cast=float, default=0.015)     # skip buys below this target weight (fraction, 0.015 = 1.5%)
    vol_ref: float = _cfg("BRAIN_VOL_REF", cast=float, default=0.03)                # reference daily ATR (fraction, 0.03 = 3%) — the vol_scalar pivot
    vol_scalar_min: float = _cfg("BRAIN_VOL_SCALAR_MIN", cast=float, default=0.5)  # floor: most a high-vol name's size is cut
    vol_scalar_max: float = _cfg("BRAIN_VOL_SCALAR_MAX", cast=float, default=1.5)  # ceil: most a low-vol name's size is boosted
    max_new_deploy_pct: float = _cfg("BRAIN_MAX_NEW_DEPLOY_PCT", cast=float, default=0.25)  # cap on total new BUYs per run (fraction of book)
    # (single-name cap = rules.max_position_pct, theme cap = rules.max_theme_pct — reused)
    # Stage-2 timing multiplier (conviction-primary only): the timing/deal signal M∈[0,1]
    # scales each buy's lot. AGGRESSIVE-DIP policy — M rewards *discount* (deeper drawdown +
    # cheaper + pulled-back → higher M → bigger lot); a name at highs sizes small. M=0.5 → ×1.0.
    size_timing_min: float = _cfg("BRAIN_SIZE_TIMING_MIN", cast=float, default=0.5)  # no discount (at highs) → half a lot
    size_timing_max: float = _cfg("BRAIN_SIZE_TIMING_MAX", cast=float, default=2.0)  # full discount (deep dip) → 2× a lot (back-up-the-truck)
    discount_full_pct: float = _cfg("BRAIN_DISCOUNT_FULL_PCT", cast=float, default=40.0)  # % below 52wk-high that counts as a full discount
    # Market-cycle overlay: when the broad market (SPY) is in a drawdown, boost every buy's lot
    # (buy into the cycle dip — the "market cycles" ask). Bounded; 1.0 = no effect at market highs.
    market_overlay_max: float = _cfg("BRAIN_MARKET_OVERLAY_MAX", cast=float, default=1.25)      # max lot boost when the market is deeply down
    market_overlay_full_dd_pct: float = _cfg("BRAIN_MARKET_OVERLAY_FULL_DD_PCT", cast=float, default=15.0)  # SPY % below its high = full boost

    # ── Quality pillar ramps ───────────────────────────────────────────────────
    # Each metric maps through smooth(x, lo, hi) → 0 below lo, 1 above hi, linear between;
    # the sub-scores (rev, GM, D/E, current-ratio, FCF-sign) are averaged into the pillar.
    q_rev_low: float = _cfg("BRAIN_Q_REV_LOW", cast=float, default=0.0)     # YoY revenue growth: 0% → score 0
    q_rev_high: float = _cfg("BRAIN_Q_REV_HIGH", cast=float, default=0.30)  #                    30% → score 1
    q_gm_low: float = _cfg("BRAIN_Q_GM_LOW", cast=float, default=0.20)      # gross margin: 20% → 0
    q_gm_high: float = _cfg("BRAIN_Q_GM_HIGH", cast=float, default=0.60)    #               60% → 1
    # debt/equity is a descending ramp (lower is better); reported as a percent (100 = 1.0× D/E).
    q_de_good: float = _cfg("BRAIN_Q_DE_GOOD", cast=float, default=100.0)   # D/E ≤ 100% (1.0×) → 1
    q_de_bad: float = _cfg("BRAIN_Q_DE_BAD", cast=float, default=300.0)     # D/E ≥ 300% (3.0×) → 0
    q_cr_low: float = _cfg("BRAIN_Q_CR_LOW", cast=float, default=1.0)       # current ratio: 1.0 → 0
    q_cr_high: float = _cfg("BRAIN_Q_CR_HIGH", cast=float, default=1.5)     #                1.5 → 1
    q_catastrophic_cap: float = _cfg("BRAIN_Q_CATASTROPHIC_CAP", cast=float, default=0.15)  # hard cap on quality if revenue declining or GM negative

    # ── Valuation pillar ───────────────────────────────────────────────────────
    v_peg_low: float = _cfg("BRAIN_V_PEG_LOW", cast=float, default=1.0)    # PEG≤1 → best
    v_peg_high: float = _cfg("BRAIN_V_PEG_HIGH", cast=float, default=3.0)  # PEG≥3 → 0
    v_pe_low: float = _cfg("BRAIN_V_PE_LOW", cast=float, default=15.0)     # fwd PE≤15 → best
    v_pe_high: float = _cfg("BRAIN_V_PE_HIGH", cast=float, default=40.0)   # fwd PE≥40 → 0
    v_neutral: float = _cfg("BRAIN_V_NEUTRAL", cast=float, default=0.5)    # missing data

    # ── Trend pillar ───────────────────────────────────────────────────────────
    t_regime_term: float = _cfg("BRAIN_T_REGIME_TERM", cast=float, default=0.5)   # ±contribution of regime (uptrend/downtrend)
    t_regime_pullback: float = _cfg("BRAIN_T_REGIME_PULLBACK", cast=float, default=0.0)  # regime term for a pullback-in-uptrend: neutral, not the -0.5 downtrend penalty
    t_mom_scale: float = _cfg("BRAIN_T_MOM_SCALE", cast=float, default=0.25)      # tanh scale for 12-1 mom
    t_ma50_scale: float = _cfg("BRAIN_T_MA50_SCALE", cast=float, default=15.0)    # tanh scale for price-vs-50MA %

    # ── Regime classification (pullback-in-uptrend vs genuine downtrend) ────────
    # A name below its 200-MA is a *pullback* (long-term trend intact) rather than a
    # downtrend only if it is (a) shallowly below the 200-MA and (b) the 200-MA is still
    # rising (slope preferred; golden-cross structure as fallback when slope is unknown).
    regime_pullback_max_below_ma200_pct: float = _cfg("BRAIN_REGIME_PULLBACK_MAX_BELOW_MA200_PCT", cast=float, default=8.0)
    regime_slope_min_pct: float = _cfg("BRAIN_REGIME_SLOPE_MIN_PCT", cast=float, default=0.0)  # min 200-MA slope over the lookback to count as "rising"
    ma_slope_lookback_days: int = _cfg("BRAIN_MA_SLOPE_LOOKBACK_DAYS", cast=int, default=21)   # bars back to measure the 200-MA slope

    # ── Entry-timing pillar (pullback measured in ATR units) ───────────────────
    e_pullback_peak_low: float = _cfg("BRAIN_E_PULLBACK_PEAK_LOW", cast=float, default=1.0)    # ATRs: start of sweet spot
    e_pullback_peak_high: float = _cfg("BRAIN_E_PULLBACK_PEAK_HIGH", cast=float, default=3.0)  # ATRs: end of sweet spot
    e_pullback_max: float = _cfg("BRAIN_E_PULLBACK_MAX", cast=float, default=5.0)              # ATRs: broken beyond this
    e_ma_proximity_pct: float = _cfg("BRAIN_E_MA_PROXIMITY_PCT", cast=float, default=8.0)      # near rising 50-MA within %
    e_rsi_high: float = _cfg("BRAIN_E_RSI_HIGH", cast=float, default=55.0)                     # uptrend: full entry score at/below this RSI, fades above
    e_rsi_overbought: float = _cfg("BRAIN_E_RSI_OVERBOUGHT", cast=float, default=70.0)         # uptrend: entry score hits 0 here (too extended to buy)
    e_rsi_oversold: float = _cfg("BRAIN_E_RSI_OVERSOLD", cast=float, default=35.0)             # downtrend: entry score peaks near this deep-oversold RSI (capitulation)

    # ── Revisions pillar (soft, best-effort — never forces a sell) ─────────────
    r_norm: float = _cfg("BRAIN_R_NORM", cast=float, default=3.0)  # net revisions → ±1 scale

    # ── Exit stack ─────────────────────────────────────────────────────────────
    exit_rev_decline: float = _cfg("BRAIN_EXIT_REV_DECLINE", cast=float, default=-0.10)  # rev growth below → thesis broken
    exit_gm_collapse: float = _cfg("BRAIN_EXIT_GM_COLLAPSE", cast=float, default=0.0)    # gross margin below → thesis broken
    trail_atr_mult: float = _cfg("BRAIN_TRAIL_ATR_MULT", cast=float, default=3.0)        # Chandelier k
    trim_gain_pct: float = _cfg("BRAIN_TRIM_GAIN_PCT", cast=float, default=0.50)         # parabolic: min unrealized gain
    trim_rsi: float = _cfg("BRAIN_TRIM_RSI", cast=float, default=70.0)                   # parabolic: overbought
    trim_ma_ext_atr_mult: float = _cfg("BRAIN_TRIM_MA_EXT_ATR_MULT", cast=float, default=4.0)  # parabolic: ATRs above 50-MA
    trim_fraction: float = _cfg("BRAIN_TRIM_FRACTION", cast=float, default=0.3333)       # trim ~⅓, keep runner

    # ── Data-layer technical windows ───────────────────────────────────────────
    atr_period: int = _cfg("BRAIN_ATR_PERIOD", cast=int, default=14)
    swing_high_lookback: int = _cfg("BRAIN_SWING_HIGH_LOOKBACK", cast=int, default=22)  # Chandelier/pullback high window
    mom_lookback_days: int = _cfg("BRAIN_MOM_LOOKBACK_DAYS", cast=int, default=252)     # 12-month
    mom_skip_days: int = _cfg("BRAIN_MOM_SKIP_DAYS", cast=int, default=21)              # skip most-recent month (12-1)


# ── Robinhood live portfolio sync ──────────────────────────────────────────────
@dataclass
class RobinhoodConfig:
    account_number: str = _cfg("ROBINHOOD_ACCOUNT_NUMBER", default="")  # primary account (read-only)
    token_key: str = _cfg("ROBINHOOD_TOKEN_KEY", default="")            # base64 AES-256 key for tokens.enc
    tokens_file: str = _cfg("ROBINHOOD_TOKENS_FILE", default="tokens.enc")


# ── Autotrader: autonomous agentic trading ───────────────────────────
# Real-money autonomous execution in a dedicated Robinhood Agentic account, mirroring the
# paper track and filtered by code-enforced guardrails. Master switch defaults False — no
# real order is placed until this is explicitly enabled AND guardrail tests pass. See
# docs/autonomy.md.
@dataclass
class AgentConfig:
    # Robust truthy parse — an unset GitHub secret injects "" (empty), which would crash a
    # strict bool cast; empty/unset/anything-not-truthy means disabled.
    trading_enabled: bool = _cfg("AGENT_TRADING_ENABLED", default="false").strip().lower() in ("true", "1", "yes", "on")
    account_number: str = _cfg("AGENT_ACCOUNT_NUMBER", default="")            # agentic_allowed=true account
    max_trades_per_day: int = _cfg("AGENT_MAX_TRADES_PER_DAY", cast=int, default=6)  # cap on orders placed per day
    min_hold_days: int = _cfg("AGENT_MIN_HOLD_DAYS", cast=int, default=2)     # trading days a lot must be held before a sell is allowed
    drawdown_halt_pct: float = _cfg("AGENT_DRAWDOWN_HALT_PCT", cast=float, default=-15.0)  # halt if net trading P&L draws down past this % of net contributed capital (negative; deposit/withdrawal-immune)
    # Sizing + pacing for the small cash sleeve. The brain sets each name's conviction weight;
    # the sleeve deploys a capped slice of remaining cash per run, split in proportion to weight,
    # and never below the reserve cushion — dry powder isn't idle (Robinhood Gold pays ~3.5% APY).
    reserve_floor_pct: float = _cfg("AGENT_RESERVE_FLOOR_PCT", cast=float, default=0.30)      # keep ≥ this fraction of sleeve equity as cash
    max_deploy_per_run_pct: float = _cfg("AGENT_MAX_DEPLOY_PER_RUN_PCT", cast=float, default=0.25)  # deploy ≤ this fraction of remaining cash per run
    # Cash account: T+1 settlement + Good-Faith-Violation aware (block selling unsettled / same-day round-trips)
    account_is_cash: bool = _cfg("AGENT_ACCOUNT_IS_CASH", cast=bool, default=True)
    # Robinhood Agentic MCP (official execution endpoint) + its own encrypted OAuth token store
    rh_mcp_url: str = _cfg("AGENT_RH_MCP_URL", default="https://agent.robinhood.com/mcp/trading")
    rh_token_key: str = _cfg("AGENT_RH_TOKEN_KEY", default="")                # base64 AES-256 key for the agentic OAuth blob
    rh_tokens_file: str = _cfg("AGENT_RH_TOKENS_FILE", default="agent_tokens.enc")


# ── Exploration (weekly discovery run) ─────────────────────────────────────────
@dataclass
class ExplorationConfig:
    model: str = _cfg("EXPLORATION_MODEL", default="claude-opus-4-8")  # weekly discovery model
    max_tokens: int = _cfg("EXPLORATION_MAX_TOKENS", cast=int, default=8000)  # response token cap
    effort: str = _cfg("EXPLORATION_EFFORT", default="medium")  # output_config.effort: low|medium|high|max
    max_searches: int = _cfg("EXPLORATION_MAX_SEARCHES", cast=int, default=2)  # web searches per theme
    max_av_calls: int = _cfg("EXPLORATION_MAX_AV_CALLS", cast=int, default=8)    # Alpha Vantage calls per run
    max_fmp_calls: int = _cfg("EXPLORATION_MAX_FMP_CALLS", cast=int, default=15)  # FMP calls per run


# ── Claude API call timeouts ───────────────────────────────────────────────────
# Wall-clock backstop for the whole client-side tool loop. Each tool call is bounded by
# tool_loop.advisor_tool_timeout, so these should now rarely fire.
@dataclass
class TimeoutsConfig:
    advisor: int = _cfg("ADVISOR_TIMEOUT", cast=int, default=300)          # seconds — 5 min backstop
    exploration: int = _cfg("EXPLORATION_TIMEOUT", cast=int, default=300)  # seconds — 5 min backstop


# ── Notifications (Telegram) ───────────────────────────────────────────────────
# Bot token from @BotFather; chat id of the destination chat. Both unset = no-op.
@dataclass
class TelegramConfig:
    bot_token: str = _cfg("TELEGRAM_BOT_TOKEN", default="")
    chat_id: str = _cfg("TELEGRAM_CHAT_ID", default="")


# ── Module-level singletons ────────────
supabase = SupabaseConfig()
services = ServicesConfig()
reasoning = ReasoningConfig()
tool_loop = ToolLoopConfig()
paper = PaperConfig()
market_data = MarketDataConfig()
rules = RulesConfig()
quality = QualityConfig()
tech = TechConfig()
brain = BrainConfig()
robinhood = RobinhoodConfig()
agent = AgentConfig()
exploration = ExplorationConfig()
timeouts = TimeoutsConfig()
telegram = TelegramConfig()
