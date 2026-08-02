"""
The settings registry: what every config field MEANS, who may change it, and how far.

`src/config.py` stays the single source of truth for *values*. This module is the single
source of truth for *metadata* — the tier, the plain-language explanation, and the bounds
that both the dashboard UI and the server-side override applier enforce.

Why a separate module rather than `field(metadata=...)` inline: config.py stays a lean,
scannable list of values, and all the human-facing prose lives in one table designed for it.
Drift is impossible — `tests/test_settings.py` asserts the registry and the dataclasses
cover exactly the same fields, so a new knob fails the suite until it is classified here.

Tiers
  0  SECRET     credentials + endpoints. Never rendered, never stored in the DB, env only.
  1  DIAL       the operating knobs worth watching while judging performance. Editable
                from the dashboard (unless `locked`), overridable from the DB.
  2  SHAPE      model-shape parameters — the scoring ramps. Editable behind "advanced".
  3  PLUMBING   timeouts, worker counts, model ids, lookback windows. Not user-facing,
                not overridable; they are plain constants in config.py.

`locked` marks the hard risk rules (agent rules 2 & 4, the drawdown halt). They are shown
so you can see what is enforced, but changing them requires a code edit + commit, which
keeps the guardrails in git history rather than in a text box.

Copy rules for `help` / `impact`: written for a non-finance reader. No jargon, no acronyms
without explanation, describe what ASTRA does rather than naming the technique.
"""

from __future__ import annotations

from dataclasses import dataclass

SECRET, DIAL, SHAPE, PLUMBING = 0, 1, 2, 3


@dataclass(frozen=True)
class Knob:
    """One config field's metadata. `key` is 'section.field' as it appears in config.py."""

    key: str
    tier: int
    label: str = ""
    help: str = ""
    impact: str = ""
    group: str = ""
    unit: str = ""            # fraction | pct | score | days | count | usd | multiple | atr | ""
    min: float | None = None
    max: float | None = None
    step: float | None = None
    locked: bool = False      # visible in the UI, but only changeable via a code commit

    @property
    def section(self) -> str:
        return self.key.split(".", 1)[0]

    @property
    def field(self) -> str:
        return self.key.split(".", 1)[1]

    @property
    def editable(self) -> bool:
        """Whether the dashboard may write an override for this field."""
        return self.tier in (DIAL, SHAPE) and not self.locked


def _secret(*keys: str) -> list[Knob]:
    return [Knob(key=k, tier=SECRET) for k in keys]


def _plumbing(*keys: str) -> list[Knob]:
    return [Knob(key=k, tier=PLUMBING) for k in keys]


# ══════════════════════════════════════════════════════════════════════════════
# Tier 0 — secrets and endpoints. Env only; excluded from the exported schema.
# ══════════════════════════════════════════════════════════════════════════════
_SECRETS: list[Knob] = _secret(
    "supabase.url", "supabase.service_key", "supabase.anon_key",
    "services.anthropic_api_key", "services.tavily_mcp_url", "services.alpha_vantage_api_key",
    "services.sec_edgar_mcp_url", "services.fmp_api_key", "services.sec_user_agent",
    "robinhood.account_number", "robinhood.token_key", "robinhood.tokens_file",
    "agent.account_number", "agent.rh_mcp_url", "agent.rh_token_key",
    "agent.rh_tokens_file", "agent.rh_token_endpoint",
    "telegram.bot_token", "telegram.chat_id",
)


# ══════════════════════════════════════════════════════════════════════════════
# Tier 1 — the operating dials.
# ══════════════════════════════════════════════════════════════════════════════
_G_GATE = "What ASTRA decides to buy"
_G_SIZE = "How big each purchase is"
_G_DIP = "Buying dips harder"
_G_SELL = "When ASTRA sells"
_G_AUTO = "Autotrader (real money)"
_G_PAPER = "Paper track"
_G_AI = "AI judgment layer"
_G_LIMITS = "Hard limits (code-enforced)"
_G_DATA = "Data sources"
_G_SCORE = "Self-evaluation"

