# ASTRA Brain

The decision engine. Given a position + market data + convictions, it emits a
`Signal` (buy / sell / trim / watch / hold / blocked) from **graded factor scores →
conviction-weighted composite → regime-aware decisions**. Every tunable lives in
`src/config.py` (grouped in `params.py`); there are no literals in the logic.

**Philosophy: conviction is the anchor.** ASTRA systematizes Abhi's convictions rather
than replacing his judgment. A name must be in `convictions` to be actionable, and its
conviction tier both gates and *weights* everything downstream. This brain serves the
full-portfolio Advisor track (manual execution) first; the $1k Autotrader mirrors a
subset with its own downstream caps.

## Pipeline

```
position + market_data + convictions
        │
        ▼
  conviction gate ──(not convicted / hold-only / do-not-add)──► blocked / hold
        │
        ▼
  hard_rules.check_hard_rules ──(violation)──► blocked
        │
        ▼
  exit stack (held positions)  ──► sell | trim        (precedence over buy)
        │ (no exit)
        ▼
  entry: Score_buy = C · S ──► buy | watch | hold
        │
        ▼
  sizing.allocate (across all BUYs) ──► final suggested_position_pct
```

## Factor pillars (`factors.py`)

Each pillar is a pure function of `market_data`. **Missing inputs are dropped from a
pillar's internal average, never penalized** — so thin-coverage names (small caps, ADRs
like ICAGY/RKLB/ASTS) degrade gracefully. `smooth(x;a,b)` is a clamped linear ramp
(0 at `a`, 1 at `b`; `a>b` descends).

| Pillar | Range | Formula (endpoints from `config.BRAIN_*`) |
|---|---|---|
| **Quality `Q`** | [0,1] | mean of: rev-growth `smooth(·;0,.30)`, gross-margin `smooth(·;.20,.60)`, D/E `smooth(·;300,100)` (descending), current-ratio `smooth(·;1,1.5)`, FCF>0. Catastrophic (revenue < −10% or negative gross margin) caps `Q` at 0.15. |
| **Valuation `V`** | [0,1] | mean of `1−smooth(PEG;1,3)` and `1−smooth(fwdPE;15,40)`; missing → neutral 0.5. |
| **Trend `T`** | [−1,1] | mean of regime term (±0.5), `tanh(mom_12_1/0.25)`, `tanh(price_vs_ma50%/15)`. |
| **Entry `E`** | [0,1] | mean of pullback-depth `tent(ΔATR;1,3,5)`, 50-MA proximity `1−smooth(\|Δ%\|;0,8)`, RSI zone (uptrend: full ≤55, fade to 70; downtrend: reward oversold). |
| **Revisions `R`** | [−1,1] | `clamp((up−down)/3, −1, 1)` from yfinance EPS revisions (30d); no coverage → dropped. **Rating *levels* are deliberately excluded — evidence shows only *changes* predict returns.** |

### Regime (`regime.py`)
Uses only scalar fields (no series): **uptrend** = price > 200-MA *and* 50-MA ≥ 200-MA
(golden-cross proxy for a rising long-term trend); **downtrend** = price < 200-MA; else
neutral. Falls back to the 50-MA when 200-MA history is short.

## Conviction (`conviction.py`)
`C` = weight by tier: preferred 1.0 / approved 0.7 / hold 0.4 / else 0. `can_buy` also
requires the name not be hold-only or do-not-add.

## Entry (`entry.py`)
Composite renormalizes over *available* pillars; trend contributes only its positive part:

```
S         = Σ wᵢ·pillarᵢ / Σ wᵢ      over available pillars  (trend uses max(T,0))
Score_buy = C · S
buy   if can_buy and Score_buy ≥ BUY_THRESHOLD   (0.50)
watch if can_buy and Score_buy ≥ WATCH_THRESHOLD (0.35)
else hold
```
Balanced default weights: Q .25, V .20, T .20, E .20, R .15. In a downtrend `max(T,0)=0`
removes the tailwind, raising the effective bar — only deep, quality-intact capitulation
clears it. This is why buys reappear on healthy pullbacks yet stay disciplined in selloffs.

## Exit stack (`exit.py`)
First match wins. Intent (from convictions) gates the layers:
`thesis_hold` → layer 1 only · `opportunistic` → 1–3 · `written_off` → layer 4.

1. **Thesis invalidation → SELL ALL** on *objective fundamental* breakdown only: revenue
   < −10% YoY or gross-margin collapse. **Analyst revisions never force a sell** — they are
   a soft input to the `R` pillar only (a near-term EPS trim is not a broken multi-year
   thesis for a conviction hold like RKLB). A manual conviction downgrade is likewise a
   **nudge, not an auto-liquidation** — consistent with ASTRA's "flag, don't auto-sell" stance.
2. **Chandelier trailing stop → SELL ALL**: `stop = recent_swing_high − k·ATR` (k=3),
   confirmed by price < 50-MA. The high is a **rolling-window** high (22d), so the stop is
   stateless and entry-date-independent — the textbook Chandelier exit. A winner at new
   highs with an intact trend never trips it.
3. **Parabolic trim → TRIM ⅓, keep a runner**: gain ≥ 50% *and* RSI > 70 *and* price
   extended > 4·ATR above the 50-MA. Only blow-off conditions trim; a calm winner runs.
4. **Written-off nudge**: opportunity-cost risk flag; never forces a sale.

## Sizing (`sizing.py`)
Full-portfolio fractions (the Advisor recommendation Abhi executes by hand):

```
vol_scalar = clip(VOL_REF / (ATR/price), 0.5, 1.5)     higher-vol → smaller
target_w   = clip(F_GLOBAL · Score_buy · vol_scalar, SIZE_MIN 2%, SIZE_MAX 10%)
```
`F_GLOBAL` (0.10) is the **master risk dial** — turn it down to size conservatively.
Then `allocate()` runs an **iterative constrained water-fill** across all simultaneous
BUYs in score order, honoring the single-name (10%), per-theme (15%, `core_tech` exempt),
and total-new-deploy (25%) caps. A BUY that can't fit a minimum position is downgraded to
watch rather than sized to dust.

## Downstream contract
`score.screen_position` returns a `Signal` dict with the fields agent.py / memory / the
executor / the dashboard consume, including `theme`, `score_buy`, `trim_fraction`,
`close_reason`. `strategy.py` re-exports
`get_ticker_guidance`, `is_excluded`, `check_hard_rules`, `compute_portfolio_summary`,
`screen_position` for `agent_guardrails` / `agent_executor` / tests.

**Autotrader mirror:** full exits (`thesis_invalidation`, `trailing_stop`) and buys mirror
into the agentic account. Parabolic **trims are not mirrored** — a trim keeps the paper lot
open, so it never enters the executor's close-based mirror.

## Tuning
Everything is in `src/config.py` under the `BRAIN_*` prefix and env-overridable. Notable
dials: `BRAIN_F_GLOBAL` (risk), `BRAIN_BUY_THRESHOLD`, `BRAIN_TRAIL_ATR_MULT` (trailing
tightness), `BRAIN_WEIGHT_*` (pillar emphasis), `BRAIN_TRIM_*` (parabolic sensitivity).