_DIALS: list[Knob] = [
    # ── The buy gate ──────────────────────────────────────────────────────────
    Knob(
        "brain.conviction_primary", DIAL, "Belief-led decisions", group=_G_GATE,
        help="When on, ASTRA decides whether to own something using only two things: how "
             "strongly you believe in its industry, and whether the business itself is still "
             "healthy. Price behaviour is then used only to decide how much to buy and when. "
             "When off, it falls back to the older method where price and value also vote on "
             "whether to own it at all.",
        impact="Turning this off reverts to the previous scoring method and changes every buy "
               "decision. Leave it on unless you are deliberately comparing the two.",
    ),
    Knob(
        "brain.cp_buy_threshold", DIAL, "Buy bar", group=_G_GATE, unit="score",
        min=0.30, max=0.95, step=0.01,
        help="Every candidate gets a score from 0 to 1 that multiplies your belief in its "
             "industry by how healthy the business looks. Score this high or above becomes a BUY.",
        impact="Raise it to buy less often and only the strongest names. Lower it to buy more "
               "freely across more of your list.",
    ),
    Knob(
        "brain.cp_watch_threshold", DIAL, "Watchlist bar", group=_G_GATE, unit="score",
        min=0.10, max=0.80, step=0.01,
        help="The same score, but the lower line: at or above this (and below the buy bar) a "
             "name is flagged as worth watching rather than bought.",
        impact="Only affects what shows up as 'watch' on the dashboard. No money moves.",
    ),
    Knob(
        "brain.conviction_very_high", DIAL, "Belief weight: very high", group=_G_GATE,
        unit="score", min=0.0, max=1.0, step=0.05,
        help="The score contribution for a stock in an industry you have marked 'very high' "
             "belief. This is one of the two halves of the buy decision.",
        impact="Lowering this can push your strongest-conviction names below the buy bar.",
    ),
    Knob(
        "brain.conviction_high", DIAL, "Belief weight: high", group=_G_GATE,
        unit="score", min=0.0, max=1.0, step=0.05,
        help="The score contribution for a stock in a 'high' belief industry. Kept below the "
             "'very high' weight so your top convictions always lead.",
        impact="Raising it toward 1.0 flattens the difference between your best and second-best ideas.",
    ),
    Knob(
        "brain.conviction_medium", DIAL, "Belief weight: medium", group=_G_GATE,
        unit="score", min=0.0, max=1.0, step=0.05,
        help="The score contribution for a 'medium' belief industry.",
        impact="At 0.6 a medium-belief name needs a near-perfect business to clear the buy bar.",
    ),
    Knob(
        "brain.conviction_low", DIAL, "Belief weight: low", group=_G_GATE,
        unit="score", min=0.0, max=1.0, step=0.05,
        help="The score contribution for a 'low' belief industry.",
        impact="At 0.4 these effectively never clear the buy bar on their own.",
    ),
    Knob(
        "brain.thesis_neutral", DIAL, "Unknown-business default", group=_G_GATE,
        unit="score", min=0.0, max=1.0, step=0.05,
        help="Some holdings have no company financials to read — funds that hold baskets of "
             "stocks, or foreign listings. This is the business-health score they are given "
             "instead, so your belief in them carries the decision on its own.",
        impact="Raising it makes fund-style holdings easier to buy; lowering it sidelines them.",
    ),

    # ── Sizing ────────────────────────────────────────────────────────────────
    Knob(
        "brain.f_global", DIAL, "Master risk dial", group=_G_SIZE, unit="fraction",
        min=0.02, max=0.30, step=0.01,
        help="The overall volume knob for trade size. Every purchase ASTRA calculates is "
             "scaled by this number before any other adjustment.",
        impact="The single biggest lever here — doubling it roughly doubles every purchase and "
               "how fast your cash gets invested.",
    ),
    Knob(
        "brain.size_min_pct", DIAL, "Skip-tiny-trades floor", group=_G_SIZE, unit="fraction",
        min=0.0, max=0.10, step=0.005,
        help="If a calculated purchase would come out smaller than this share of the account, "
             "ASTRA skips it rather than placing a trivially small order.",
        impact="Raise it to cut down on small dribbling buys; too high and weaker signals never trade.",
    ),
    Knob(
        "brain.max_new_deploy_pct", DIAL, "New-money cap per run", group=_G_SIZE, unit="fraction",
        min=0.05, max=1.0, step=0.05,
        help="The most of the account ASTRA will commit to brand-new purchases in a single "
             "day's run, added across all names.",
        impact="A brake on going all-in on one dramatic day. Lower spreads entries out over time.",
    ),
    Knob(
        "brain.vol_ref", DIAL, "Normal jumpiness benchmark", group=_G_SIZE, unit="fraction",
        min=0.01, max=0.10, step=0.005,
        help="ASTRA buys less of a stock that swings wildly day to day and more of a steady "
             "one. This is the 'typical daily swing' it compares against — a stock that moves "
             "about this much on an average day gets a normal-sized purchase.",
        impact="Lowering it treats more stocks as jumpy, shrinking most purchases.",
    ),
    Knob(
        "brain.vol_scalar_min", DIAL, "Most a wild stock is shrunk", group=_G_SIZE,
        unit="multiple", min=0.1, max=1.0, step=0.05,
        help="The floor on that adjustment — even the wildest stock still gets at least this "
             "fraction of a normal purchase.",
        impact="Lower it to be harsher on volatile names.",
    ),
    Knob(
        "brain.vol_scalar_max", DIAL, "Most a calm stock is boosted", group=_G_SIZE,
        unit="multiple", min=1.0, max=3.0, step=0.05,
        help="The ceiling on that adjustment — the steadiest stock gets at most this many "
             "times a normal purchase.",
        impact="Raise it to lean harder into low-volatility names.",
    ),

    # ── Dip-buying ────────────────────────────────────────────────────────────
    Knob(
        "brain.size_timing_min", DIAL, "Smallest lot (at highs)", group=_G_DIP,
        unit="multiple", min=0.1, max=1.0, step=0.05,
        help="ASTRA deliberately buys more when a stock is cheap relative to its recent peak "
             "and less when it is near its high. This is the multiplier applied when there is "
             "no discount at all.",
        impact="Lower it to almost refuse to buy anything trading at its highs.",
    ),
    Knob(
        "brain.size_timing_max", DIAL, "Biggest lot (deep dip)", group=_G_DIP,
        unit="multiple", min=1.0, max=4.0, step=0.1,
        help="The other end of that scale: the multiplier when a stock is at a full discount. "
             "This is the 'back up the truck' setting.",
        impact="The main knob for how aggressively you buy falling prices.",
    ),
    Knob(
        "brain.discount_full_pct", DIAL, "What counts as fully on sale", group=_G_DIP,
        unit="pct", min=10.0, max=70.0, step=1.0,
        help="How far below its highest price of the past year a stock has to fall before "
             "ASTRA treats it as maximally discounted and applies the biggest lot multiplier.",
        impact="Lower it and ordinary dips already trigger maximum buying.",
    ),
    Knob(
        "brain.market_overlay_max", DIAL, "Market-crash boost", group=_G_DIP,
        unit="multiple", min=1.0, max=2.0, step=0.05,
        help="An extra multiplier applied to every purchase when the whole stock market is "
             "down, not just your stock. Buying into broad panic rather than a company problem.",
        impact="At 1.0 the market's own condition stops affecting purchase size entirely.",
    ),
    Knob(
        "brain.market_overlay_full_dd_pct", DIAL, "What counts as a full crash", group=_G_DIP,
        unit="pct", min=5.0, max=40.0, step=1.0,
        help="How far the overall market has to fall from its high before that boost reaches "
             "its maximum.",
        impact="Lower it and routine market wobbles get treated as crashes.",
    ),

    # ── Selling ───────────────────────────────────────────────────────────────
    Knob(
        "brain.trail_atr_mult", DIAL, "How much rope before selling", group=_G_SELL,
        unit="atr", min=1.0, max=8.0, step=0.5,
        help="ASTRA sells a holding if it slides too far from its recent peak. 'Too far' is "
             "measured in multiples of that stock's own typical daily move, so a jumpy stock "
             "is given more room than a steady one.",
        impact="Bigger means more patience through normal swings but deeper losses when a "
               "decline is real. Smaller sells out quickly and more often.",
    ),
    Knob(
        "brain.trim_gain_pct", DIAL, "Profit needed before taking some off", group=_G_SELL,
        unit="fraction", min=0.1, max=2.0, step=0.05,
        help="A winner has to be up at least this much before ASTRA will consider selling part "
             "of it. It never sells the whole position for being up.",
        impact="Lower it to bank profits earlier and more often.",
    ),
    Knob(
        "brain.trim_fraction", DIAL, "How much to sell when trimming", group=_G_SELL,
        unit="fraction", min=0.1, max=0.9, step=0.05,
        help="When a holding spikes far above its normal range, ASTRA sells this share of it "
             "and lets the rest keep running.",
        impact="Higher banks more cash but leaves less on the table if the run continues.",
    ),
    Knob(
        "brain.trim_rsi", DIAL, "Overheated line", group=_G_SELL, unit="score",
        min=50.0, max=90.0, step=1.0,
        help="One of two conditions for trimming a spike. This is a standard 0–100 momentum "
             "gauge — above roughly 70 means a stock has risen unusually fast recently.",
        impact="Raise it to only trim truly extreme run-ups.",
    ),
    Knob(
        "brain.trim_ma_ext_atr_mult", DIAL, "How stretched before trimming", group=_G_SELL,
        unit="atr", min=1.0, max=8.0, step=0.5,
        help="The second trim condition: how far above its recent average price a stock has "
             "stretched, measured in its own typical daily moves.",
        impact="Higher means only the most extreme spikes get trimmed.",
    ),
    Knob(
        "brain.exit_rev_decline", DIAL, "Broken business: shrinking sales", group=_G_SELL,
        unit="fraction", min=-0.50, max=0.0, step=0.01,
        help="If a company's sales shrink by more than this compared to a year ago, ASTRA "
             "treats the reason for owning it as broken and sells regardless of your belief "
             "in the industry. This is the only thing that can override conviction.",
        impact="The safety valve against holding a genuinely deteriorating company. Loosening "
               "it means ASTRA rides declines further down.",
    ),
    Knob(
        "brain.exit_gm_collapse", DIAL, "Broken business: losing money per sale", group=_G_SELL,
        unit="fraction", min=-0.50, max=0.30, step=0.01,
        help="The other break condition: profit left on each dollar of sales falling below "
             "this. At 0 it means the company is losing money on every sale it makes.",
        impact="Raising it above 0 sells companies with thin but positive margins.",
    ),

    # ── Autotrader ────────────────────────────────────────────────────────────
    Knob(
        "agent.trading_enabled", DIAL, "Real-money trading", group=_G_AUTO,
        help="The master switch for placing real orders in the Robinhood agentic account. "
             "Off means ASTRA still decides and paper-trades everything, but places nothing.",
        impact="Turning this off stops all real trading immediately. Safety switch: if either "
               "this or the environment setting says off, trading is off.",
    ),
    Knob(
        "agent.max_trades_per_day", DIAL, "Daily order cap", group=_G_AUTO, unit="count",
        min=1, max=30, step=1,
        help="A hard ceiling on how many orders the Autotrader may place in one day. Purely a "
             "brake against a runaway loop, not a strategy setting.",
        impact="Too low and legitimate buys get silently dropped on busy days.",
    ),
    Knob(
        "agent.min_hold_days", DIAL, "Minimum holding period", group=_G_AUTO, unit="days",
        min=0, max=30, step=1,
        help="Trading days a newly bought stock must be held before it can be sold. The "
             "account settles trades a day late, so quick round-trips get penalised.",
        impact="Lower risks account restrictions; higher delays legitimate exits.",
    ),
    Knob(
        "agent.reserve_floor_pct", DIAL, "Cash cushion", group=_G_AUTO, unit="fraction",
        min=0.0, max=0.90, step=0.05,
        help="The share of the Autotrader account that stays as cash and never gets invested. "
             "Idle cash is not wasted — the account earns interest on it.",
        impact="Higher keeps more dry powder for a crash but invests less overall.",
    ),
    Knob(
        "agent.max_deploy_per_run_pct", DIAL, "Spend limit per run", group=_G_AUTO,
        unit="fraction", min=0.05, max=1.0, step=0.05,
        help="The most of its remaining cash the Autotrader will spend in a single day's run. "
             "Paces buying out over weeks instead of emptying the account at once.",
        impact="Higher invests faster and concentrates your entry into fewer days.",
    ),
    Knob(
        "agent.min_position_usd", DIAL, "Smallest worthwhile position", group=_G_AUTO,
        unit="usd", min=5, max=500, step=5,
        help="The smallest dollar amount the Autotrader will put into any one stock. It buys "
             "fewer, properly-sized positions instead of splitting a small budget into many "
             "tiny ones. Anything you already own that sits below this gets topped up first — "
             "nothing is ever sold to reach it.",
        impact="Higher means fewer but more meaningful holdings; lower spreads the same money "
               "across more names and risks positions too small to matter.",
    ),

    # ── Paper track ───────────────────────────────────────────────────────────
    Knob(
        "paper.portfolio_size", DIAL, "Fallback account size", group=_G_PAPER, unit="usd",
        min=1000, max=1_000_000, step=1000,
        help="Only used for sizing the paper track when no real account balance has been read "
             "yet. Normally your actual Robinhood balance is used instead.",
        impact="Affects paper trade sizes only when the live balance is unavailable.",
    ),
    Knob(
        "paper.max_adds_per_ticker", DIAL, "Extra top-ups per stock", group=_G_PAPER,
        unit="count", min=0, max=10, step=1,
        help="After the first purchase of a name, how many additional times ASTRA may add to "
             "it while the buy signal persists. Zero means one purchase and no more.",
        impact="Higher builds bigger positions in names that keep looking good.",
    ),
    Knob(
        "paper.add_cooldown_days", DIAL, "Wait between top-ups", group=_G_PAPER, unit="days",
        min=0, max=60, step=1,
        help="Minimum days between adding to the same stock, so a signal that fires every "
             "single day does not buy every single day.",
        impact="Shorter builds positions faster; longer spreads your average price out.",
    ),

    # ── AI judgment layer ─────────────────────────────────────────────────────
    Knob(
        "brain.contextual_enabled", DIAL, "AI judgment layer", group=_G_AI,
        help="When on, an AI pass reads your Playbook of strategies and may suggest bounded "
             "adjustments — buy a bit more of this, nudge belief slightly. It never makes "
             "decisions; every suggestion is capped and re-checked by the rules below.",
        impact="Turning it off falls back to purely mechanical decisions. Nothing else changes.",
    ),
    Knob(
        "brain.contextual_size_mult_min", DIAL, "Most the AI may shrink a buy", group=_G_AI,
        unit="multiple", min=0.1, max=1.0, step=0.05,
        help="A hard floor enforced in code on any purchase-size change the AI proposes, no "
             "matter what it asks for.",
        impact="Closer to 1.0 gives the AI less room to shrink positions.",
    ),
    Knob(
        "brain.contextual_size_mult_max", DIAL, "Most the AI may grow a buy", group=_G_AI,
        unit="multiple", min=1.0, max=4.0, step=0.05,
        help="The matching hard ceiling on any purchase-size increase the AI proposes.",
        impact="The main limit on how much influence the AI has over trade size.",
    ),
    Knob(
        "brain.contextual_nudge_cap", DIAL, "Most the AI may shift belief", group=_G_AI,
        unit="score", min=0.0, max=0.30, step=0.01,
        help="The AI may nudge a stock's belief score slightly up or down, which can push a "
             "borderline name across the buy bar. This caps how far, in either direction.",
        impact="Set to 0 to forbid the AI from affecting the buy decision at all — it could "
               "then only change sizing.",
    ),

    # ── Data sources ──────────────────────────────────────────────────────────
    Knob(
        "market_data.sec_fundamentals_enabled", DIAL, "Use official filings for small caps",
        group=_G_DATA,
        help="Pulls company financials straight from official regulatory filings and layers "
             "them over the free market-data feed, which reports poorly on smaller companies.",
        impact="Turning it off makes business-health scores for small companies less reliable.",
    ),

    # ── Hard limits (visible, locked) ─────────────────────────────────────────
    Knob(
        "rules.max_position_pct", DIAL, "Max in one stock", group=_G_LIMITS, unit="fraction",
        locked=True,
        help="No single company may exceed this share of the account. Enforced in code on "
             "every purchase.",
        impact="Locked. Changing it requires a code change so the limit stays in version history.",
    ),
    Knob(
        "rules.max_theme_pct", DIAL, "Max in one speculative theme", group=_G_LIMITS,
        unit="fraction", locked=True,
        help="No single speculative theme (space names, for example) may exceed this share of "
             "the account. Your core technology holdings are exempt.",
        impact="Locked. Changing it requires a code change.",
    ),
    Knob(
        "rules.theme_cap_exempt", DIAL, "Theme exempt from that cap", group=_G_LIMITS,
        locked=True,
        help="The one theme allowed to exceed the speculative cap, because it is your core "
             "long-term allocation rather than a speculation.",
        impact="Locked. Changing it requires a code change.",
    ),
    Knob(
        "rules.averaging_down_drawdown", DIAL, "Losing-position buy block", group=_G_LIMITS,
        unit="fraction", locked=True,
        help="Once a holding is down this much from what you paid, ASTRA stops buying more of "
             "it. Prevents throwing money at a losing position.",
        impact="Locked. Changing it requires a code change.",
    ),
    Knob(
        "rules.averaging_down_max_buys", DIAL, "Buys allowed while down", group=_G_LIMITS,
        unit="count", locked=True,
        help="How many additional purchases are allowed on a losing position before the block "
             "above kicks in.",
        impact="Locked. Changing it requires a code change.",
    ),
    Knob(
        "agent.drawdown_halt_pct", DIAL, "Emergency stop", group=_G_LIMITS, unit="pct",
        locked=True,
        help="If the Autotrader's total gain or loss falls this far below the money you have "
             "put into it, it stops trading entirely until you step in. Deposits and "
             "withdrawals do not trigger it.",
        impact="Locked. Changing it requires a code change so the circuit breaker stays in "
               "version history.",
    ),

    # ── Self-evaluation ───────────────────────────────────────────────────────
    Knob(
        "scorecard.enabled", DIAL, "Grade my own record", group=_G_SCORE,
        help="When on, at the end of each day ASTRA grades its own track record: how its "
             "results compare with simply owning the S&P 500, and whether the companies it "
             "picked actually did better than the market. It only reads past history — it "
             "never changes a setting and never places a trade.",
        impact="Turning this off stops the grading and the alerts. Nothing about how ASTRA "
               "trades changes either way.",
    ),
    Knob(
        "scorecard.notify_enabled", DIAL, "Tell me when the grade changes", group=_G_SCORE,
        help="Sends a message only when the conclusion actually changes — for example from "
             "'not enough history yet' to 'doing well, consider adding funds'. A day where "
             "nothing changed stays silent.",
        impact="Off means the grade is still worked out and recorded, just never pushed to "
               "your phone.",
    ),
]


# ══════════════════════════════════════════════════════════════════════════════
# Tier 2 — model shape. Editable behind the "advanced" disclosure.
# ══════════════════════════════════════════════════════════════════════════════
_G_PILLARS = "Scoring mix (legacy method)"
_G_QUALITY = "Business-health scoring"
_G_VALUE = "Cheapness scoring"
_G_TREND = "Price-trend scoring"
_G_ENTRY = "Timing scoring"
_G_REGIME = "Dip vs. real decline"
_G_MACRO = "Market-wide selloff detection"
_G_EXPLORE = "New-name screening"

_SHAPE: list[Knob] = [
    # Legacy composite path (only live with belief-led decisions off)
    Knob("brain.conviction_preferred", SHAPE, "Legacy weight: named holding", group=_G_PILLARS,
         unit="score", min=0.0, max=1.0, step=0.05,
         help="Used only when belief-led decisions are turned off.",
         impact="No effect under the current settings."),
    Knob("brain.conviction_approved", SHAPE, "Legacy weight: approved theme", group=_G_PILLARS,
         unit="score", min=0.0, max=1.0, step=0.05,
         help="Used only when belief-led decisions are turned off.",
         impact="No effect under the current settings."),
    Knob("brain.conviction_hold", SHAPE, "Weight: hold-only names", group=_G_PILLARS,
         unit="score", min=0.0, max=1.0, step=0.05,
         help="The score weight for names you have marked hold-only — keep, but do not buy more.",
         impact="Keeps hold-only names from clearing the buy bar."),
    Knob("brain.buy_threshold", SHAPE, "Legacy buy bar", group=_G_PILLARS, unit="score",
         min=0.1, max=0.9, step=0.01,
         help="The buy line for the older scoring method. Only used with belief-led decisions off.",
         impact="No effect under the current settings."),
    Knob("brain.watch_threshold", SHAPE, "Legacy watch bar", group=_G_PILLARS, unit="score",
         min=0.1, max=0.9, step=0.01,
         help="The watch line for the older scoring method.",
         impact="No effect under the current settings."),
    Knob("brain.weight_quality", SHAPE, "Mix: business health", group=_G_PILLARS,
         unit="fraction", min=0.0, max=1.0, step=0.05,
         help="How much business health counts in the older combined score. The five mix "
              "weights should add up to 1.",
         impact="Only affects the legacy method and the displayed sub-scores."),
    Knob("brain.weight_valuation", SHAPE, "Mix: cheapness", group=_G_PILLARS,
         unit="fraction", min=0.0, max=1.0, step=0.05,
         help="How much price-versus-value counts in the older combined score.",
         impact="Only affects the legacy method and the displayed sub-scores."),
    Knob("brain.weight_trend", SHAPE, "Mix: price trend", group=_G_PILLARS,
         unit="fraction", min=0.0, max=1.0, step=0.05,
         help="How much the direction of the price counts in the older combined score.",
         impact="Only affects the legacy method and the displayed sub-scores."),
    Knob("brain.weight_entry", SHAPE, "Mix: timing", group=_G_PILLARS,
         unit="fraction", min=0.0, max=1.0, step=0.05,
         help="How much entry timing counts in the older combined score.",
         impact="Only affects the legacy method and the displayed sub-scores."),
    Knob("brain.weight_revisions", SHAPE, "Mix: analyst updates", group=_G_PILLARS,
         unit="fraction", min=0.0, max=1.0, step=0.05,
         help="How much recent analyst opinion changes count in the older combined score.",
         impact="Only affects the legacy method and the displayed sub-scores."),

    # Business-health ramps
    Knob("brain.q_rev_low", SHAPE, "Sales growth: zero-score point", group=_G_QUALITY,
         unit="fraction", min=-0.5, max=0.5, step=0.01,
         help="Yearly sales growth at or below this scores 0 for the growth check.",
         impact="Raising it is stricter about growth."),
    Knob("brain.q_rev_high", SHAPE, "Sales growth: full-score point", group=_G_QUALITY,
         unit="fraction", min=0.0, max=1.5, step=0.05,
         help="Yearly sales growth at or above this scores full marks.",
         impact="Lowering it makes moderate growers look excellent."),
    Knob("brain.q_gm_low", SHAPE, "Profit per sale: zero-score point", group=_G_QUALITY,
         unit="fraction", min=0.0, max=1.0, step=0.05,
         help="How much profit is left on each dollar of sales, before overheads. At or below "
              "this scores 0.",
         impact="Raising it penalises low-margin businesses harder."),
    Knob("brain.q_gm_high", SHAPE, "Profit per sale: full-score point", group=_G_QUALITY,
         unit="fraction", min=0.0, max=1.0, step=0.05,
         help="Profit per dollar of sales at or above this scores full marks.",
         impact="Lowering it flatters thinner-margin businesses."),
    Knob("brain.q_de_good", SHAPE, "Borrowing: healthy level", group=_G_QUALITY, unit="pct",
         min=0.0, max=400.0, step=10.0,
         help="How much a company has borrowed compared to what its owners have put in, as a "
              "percentage. At or below this scores full marks.",
         impact="Lowering it is stricter about debt."),
    Knob("brain.q_de_bad", SHAPE, "Borrowing: danger level", group=_G_QUALITY, unit="pct",
         min=0.0, max=1000.0, step=10.0,
         help="Borrowing at or above this level scores 0 on the debt check.",
         impact="Raising it tolerates far more leveraged companies."),
    Knob("brain.q_cr_low", SHAPE, "Short-term cash: zero-score point", group=_G_QUALITY,
         unit="multiple", min=0.0, max=5.0, step=0.1,
         help="Whether a company can cover the bills due within a year from what it can turn "
              "into cash within a year. Below 1 means it cannot.",
         impact="Raising it demands a bigger safety margin."),
    Knob("brain.q_cr_high", SHAPE, "Short-term cash: full-score point", group=_G_QUALITY,
         unit="multiple", min=0.0, max=5.0, step=0.1,
         help="At or above this ratio the company scores full marks on covering near-term bills.",
         impact="Lowering it is more forgiving."),
    Knob("brain.q_catastrophic_cap", SHAPE, "Broken-business score ceiling", group=_G_QUALITY,
         unit="score", min=0.0, max=1.0, step=0.05,
         help="If sales are shrinking badly or the company loses money on every sale, its "
              "business-health score is forced no higher than this no matter what else looks good.",
         impact="Raising it lets clearly deteriorating companies still clear the buy bar."),

    # Cheapness
    Knob("brain.v_peg_low", SHAPE, "Growth-adjusted price: cheap", group=_G_VALUE,
         unit="multiple", min=0.0, max=5.0, step=0.1,
         help="Price compared to earnings, then adjusted for how fast the company is growing. "
              "At or below this counts as cheap.",
         impact="Only affects the cheapness sub-score."),
    Knob("brain.v_peg_high", SHAPE, "Growth-adjusted price: expensive", group=_G_VALUE,
         unit="multiple", min=0.0, max=10.0, step=0.1,
         help="At or above this the same measure counts as expensive and scores 0.",
         impact="Only affects the cheapness sub-score."),
    Knob("brain.v_pe_low", SHAPE, "Price vs. expected earnings: cheap", group=_G_VALUE,
         unit="multiple", min=0.0, max=60.0, step=1.0,
         help="How many years of expected profit the share price represents. At or below this "
              "counts as cheap.",
         impact="Only affects the cheapness sub-score."),
    Knob("brain.v_pe_high", SHAPE, "Price vs. expected earnings: expensive", group=_G_VALUE,
         unit="multiple", min=0.0, max=150.0, step=1.0,
         help="At or above this the same measure counts as expensive and scores 0.",
         impact="Only affects the cheapness sub-score."),
    Knob("brain.v_neutral", SHAPE, "Cheapness score when data is missing", group=_G_VALUE,
         unit="score", min=0.0, max=1.0, step=0.05,
         help="Many holdings have no usable valuation data. They get this middling score "
              "rather than being penalised for it.",
         impact="Moving it away from the middle silently rewards or punishes names with poor data."),

    # Trend
    Knob("brain.t_regime_term", SHAPE, "Trend bonus/penalty size", group=_G_TREND,
         unit="score", min=0.0, max=1.0, step=0.05,
         help="How much being in a clear uptrend adds to the trend score, and being in a "
              "downtrend subtracts.",
         impact="Only affects the trend sub-score and the legacy method."),
    Knob("brain.t_regime_pullback", SHAPE, "Trend score for a normal dip", group=_G_TREND,
         unit="score", min=-1.0, max=1.0, step=0.05,
         help="A shallow dip inside a long-term rise is scored neutrally rather than being "
              "punished like a genuine decline.",
         impact="Raising it treats dips as outright positive."),
    Knob("brain.t_mom_scale", SHAPE, "Past-year momentum sensitivity", group=_G_TREND,
         unit="fraction", min=0.05, max=1.0, step=0.05,
         help="How strongly last year's price performance moves the trend score.",
         impact="Smaller makes the score react more sharply to modest moves."),
    Knob("brain.t_ma50_scale", SHAPE, "Distance-from-average sensitivity", group=_G_TREND,
         unit="pct", min=1.0, max=50.0, step=1.0,
         help="How strongly the gap between today's price and its recent average moves the "
              "trend score.",
         impact="Smaller makes the score react more sharply."),

    # Regime classification
    Knob("brain.regime_pullback_max_below_ma200_pct", SHAPE, "Dip depth limit", group=_G_REGIME,
         unit="pct", min=0.0, max=30.0, step=1.0,
         help="How far below its long-term average price a stock can sit and still be called "
              "a temporary dip rather than a real decline.",
         impact="Raising it treats deeper falls as harmless dips, which keeps ASTRA buying them."),
    Knob("brain.regime_slope_min_pct", SHAPE, "Long-term trend must be rising by", group=_G_REGIME,
         unit="pct", min=-10.0, max=10.0, step=0.5,
         help="The second condition for calling a fall a dip: the long-term average price must "
              "still be climbing by at least this much.",
         impact="Raising it demands a clearly rising trend before dips get bought."),

    # Market-wide selloff detection
    Knob("brain.beta_min_r2", SHAPE, "Market-link reliability floor", group=_G_MACRO,
         unit="fraction", min=0.0, max=1.0, step=0.05,
         help="Before claiming a stock fell because the whole market fell, ASTRA checks how "
              "closely that stock normally tracks the market. Below this the relationship is "
              "too weak to trust and the claim is not made.",
         impact="Lowering it lets weakly-correlated names be treated as market-driven falls."),
    Knob("brain.selloff_panic_return_pct", SHAPE, "Market drop that counts as panic",
         group=_G_MACRO, unit="pct", min=-20.0, max=0.0, step=0.5,
         help="How far the overall market must fall over a short window to count as a genuine "
              "broad selloff rather than ordinary noise.",
         impact="Closer to 0 fires the crash-buying plays far more often."),
    Knob("brain.selloff_breadth_min_pct", SHAPE, "How widespread the fall must be",
         group=_G_MACRO, unit="pct", min=0.0, max=100.0, step=5.0,
         help="The share of your tracked stocks that must also be falling, confirming it is a "
              "market-wide event and not a few names dragging the average.",
         impact="Lowering it makes narrow selloffs look like broad panics."),

    # Exploration screening (new names only)
    Knob("quality.min_revenue_growth", SHAPE, "New names: minimum sales growth",
         group=_G_EXPLORE, unit="fraction", min=-0.5, max=1.0, step=0.05,
         help="Screening bar applied only to brand-new candidate companies, not to anything "
              "you already hold.",
         impact="Only affects which new names get suggested."),
    Knob("quality.min_gross_margin", SHAPE, "New names: minimum profit per sale",
         group=_G_EXPLORE, unit="fraction", min=0.0, max=1.0, step=0.05,
         help="Screening bar for new candidates only.",
         impact="Only affects which new names get suggested."),
    Knob("quality.max_debt_equity", SHAPE, "New names: maximum borrowing", group=_G_EXPLORE,
         unit="pct", min=0.0, max=1000.0, step=10.0,
         help="Screening bar for new candidates only.",
         impact="Only affects which new names get suggested."),
    Knob("quality.min_checks_to_pass", SHAPE, "New names: checks that must pass",
         group=_G_EXPLORE, unit="count", min=1, max=5, step=1,
         help="How many of the five screening checks a new candidate must clear to be "
              "considered at all.",
         impact="Raising it makes new-name discovery much stricter."),
    Knob("tech.min_pct_below_52w_high", SHAPE, "New names: dip depth wanted", group=_G_EXPLORE,
         unit="pct", min=0.0, max=80.0, step=1.0,
         help="How far below its yearly high a new candidate should be to count as a dip "
              "opportunity.",
         impact="Only affects which new names get suggested."),
    Knob("tech.max_rsi", SHAPE, "New names: oversold line", group=_G_EXPLORE, unit="score",
         min=0.0, max=100.0, step=1.0,
         help="A 0–100 momentum gauge; below roughly 40 means a stock has fallen unusually "
              "hard recently.",
         impact="Only affects which new names get suggested."),
    Knob("tech.signals_required", SHAPE, "New names: dip signals needed", group=_G_EXPLORE,
         unit="count", min=1, max=2, step=1,
         help="How many of the two dip checks a new candidate must show.",
         impact="Only affects which new names get suggested."),

    # Entry timing — how attractive the moment of purchase looks
    Knob("brain.e_pullback_peak_low", SHAPE, "Sweet spot: start", group=_G_ENTRY, unit="atr",
         min=0.0, max=5.0, step=0.25,
         help="How far a stock should have pulled back from its recent peak for the timing to "
              "look good, measured in that stock's own typical daily moves. Below this it has "
              "barely dipped.",
         impact="Raising it waits for deeper pullbacks before timing scores well."),
    Knob("brain.e_pullback_peak_high", SHAPE, "Sweet spot: end", group=_G_ENTRY, unit="atr",
         min=0.0, max=10.0, step=0.25,
         help="The far edge of that ideal pullback range.",
         impact="Widening the range makes more moments score as good entries."),
    Knob("brain.e_pullback_max", SHAPE, "Too-far-gone point", group=_G_ENTRY, unit="atr",
         min=0.0, max=15.0, step=0.5,
         help="Beyond this the fall is treated as a breakdown rather than a buyable dip, and "
              "the timing score drops to 0.",
         impact="Raising it keeps calling very deep falls buyable."),
    Knob("brain.e_ma_proximity_pct", SHAPE, "Near-the-average window", group=_G_ENTRY,
         unit="pct", min=0.0, max=30.0, step=1.0,
         help="Buying near a stock's rising recent average price scores well. This is how "
              "close counts as near.",
         impact="Widening it rewards a looser definition of a good entry."),
    Knob("brain.e_rsi_high", SHAPE, "Calm-enough line", group=_G_ENTRY, unit="score",
         min=0.0, max=100.0, step=1.0,
         help="Using the 0–100 momentum gauge: in a rising stock, timing scores full marks at "
              "or below this and fades above it.",
         impact="Raising it lets ASTRA buy stocks that have already run up hard."),
    Knob("brain.e_rsi_overbought", SHAPE, "Too-hot line", group=_G_ENTRY, unit="score",
         min=0.0, max=100.0, step=1.0,
         help="Where the timing score hits 0 for a rising stock — it has climbed too fast to "
              "be a good entry.",
         impact="Raising it removes the brake on chasing sharp rallies."),
    Knob("brain.e_rsi_oversold", SHAPE, "Capitulation line", group=_G_ENTRY, unit="score",
         min=0.0, max=100.0, step=1.0,
         help="For a stock in a genuine decline, ASTRA wants real panic before stepping in. "
              "Timing scores best around this deeply-sold level.",
         impact="Raising it accepts shallower falls in a downtrend."),

    # Remaining shape fields with no dedicated group
    Knob("brain.r_norm", SHAPE, "Analyst-update sensitivity", group=_G_TREND, unit="count",
         min=1.0, max=20.0, step=1.0,
         help="How many net analyst upgrades or downgrades it takes to move that sub-score to "
              "its maximum. This signal is advisory only and never forces a sell.",
         impact="Minor — the signal carries little weight."),
    Knob("paper.max_position_pct", SHAPE, "Paper: max in one stock", group=_G_PAPER,
         unit="fraction", min=0.01, max=1.0, step=0.01,
         help="The paper track's own ceiling on a single holding, alongside the code-enforced "
              "hard limit.",
         impact="Only affects paper trade sizing."),
    Knob("paper.default_position_pct", SHAPE, "Paper: default purchase size", group=_G_PAPER,
         unit="fraction", min=0.005, max=0.5, step=0.005,
         help="The fallback purchase size used when no explicit size was calculated.",
         impact="Rarely used — the brain almost always supplies a size."),
    Knob("agent.account_is_cash", SHAPE, "Autotrader is a cash account", group=_G_AUTO,
         help="Cash accounts settle trades a day late and penalise quick round-trips, so extra "
              "checks apply before selling. Turn off only if the account type changes.",
         impact="Turning this off removes those settlement checks."),

    # ── Self-evaluation: how much evidence before ASTRA draws a conclusion ────
    # These decide when ASTRA is allowed to ask for more money, so they sit behind the
    # advanced reveal rather than on the main dial board.
    Knob("scorecard.min_nav_days", SHAPE, "Days of history required", group=_G_SCORE,
         unit="days", min=30, max=500, step=5,
         help="How many days where both ASTRA's own value and the S&P 500's value were "
              "recorded before it will draw any conclusion at all. Below this it reports "
              "'too early to tell' and says what it is still waiting for.",
         impact="Lowering it makes ASTRA reach a verdict sooner on thinner evidence, which at "
                "these sample sizes mostly means reaching the wrong one."),
    Knob("scorecard.min_closed_trips", SHAPE, "Finished trades required", group=_G_SCORE,
         unit="count", min=5, max=200, step=1,
         help="How many positions must have been bought and then fully sold before the grade "
              "counts. A position still held has no result yet.",
         impact="Lowering it lets a handful of lucky or unlucky trades drive the conclusion."),
    Knob("scorecard.min_matured_decisions", SHAPE, "Old-enough calls required", group=_G_SCORE,
         unit="count", min=10, max=500, step=5,
         help="How many past buy calls must be old enough for their result to be visible.",
         impact="Lowering it grades ASTRA on calls that have not had time to play out."),
    Knob("scorecard.min_distinct_tickers", SHAPE, "Different companies required",
         group=_G_SCORE, unit="count", min=3, max=50, step=1,
         help="Three hundred results spread over fourteen companies is really fourteen bets, "
              "not three hundred. This is the minimum number of different companies that must "
              "sit behind those results.",
         impact="Lowering it lets a couple of names masquerade as a large sample."),
    Knob("scorecard.min_decision_weeks", SHAPE, "Different weeks required", group=_G_SCORE,
         unit="count", min=1, max=52, step=1,
         help="How many separate weeks the graded calls must be spread across. Calls made in "
              "one week all faced the same market and are really one bet on that moment.",
         impact="Lowering it lets a single good or bad week decide the verdict."),
    Knob("scorecard.ir_scale_threshold", SHAPE, "Bar for 'doing well'", group=_G_SCORE,
         unit="score", min=0.0, max=3.0, step=0.1,
         help="How far ahead of the market ASTRA must be, measured against how bumpy the ride "
              "was, before it says the record justifies more money. This is only half the "
              "test — the result must also be strong enough that it is unlikely to be luck.",
         impact="Lowering it makes ASTRA ask for funds on a weaker record."),
    Knob("scorecard.ir_review_threshold", SHAPE, "Bar for 'needs attention'", group=_G_SCORE,
         unit="score", min=-3.0, max=0.0, step=0.1,
         help="The same measure in the other direction: how far behind the market ASTRA must "
              "be before it flags that something needs fixing.",
         impact="Raising it toward zero makes ASTRA raise the alarm more readily."),
    Knob("scorecard.pick_excess_scale_pct", SHAPE, "Good pick margin", group=_G_SCORE,
         unit="pct", min=0.0, max=25.0, step=0.5,
         help="How much better than the S&P 500 the average pick must have done, over the "
              "measurement window, to count as genuinely good stock picking.",
         impact="Lowering it credits ASTRA with skill for beating the market by a hair."),
    Knob("scorecard.pick_excess_review_pct", SHAPE, "Bad pick margin", group=_G_SCORE,
         unit="pct", min=-25.0, max=0.0, step=0.5,
         help="How far behind the S&P 500 the average pick must be before ASTRA concludes the "
              "problem is what it chooses to buy, rather than how it buys it. That is the "
              "difference between a settings problem and a rethink of your convictions.",
         impact="Raising it toward zero triggers a strategy rethink on smaller shortfalls."),
    Knob("scorecard.confidence_level", SHAPE, "How sure before it speaks", group=_G_SCORE,
         unit="fraction", min=0.50, max=0.99, step=0.01,
         help="ASTRA only calls a result good or bad when the evidence would hold up this "
              "often. Higher means it stays quiet longer and is right more often when it does "
              "speak up.",
         impact="Lowering it produces confident-sounding verdicts out of pure noise."),
    Knob("scorecard.beta_adjust_benchmark", SHAPE, "Allow for uninvested cash",
         group=_G_SCORE,
         help="Both tracks deliberately hold some cash, so comparing them against a fully "
              "invested S&P 500 makes them look bad in a rising market for a reason that has "
              "nothing to do with skill. When on, the comparison is against holding the same "
              "proportion in the S&P 500 and the rest in cash.",
         impact="Turning this off reverts to the raw comparison, which will read as poor "
                "execution in almost any rising market."),
    Knob("scorecard.pick_window_days", SHAPE, "Pick measurement window", group=_G_SCORE,
         unit="days", min=15, max=180, step=5,
         help="How long after a buy call ASTRA waits before judging whether it was a good one.",
         impact="Must match a window ASTRA actually records results for (currently 30 or 60 "
                "days); any other value finds no data and the grade stays 'too early'."),
]


# ══════════════════════════════════════════════════════════════════════════════
# Tier 3 — plumbing. Plain constants in config.py; hidden and not overridable.
# ══════════════════════════════════════════════════════════════════════════════
_PLUMBING: list[Knob] = _plumbing(
    "reasoning.model", "reasoning.max_tokens", "reasoning.advisor_effort",
    "reasoning.tavily_max_searches", "reasoning.alpha_vantage_max_calls", "reasoning.fmp_max_calls",
    "tool_loop.advisor_tool_timeout", "tool_loop.advisor_max_tool_rounds",
    "tool_loop.mcp_tool_failure_limit",
    "market_data.workers", "market_data.period_days",
    "market_data.fundamentals_max_age_days", "market_data.fundamentals_max_period_age_days",
    "brain.atr_period", "brain.swing_high_lookback", "brain.mom_lookback_days",
    "brain.mom_skip_days", "brain.returns_recent_days", "brain.ma_slope_lookback_days",
    "brain.beta_window_days", "brain.beta_drop_window_days", "brain.selloff_nday",
    "agent.token_refresh_buffer_s", "agent.retry_attempts", "agent.retry_backoff_s",
    "exploration.model", "exploration.max_tokens", "exploration.effort",
    "exploration.max_searches", "exploration.max_av_calls", "exploration.max_fmp_calls",
    "contextual.model", "contextual.max_tokens", "contextual.effort",
    "contextual.max_rounds", "contextual.max_searches",
    "timeouts.advisor", "timeouts.exploration", "timeouts.contextual",
    "scorecard.pick_horizon_tolerance_days", "scorecard.max_join_gap_days",
    "scorecard.bootstrap_samples", "scorecard.bootstrap_block", "scorecard.bootstrap_seed",
    "scorecard.benchmark_symbol", "scorecard.history_days",
)


# ══════════════════════════════════════════════════════════════════════════════
# The registry
# ══════════════════════════════════════════════════════════════════════════════
REGISTRY: tuple[Knob, ...] = tuple(_SECRETS + _DIALS + _SHAPE + _PLUMBING)

BY_KEY: dict[str, Knob] = {k.key: k for k in REGISTRY}

# Group display order for the dashboard (groups not listed sort last, alphabetically).
GROUP_ORDER: tuple[str, ...] = (
    _G_GATE, _G_SIZE, _G_DIP, _G_SELL, _G_AUTO, _G_PAPER, _G_AI, _G_DATA, _G_LIMITS,
    _G_SCORE,
    _G_PILLARS, _G_QUALITY, _G_VALUE, _G_TREND, _G_ENTRY, _G_REGIME, _G_MACRO, _G_EXPLORE,
)


def overridable_keys() -> set[str]:
    """Keys the DB override layer is allowed to touch (tier 1/2, not locked)."""
    return {k.key for k in REGISTRY if k.editable}
