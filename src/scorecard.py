"""
ASTRA grading its own track record.

Phase 3 is "scale the sleeve as the track record justifies", but until this module nothing read
the performance series back: `src/performance.py` computes the NAV curve, the dashboard draws it
against SPY, and the loop ended there. The scorecard closes it — read-only. It answers one
question per track, daily, and reaches one of five conclusions:

    INSUFFICIENT_DATA  too little evidence to say anything (and what it is still waiting for)
    SCALE              picks and execution both hold up → the record supports more capital
    TUNE               picks hold up but execution doesn't → the timing/sizing side leaks alpha
    REVIEW             the picks themselves lag → a conviction question, not a settings question
    STEADY             measured, and unremarkable

Two axes, measured separately per track and never blended into one number (docs/attribution.md's
"segment, never pool"):

  • **Execution quality** — the book's NAV against the benchmark over the paired daily series.
  • **Pick quality** — decision-level forward returns against the benchmark over the same window.

Crossing them is what separates a tunable problem from a human one, and the split is not
cosmetic: `docs/attribution.md` forbids machinery touching conviction or the buy gate, so a
"picks are bad" finding *must* route to Abhi rather than to a dial.

Three things make the verdict honest rather than merely confident:

  1. **Cash drag is removed before comparing.** Both books deliberately hold cash (the Autotrader
     keeps a ≥30% reserve floor and runs ~65% invested). Against a fully-invested index that is
     negative excess by construction in any rising market — a market-direction detector wearing a
     skill-measurement costume. The primary statistic compares against the *same invested
     fraction* in the benchmark. Raw excess is computed too, so this never silently disagrees
     with the dashboard chart.
  2. **Nothing acts on a point estimate.** At n=90 an annualized information ratio of 0.5 is
     t≈0.30 (p≈0.76) — thresholding on it alone would recommend funding roughly one time in
     three, at random. Every decisive verdict needs a block-bootstrap confidence interval
     excluding zero AND the point estimate clearing its bar, cross-checked by an exact sign test.
  3. **Sample size is counted in bets, not rows.** 329 outcome rows across 14 tickers and 3
     run-dates is ~14 correlated bets. Pick statistics are averaged within a ticker first, and
     the readiness gate requires ticker and calendar diversity alongside raw volume.

The consequence, accepted deliberately: this reports INSUFFICIENT_DATA for a long time. That is
the correct answer, and saying it explicitly — with the counts and an ETA — is the actual product.
A confident verdict in month four would be a lie.

Layout mirrors `src/performance.py`: pure functions first (no I/O, unit-tested without mocks),
the read/persist layer after. The module writes exactly one table, `scorecard_runs`, and reads
everything else — it must never be able to influence a trade.
"""

from __future__ import annotations

import logging
import math
import random
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

PAPER, AUTOTRADER = "paper", "autotrader"
TRACKS = (PAPER, AUTOTRADER)

INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
SCALE = "SCALE"
TUNE = "TUNE"
REVIEW = "REVIEW"
STEADY = "STEADY"

GOOD, BAD, FLAT = "good", "bad", "flat"

_DAYS_PER_YEAR = 365.25


# ══════════════════════════════════════════════════════════════════════════════
# Pure — shapes
# ══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Pair:
    """One date on which both the book's NAV and the benchmark are known."""

    day: date
    nav: float
    bench: float
    invested_frac: float | None


@dataclass(frozen=True)
class Obs:
    """One return observation spanning the interval between two consecutive `Pair`s.

    Both legs cover the identical interval by construction, so a long weekend is simply a
    longer observation rather than a fabricated day.
    """

    day: date
    nav_ret: float
    bench_ret: float
    gap_days: int
    invested_frac: float | None


@dataclass(frozen=True)
class Pick:
    """One matured buy call whose forward return is known."""

    ticker: str
    decision_id: int | None
    run_day: date
    outcome_day: date
    realized_days: int
    pct_change: float
    bench_pct: float | None = None

    @property
    def excess(self) -> float:
        return self.pct_change - (self.bench_pct or 0.0)


@dataclass(frozen=True)
class ExecutionMetrics:
    n_obs: int = 0
    span_days: int = 0
    dropped_gap: int = 0
    mean_excess: float | None = None
    stdev_excess: float | None = None
    cum_excess_raw: float | None = None
    cum_excess_adj: float | None = None
    ir_annual: float | None = None
    periods_per_year: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    sign_wins: int = 0
    sign_n: int = 0
    sign_p: float | None = None
    mean_invested_frac: float | None = None
    axis: str = FLAT


@dataclass(frozen=True)
class PickMetrics:
    n_picks: int = 0
    n_tickers: int = 0
    n_weeks: int = 0
    dropped_horizon: int = 0
    hit_rate: float | None = None
    hit_rate_p: float | None = None
    mean_excess: float | None = None
    clustered_excess: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    axis: str = FLAT


@dataclass(frozen=True)
class Readiness:
    ok: bool = False
    nav_days: int = 0
    closed_trips: int = 0
    matured_decisions: int = 0
    distinct_tickers: int = 0
    distinct_decision_weeks: int = 0
    missing: list[str] = field(default_factory=list)
    eta_days: int | None = None


# ══════════════════════════════════════════════════════════════════════════════
# Pure — small statistics (no scipy; everything here is exact or seeded)
# ══════════════════════════════════════════════════════════════════════════════

def compound(values: Sequence[float]) -> float:
    """Geometrically chain a series of period returns into one cumulative return."""
    total = 1.0
    for v in values:
        total *= (1.0 + v)
    return total - 1.0


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile of an already-sorted sequence. `q` in [0, 1]."""
    if not sorted_values:
        return float("nan")
    idx = int(round(q * (len(sorted_values) - 1)))
    return sorted_values[min(max(idx, 0), len(sorted_values) - 1)]


def block_bootstrap_ci(
    values: Sequence[float],
    *,
    block: int = 5,
    samples: int = 2000,
    alpha: float = 0.10,
    seed: int = 0,
) -> tuple[float | None, float | None]:
    """Confidence interval on the compounded return of `values`, by moving-block bootstrap.

    Blocks (rather than single observations) are resampled so serial correlation in daily
    excess isn't destroyed — an iid bootstrap would understate the interval. The seed is fixed
    by default because a verdict that changed between two runs on identical data would be
    indefensible; reproducibility is a requirement here, not a convenience.

    Returns (None, None) below two observations, where no interval is meaningful.
    """
    n = len(values)
    if n < 2:
        return None, None
    block = max(1, min(block, n))
    rng = random.Random(seed)
    n_blocks = math.ceil(n / block)
    last_start = n - block
    stats: list[float] = []
    for _ in range(max(1, samples)):
        series: list[float] = []
        for _ in range(n_blocks):
            start = rng.randint(0, last_start)
            series.extend(values[start:start + block])
        stats.append(compound(series[:n]))
    stats.sort()
    return _percentile(stats, alpha / 2.0), _percentile(stats, 1.0 - alpha / 2.0)


def sign_test(values: Sequence[float]) -> tuple[int, int, float | None]:
    """Exact two-sided binomial sign test on how often `values` are positive.

    The robust cross-check on the mean/stdev statistics: at small n with fat tails, one gap-day
    that moved 6% dominates an information ratio but counts for exactly one observation here.
    Zeros are discarded (the standard treatment). Returns (wins, n, p_value).
    """
    nonzero = [v for v in values if v != 0]
    n = len(nonzero)
    wins = sum(1 for v in nonzero if v > 0)
    if n == 0:
        return 0, 0, None
    extreme = max(wins, n - wins)
    tail = sum(math.comb(n, k) for k in range(extreme, n + 1)) / (2 ** n)
    return wins, n, min(1.0, 2.0 * tail)


def information_ratio(excess: Sequence[float], periods_per_year: float | None) -> float | None:
    """Annualized mean excess per unit of its own volatility.

    Reported because it is the number a finance-literate reader will look for, and because it
    is a real part of the decisive test — but it is always ANDed with a confidence interval
    that excludes zero, never used on its own (see the module docstring).
    """
    if len(excess) < 2 or not periods_per_year or periods_per_year <= 0:
        return None
    sd = statistics.stdev(excess)
    if sd == 0:
        return None
    return (statistics.fmean(excess) / sd) * math.sqrt(periods_per_year)


# ══════════════════════════════════════════════════════════════════════════════
# Pure — execution axis
# ══════════════════════════════════════════════════════════════════════════════

def _as_day(value: Any) -> date | None:
    """Calendar day from a timestamp/date in any of the shapes Supabase hands back.

    Deliberately day-level: every calculation here is daily, and slicing the first ten
    characters sidesteps the tz-offset formats ("+00" vs "+00:00") entirely.
    """
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pair_series(
    nav_rows: Iterable[dict],
    bench_by_day: dict[date, float],
    *,
    date_key: str = "snapshot_time",
    nav_key: str = "nav_index",
) -> list[Pair]:
    """Inner-join the book's NAV series against the benchmark, one row per calendar day.

    Nothing is forward-filled: a day the benchmark didn't trade, or the book didn't snapshot,
    simply does not become an observation. Where a day carries several snapshots (a re-run) the
    last wins — carrying both would manufacture a phantom 0% interval.
    """
    by_day: dict[date, dict] = {}
    for row in nav_rows:
        day = _as_day(row.get(date_key))
        nav = _num(row.get(nav_key))
        if day is None or nav is None:
            continue
        by_day[day] = row  # ascending input → last write per day wins

    pairs: list[Pair] = []
    for day in sorted(by_day):
        bench = bench_by_day.get(day)
        if bench is None:
            continue
        row = by_day[day]
        equity = _num(row.get("total_equity"))
        market_value = _num(row.get("market_value"))
        invested = (market_value / equity) if (equity and market_value is not None) else None
        pairs.append(Pair(day=day, nav=_num(row.get(nav_key)) or 0.0,
                          bench=float(bench), invested_frac=invested))
    return pairs


def period_returns(pairs: Sequence[Pair], max_gap_days: int = 4) -> tuple[list[Obs], int]:
    """Differences between consecutive surviving pairs. Returns (observations, dropped).

    An interval longer than `max_gap_days` is discarded rather than treated as one observation:
    a multi-week outage is a genuine discontinuity in which the book may have been reconciled
    differently, and folding it in as a single "day" would distort both the mean and the
    bootstrap. The invested fraction is taken at the START of the interval — that is the
    exposure the book actually carried through it.
    """
    obs: list[Obs] = []
    dropped = 0
    for prev, cur in zip(pairs, pairs[1:]):
        gap = (cur.day - prev.day).days
        if gap <= 0 or gap > max_gap_days:
            dropped += 1
            continue
        if prev.nav == 0 or prev.bench == 0:
            dropped += 1
            continue
        obs.append(Obs(
            day=cur.day,
            nav_ret=cur.nav / prev.nav - 1.0,
            bench_ret=cur.bench / prev.bench - 1.0,
            gap_days=gap,
            invested_frac=prev.invested_frac,
        ))
    return obs, dropped


def excess_returns(obs: Sequence[Obs], beta_adjust: bool = True) -> list[float]:
    """Per-observation excess over the benchmark.

    With `beta_adjust`, the comparison is against holding the book's own invested fraction in
    the benchmark and the rest in cash — the counterfactual that actually corresponds to the
    decision "should this account get more money". Without it, a book that is 65% invested by
    design is scored as though it had chosen to be fully invested and simply underperformed.
    """
    out: list[float] = []
    for o in obs:
        weight = o.invested_frac if (beta_adjust and o.invested_frac is not None) else 1.0
        out.append(o.nav_ret - weight * o.bench_ret)
    return out


def periods_per_year(obs: Sequence[Obs]) -> float | None:
    """Observations per year implied by the series' own cadence.

    Not a hardcoded 252: the Autotrader only snapshots on days it actually ran (it is chained
    off the Advisor and can be paused or halted), so assuming a daily cadence would inflate its
    annualized figures relative to the paper track's.
    """
    if not obs:
        return None
    mean_gap = statistics.fmean(o.gap_days for o in obs)
    return _DAYS_PER_YEAR / mean_gap if mean_gap > 0 else None


def execution_metrics(
    pairs: Sequence[Pair],
    *,
    max_gap_days: int = 4,
    beta_adjust: bool = True,
    alpha: float = 0.10,
    seed: int = 0,
    block: int = 5,
    samples: int = 2000,
    good_threshold: float = 0.5,
    bad_threshold: float = -0.5,
) -> ExecutionMetrics:
    """The execution axis end to end: pair → difference → excess → interval → verdict."""
    obs, dropped = period_returns(pairs, max_gap_days)
    if not obs:
        return ExecutionMetrics(dropped_gap=dropped)

    adj = excess_returns(obs, beta_adjust=beta_adjust)
    raw = excess_returns(obs, beta_adjust=False)
    ppy = periods_per_year(obs)
    ir = information_ratio(adj, ppy)
    ci_low, ci_high = block_bootstrap_ci(adj, block=block, samples=samples, alpha=alpha, seed=seed)
    wins, sign_n, sign_p = sign_test(adj)
    fractions = [o.invested_frac for o in obs if o.invested_frac is not None]

    return ExecutionMetrics(
        n_obs=len(obs),
        span_days=(pairs[-1].day - pairs[0].day).days if len(pairs) > 1 else 0,
        dropped_gap=dropped,
        mean_excess=statistics.fmean(adj),
        stdev_excess=statistics.stdev(adj) if len(adj) > 1 else None,
        cum_excess_raw=compound(raw),
        cum_excess_adj=compound(adj),
        ir_annual=ir,
        periods_per_year=ppy,
        ci_low=ci_low,
        ci_high=ci_high,
        sign_wins=wins,
        sign_n=sign_n,
        sign_p=sign_p,
        mean_invested_frac=statistics.fmean(fractions) if fractions else None,
        axis=axis_verdict(ir, ci_low, ci_high, good_threshold, bad_threshold),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Pure — pick axis
# ══════════════════════════════════════════════════════════════════════════════

def _embedded(row: dict, key: str) -> dict:
    """A PostgREST embedded parent row, which arrives as a dict or a one-element list."""
    value = row.get(key)
    if isinstance(value, list):
        return value[0] if value else {}
    return value if isinstance(value, dict) else {}


def matured_picks(
    joined_rows: Iterable[dict],
    *,
    window_days: int = 30,
    tolerance_days: int = 15,
    actions: Sequence[str] = ("buy",),
) -> tuple[list[Pick], int]:
    """Outcome rows whose realized horizon genuinely matches the window being graded.

    The `notes` label ("30d outcome") is ignored on purpose. `backfill_outcomes` stamps it from
    the requested window but prices at whatever day it happened to run, so a missed run — or the
    first run over a backlog of older decisions — produces rows labelled 30d that realized over
    a much longer horizon. Grading those as 30-day results would silently mix horizons.

    Returns (picks, dropped) where `dropped` counts only horizon failures; rows filtered for
    being the wrong action were never candidates.
    """
    picks: list[Pick] = []
    dropped = 0
    wanted = {a.lower() for a in actions}
    for row in joined_rows:
        decision = _embedded(row, "decisions")
        if str(decision.get("action", "")).lower() not in wanted:
            continue
        run_day = _as_day(decision.get("run_date"))
        outcome_day = _as_day(row.get("outcome_date"))
        pct = _num(row.get("pct_change"))
        ticker = decision.get("ticker")
        if run_day is None or outcome_day is None or pct is None or not ticker:
            dropped += 1
            continue
        realized = (outcome_day - run_day).days
        if not (window_days <= realized <= window_days + tolerance_days):
            dropped += 1
            continue
        picks.append(Pick(
            ticker=str(ticker),
            decision_id=row.get("decision_id"),
            run_day=run_day,
            outcome_day=outcome_day,
            realized_days=realized,
            pct_change=pct,
        ))
    return picks, dropped


def nearest_close(series: dict[date, float], day: date, max_back_days: int = 5) -> float | None:
    """The benchmark close on `day`, or the most recent one within `max_back_days` before it.

    Walking back covers weekends and holidays. Beyond that window the caller is expected to drop
    the observation rather than benchmark against a stale price.
    """
    for back in range(max_back_days + 1):
        value = series.get(day - timedelta(days=back))
        if value is not None:
            return value
    return None


def attach_benchmark(picks: Sequence[Pick], bench_by_day: dict[date, float]) -> list[Pick]:
    """Attach each pick's benchmark move over its own holding window.

    A pick whose window can't be priced on both ends is excluded, not zero-filled: crediting a
    +4% call as +4% excess because the benchmark was unknown would flatter every gap in the data.
    """
    out: list[Pick] = []
    for p in picks:
        start = nearest_close(bench_by_day, p.run_day)
        end = nearest_close(bench_by_day, p.outcome_day)
        if not start or end is None:
            continue
        out.append(Pick(
            ticker=p.ticker, decision_id=p.decision_id, run_day=p.run_day,
            outcome_day=p.outcome_day, realized_days=p.realized_days,
            pct_change=p.pct_change, bench_pct=(end / start - 1.0) * 100.0,
        ))
    return out


def cluster_by_ticker(picks: Sequence[Pick]) -> list[float]:
    """One mean excess return per ticker — the effective observations on the pick axis.

    ASTRA re-logs a persisting signal on the same conviction names every week, so the raw row
    count wildly overstates independence: at the time of writing, 329 outcome rows sit on 14
    tickers. Averaging within a ticker first makes the sample count what it really is, bets.
    """
    grouped: dict[str, list[float]] = {}
    for p in picks:
        grouped.setdefault(p.ticker, []).append(p.excess)
    return [statistics.fmean(v) for _, v in sorted(grouped.items())]


def decision_weeks(picks: Sequence[Pick]) -> int:
    """Distinct ISO weeks the graded calls were made in — calendar diversity.

    Calls made in one week all faced the same market and are, in the way that matters here, one
    bet on that moment. 258 of the current 329 rows share a single run-date.
    """
    return len({p.run_day.isocalendar()[:2] for p in picks})


def pick_metrics(
    picks: Sequence[Pick],
    *,
    alpha: float = 0.10,
    seed: int = 0,
    samples: int = 2000,
    dropped_horizon: int = 0,
    good_threshold: float = 2.0,
    bad_threshold: float = -2.0,
) -> PickMetrics:
    """The pick axis end to end. All decisive statistics are ticker-clustered."""
    if not picks:
        return PickMetrics(dropped_horizon=dropped_horizon)

    clustered = cluster_by_ticker(picks)
    wins, sign_n, sign_p = sign_test(clustered)
    # Tickers are independent enough to resample singly; the correlation the block size exists
    # to protect against was already removed by clustering.
    ci_low, ci_high = block_bootstrap_ci(
        clustered, block=1, samples=samples, alpha=alpha, seed=seed,
    )
    clustered_mean = statistics.fmean(clustered)

    return PickMetrics(
        n_picks=len(picks),
        n_tickers=len(clustered),
        n_weeks=decision_weeks(picks),
        dropped_horizon=dropped_horizon,
        hit_rate=(wins / sign_n) if sign_n else None,
        hit_rate_p=sign_p,
        mean_excess=statistics.fmean(p.excess for p in picks),
        clustered_excess=clustered_mean,
        ci_low=ci_low,
        ci_high=ci_high,
        axis=axis_verdict(clustered_mean, ci_low, ci_high, good_threshold, bad_threshold),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Pure — round trips
# ══════════════════════════════════════════════════════════════════════════════

def round_trips(paper_trade_rows: Iterable[dict]) -> list[dict]:
    """Closed paper positions with their realized result.

    `paper_trades` stores no realized P&L — `memory.close_paper_trade` computes it inline at
    close time and only logs it — so it is recomputed here from the stored prices and share
    count. A `paper_trims` event is a PARTIAL realization and is deliberately not a round trip:
    the position is still open and its final result is still unknown.
    """
    trips: list[dict] = []
    for row in paper_trade_rows:
        if row.get("is_open"):
            continue
        entry = _num(row.get("price_at_signal"))
        exit_price = _num(row.get("close_price"))
        shares = _num(row.get("virtual_shares"))
        if not entry or exit_price is None or shares is None:
            continue
        trips.append({
            "ticker": row.get("ticker"),
            "entry": entry,
            "exit": exit_price,
            "shares": shares,
            "realized_pnl": (exit_price - entry) * shares,
            "return_pct": (exit_price / entry - 1.0) * 100.0,
            "close_reason": row.get("close_reason"),
        })
    return trips


def agent_round_trips(agent_trade_rows: Iterable[dict]) -> list[dict]:
    """Closed agentic positions. Unlike paper_trades, `realized_pnl` is stored here."""
    trips: list[dict] = []
    for row in agent_trade_rows:
        pnl = _num(row.get("realized_pnl"))
        if pnl is None or str(row.get("side", "")).lower() != "sell":
            continue
        entry = _num(row.get("price_at_signal")) or _num(row.get("avg_cost"))
        exit_price = _num(row.get("close_price")) or _num(row.get("fill_price"))
        ret = ((exit_price / entry - 1.0) * 100.0) if (entry and exit_price) else None
        trips.append({
            "ticker": row.get("ticker"),
            "entry": entry,
            "exit": exit_price,
            "realized_pnl": pnl,
            "return_pct": ret,
            "close_reason": row.get("close_reason"),
        })
    return trips


# ══════════════════════════════════════════════════════════════════════════════
# Pure — readiness, classification, diagnostics
# ══════════════════════════════════════════════════════════════════════════════

def readiness(
    *,
    nav_days: int,
    closed_trips: int,
    matured_decisions: int,
    distinct_tickers: int,
    distinct_decision_weeks: int,
    min_nav_days: int,
    min_closed_trips: int,
    min_matured_decisions: int,
    min_distinct_tickers: int,
    min_decision_weeks: int,
    nav_span_days: int = 0,
    decision_span_days: int = 0,
) -> Readiness:
    """Whether there is enough evidence to say anything decisive, and if not, what is missing.

    Every dimension must clear independently. Volume alone is not a gate — ticker and calendar
    diversity are separate requirements precisely because row counts here are dominated by the
    same handful of conviction names re-logged over and over.
    """
    checks = [
        ("nav days", nav_days, min_nav_days, nav_span_days),
        ("finished trades", closed_trips, min_closed_trips, nav_span_days),
        ("matured calls", matured_decisions, min_matured_decisions, decision_span_days),
        ("different companies", distinct_tickers, min_distinct_tickers, decision_span_days),
        ("different weeks", distinct_decision_weeks, min_decision_weeks, decision_span_days),
    ]
    missing = [f"{name} {have}/{need}" for name, have, need, _ in checks if have < need]

    eta: int | None = 0
    for _, have, need, span in checks:
        if have >= need:
            continue
        rate = (have / span) if span > 0 else 0.0
        if rate <= 0:
            eta = None  # nothing is accruing — an ETA would be invented, not projected
            break
        eta = max(eta or 0, math.ceil((need - have) / rate))

    return Readiness(
        ok=not missing,
        nav_days=nav_days,
        closed_trips=closed_trips,
        matured_decisions=matured_decisions,
        distinct_tickers=distinct_tickers,
        distinct_decision_weeks=distinct_decision_weeks,
        missing=missing,
        eta_days=None if not missing else eta,
    )


def axis_verdict(
    point: float | None,
    ci_low: float | None,
    ci_high: float | None,
    good_threshold: float,
    bad_threshold: float,
) -> str:
    """Grade one axis, requiring BOTH a meaningful point estimate and an interval off zero.

    Either test alone is inadequate here: the point estimate says "big enough to care about"
    but at these sample sizes says nothing about reliability, while a confidence interval that
    excludes zero can still describe an effect too small to act on.
    """
    if point is None or ci_low is None or ci_high is None:
        return FLAT
    if point >= good_threshold and ci_low > 0:
        return GOOD
    if point <= bad_threshold and ci_high < 0:
        return BAD
    return FLAT


def classify(
    gate: Readiness, execution: ExecutionMetrics, picks: PickMetrics,
) -> tuple[str, list[str]]:
    """Cross the two axes into a verdict, with the reasoning that produced it."""
    if not gate.ok:
        reasons = [f"Still gathering evidence: {', '.join(gate.missing)}."]
        if gate.eta_days:
            reasons.append(f"At the current rate that is roughly {gate.eta_days} more days.")
        return INSUFFICIENT_DATA, reasons

    if picks.axis == BAD:
        return REVIEW, [
            f"The picks themselves are lagging the market: {picks.clustered_excess:+.1f}% "
            f"average excess across {picks.n_tickers} companies.",
            "No setting fixes this — it is a question about the convictions themselves.",
        ]
    if picks.axis == GOOD and execution.axis == GOOD:
        return SCALE, [
            f"Picks are ahead of the market ({picks.clustered_excess:+.1f}% average excess "
            f"across {picks.n_tickers} companies).",
            f"The book is ahead too ({(execution.cum_excess_adj or 0) * 100:+.1f}% after "
            "allowing for uninvested cash).",
            "The record supports putting more capital into the Autotrader sleeve.",
        ]
    if picks.axis == GOOD and execution.axis == BAD:
        return TUNE, [
            f"The picks are good ({picks.clustered_excess:+.1f}% average excess) but the book "
            f"is behind ({(execution.cum_excess_adj or 0) * 100:+.1f}%).",
            "That gap is in how positions are timed, sized or exited — not in what is chosen.",
        ]
    return STEADY, [
        f"Measured over {execution.n_obs} paired days and {picks.n_tickers} companies, "
        "nothing stands out either way.",
        "Neither result is far enough from the market to be more than noise.",
    ]


def tune_diagnostic(
    execution: ExecutionMetrics, picks: PickMetrics, trips: Sequence[dict],
) -> dict[str, Any]:
    """Where the execution gap most likely sits. Descriptive only — never a proposed change.

    This is the piece that makes TUNE actionable without violating the doctrine in
    docs/attribution.md: it names a measurable fingerprint and hands it to a human. It must
    never contain a setting name paired with a number to apply, and must never write anything.
    """
    out: dict[str, Any] = {"fingerprints": []}

    returns = [t["return_pct"] for t in trips if t.get("return_pct") is not None]
    if returns and picks.clustered_excess is not None:
        trip_mean = statistics.fmean(returns)
        out["trip_mean_return_pct"] = round(trip_mean, 3)
        out["pick_clustered_excess_pct"] = round(picks.clustered_excess, 3)
        if trip_mean < picks.clustered_excess:
            out["fingerprints"].append({
                "name": "exits-too-early",
                "detail": (
                    f"Positions were closed at an average of {trip_mean:+.1f}%, while the same "
                    f"kinds of calls were still {picks.clustered_excess:+.1f}% ahead of the "
                    "market at the measurement horizon. The selling rules may be cutting "
                    "winners short."
                ),
                "gap_pct": round(picks.clustered_excess - trip_mean, 3),
            })

    raw, adj = execution.cum_excess_raw, execution.cum_excess_adj
    frac = execution.mean_invested_frac
    if raw is not None and adj is not None and frac is not None and frac < 0.9 and raw < 0 <= adj:
        out["mean_invested_frac"] = round(frac, 4)
        out["fingerprints"].append({
            "name": "cash-drag",
            "detail": (
                f"On average only {frac * 100:.0f}% of the account was invested. Allowing for "
                f"that, the picks kept pace ({adj * 100:+.1f}%); it is the amount of money left "
                f"uninvested that produced the raw shortfall ({raw * 100:+.1f}%). The question "
                "is deployment pace and the cash reserve, not stock selection."
            ),
        })

    if not out["fingerprints"]:
        out["fingerprints"].append({
            "name": "unattributed",
            "detail": "The shortfall did not match a known pattern — worth looking at by hand.",
        })
    return out


def should_notify(verdict: str, previous_verdict: str | None) -> bool:
    """Alert only when the conclusion actually changes.

    A daily "still not enough data" message is worthless and would train the alert to be
    ignored, which costs exactly the signal this whole module exists to deliver. The very first
    row is silent when there is nothing to say yet, but a decisive first verdict does fire.
    """
    if previous_verdict is None:
        return verdict != INSUFFICIENT_DATA
    return verdict != previous_verdict


# ══════════════════════════════════════════════════════════════════════════════
# I/O — reading the record, persisting the verdict
# ══════════════════════════════════════════════════════════════════════════════

_SPY_CACHE: dict[tuple[str, int], tuple[dict[date, float], str]] = {}


def _benchmark_by_day(symbol: str, days: int) -> tuple[dict[date, float], str]:
    """Benchmark closes keyed by day, preferring a complete yfinance history.

    `benchmark_prices` is the fallback rather than the source: it only gains a row on days the
    daily run reached its performance step, so it shares a failure path with the very NAV series
    it would be compared against — a day the SPY fetch failed silently costs a paired
    observation. The chosen source is persisted with the verdict so an odd result is traceable.
    """
    cached = _SPY_CACHE.get((symbol, days))
    if cached:
        return cached

    result: tuple[dict[date, float], str] | None = None
    try:
        import yfinance as yf

        history = yf.Ticker(symbol).history(period=f"{days}d")
        closes = history["Close"]
        series = {}
        for idx in closes.index:
            day, close = _as_day(idx), _num(closes.loc[idx])
            if day is not None and close is not None and not math.isnan(close):
                series[day] = close
        if series:
            result = (series, "yfinance")
    except Exception as e:  # noqa: BLE001 — degrade to the stored series, never fail the run
        logger.warning(f"Scorecard: yfinance {symbol} history unavailable ({e}) — using the DB series.")

    if result is None:
        from src import memory

        rows = memory.get_benchmark_series(symbol)
        series = {}
        for row in rows:
            day, close = _as_day(row.get("price_date")), _num(row.get("close"))
            if day is not None and close is not None:
                series[day] = close
        result = (series, "benchmark_prices")

    _SPY_CACHE[(symbol, days)] = result
    return result


def _round(value: float | None, places: int = 8) -> float | None:
    return None if value is None else round(value, places)


def evaluate_track(
    track: str, *, as_of: str | None = None, bench: tuple[dict[date, float], str] | None = None,
) -> dict[str, Any]:
    """Grade one track. Reads only — every write lives in `run_scorecard`."""
    from src import config, memory

    cfg = config.scorecard
    bench_by_day, bench_source = bench or _benchmark_by_day(cfg.benchmark_symbol, cfg.history_days)
    cutoff = _as_day(as_of)
    if cutoff:
        bench_by_day = {d: c for d, c in bench_by_day.items() if d <= cutoff}

    if track == PAPER:
        nav_rows = memory.get_paper_equity_series()
        trips = round_trips(memory.get_paper_trades_history())
    else:
        nav_rows = memory.get_agent_snapshot_series()
        trips = agent_round_trips(memory.get_agent_trades_history())

    pairs = pair_series(nav_rows, bench_by_day)
    if cutoff:
        pairs = [p for p in pairs if p.day <= cutoff]

    alpha = round(1.0 - cfg.confidence_level, 6)
    execution = execution_metrics(
        pairs,
        max_gap_days=cfg.max_join_gap_days,
        beta_adjust=cfg.beta_adjust_benchmark,
        alpha=alpha,
        seed=cfg.bootstrap_seed,
        block=cfg.bootstrap_block,
        samples=cfg.bootstrap_samples,
        good_threshold=cfg.ir_scale_threshold,
        bad_threshold=cfg.ir_review_threshold,
    )

    raw_picks, dropped = matured_picks(
        memory.get_outcomes_with_decisions(),
        window_days=cfg.pick_window_days,
        tolerance_days=cfg.pick_horizon_tolerance_days,
    )
    if cutoff:
        raw_picks = [p for p in raw_picks if p.outcome_day <= cutoff]
    picks = pick_metrics(
        attach_benchmark(raw_picks, bench_by_day),
        alpha=alpha,
        seed=cfg.bootstrap_seed,
        samples=cfg.bootstrap_samples,
        dropped_horizon=dropped,
        good_threshold=cfg.pick_excess_scale_pct,
        bad_threshold=cfg.pick_excess_review_pct,
    )

    decision_span = 0
    if raw_picks:
        decision_span = (max(p.run_day for p in raw_picks)
                         - min(p.run_day for p in raw_picks)).days
    gate = readiness(
        nav_days=execution.n_obs,
        closed_trips=len(trips),
        matured_decisions=picks.n_picks,
        distinct_tickers=picks.n_tickers,
        distinct_decision_weeks=picks.n_weeks,
        min_nav_days=cfg.min_nav_days,
        min_closed_trips=cfg.min_closed_trips,
        min_matured_decisions=cfg.min_matured_decisions,
        min_distinct_tickers=cfg.min_distinct_tickers,
        min_decision_weeks=cfg.min_decision_weeks,
        nav_span_days=execution.span_days,
        decision_span_days=decision_span,
    )

    verdict, reasons = classify(gate, execution, picks)
    diagnostic = tune_diagnostic(execution, picks, trips) if verdict == TUNE else None

    return {
        "scored_date": (cutoff or date.today()).isoformat(),
        "as_of_date": pairs[-1].day.isoformat() if pairs else None,
        "track": track,
        "verdict": verdict,
        "ready": gate.ok,
        "nav_days": gate.nav_days,
        "nav_span_days": execution.span_days,
        "dropped_gap_obs": execution.dropped_gap,
        "closed_trips": gate.closed_trips,
        "matured_decisions": gate.matured_decisions,
        "dropped_horizon": picks.dropped_horizon,
        "distinct_tickers": gate.distinct_tickers,
        "distinct_decision_weeks": gate.distinct_decision_weeks,
        "missing": gate.missing,
        "eta_days": gate.eta_days,
        "exec_axis": execution.axis,
        "exec_cum_excess_raw": _round(execution.cum_excess_raw),
        "exec_cum_excess_adj": _round(execution.cum_excess_adj),
        "exec_mean_excess": _round(execution.mean_excess, 10),
        "exec_stdev_excess": _round(execution.stdev_excess, 10),
        "exec_ir_annual": _round(execution.ir_annual),
        "exec_periods_per_year": _round(execution.periods_per_year, 4),
        "exec_ci_low": _round(execution.ci_low, 10),
        "exec_ci_high": _round(execution.ci_high, 10),
        "exec_sign_wins": execution.sign_wins,
        "exec_sign_n": execution.sign_n,
        "exec_sign_p": _round(execution.sign_p),
        "exec_mean_invested_frac": _round(execution.mean_invested_frac, 6),
        "bench_source": bench_source,
        "pick_axis": picks.axis,
        "pick_window_days": cfg.pick_window_days,
        "pick_hit_rate": _round(picks.hit_rate, 6),
        "pick_hit_rate_p": _round(picks.hit_rate_p),
        "pick_mean_excess": _round(picks.mean_excess),
        "pick_clustered_excess": _round(picks.clustered_excess),
        "pick_ci_low": _round(picks.ci_low),
        "pick_ci_high": _round(picks.ci_high),
        "diagnostic": diagnostic,
        "reasons": reasons,
        "metrics": {
            "trips": len(trips),
            "trip_mean_return_pct": (
                round(statistics.fmean([t["return_pct"] for t in trips
                                        if t.get("return_pct") is not None]), 3)
                if any(t.get("return_pct") is not None for t in trips) else None
            ),
        },
    }


def run_scorecard(
    *, as_of: str | None = None, persist: bool = True, notify: bool = True,
) -> list[dict[str, Any]]:
    """Grade both tracks, persist each verdict, and alert only on a change.

    Persist happens BEFORE notify on purpose: if Telegram fails after a verdict row is already
    written, the change is recorded and simply goes unannounced — whereas notifying first and
    failing to persist would re-fire the same transition every day until it was stored.

    Each track is isolated: a broken read on one never stops the other from being graded.
    """
    from src import config, memory
    from src import notify as notifier

    cfg = config.scorecard
    if not cfg.enabled:
        logger.info("Scorecard: disabled — skipping.")
        return []

    results: list[dict[str, Any]] = []
    for track in TRACKS:
        try:
            result = evaluate_track(track, as_of=as_of)
            previous = memory.get_latest_scorecard(track) if persist else None
            prev_verdict = previous.get("verdict") if previous else None
            fire = notify and cfg.notify_enabled and should_notify(result["verdict"], prev_verdict)

            result["prev_verdict"] = prev_verdict
            result["notified"] = bool(fire)
            if persist:
                memory.save_scorecard_run(result)
            if fire:
                notifier.notify_scorecard(result, prev_verdict)

            logger.info(
                f"Scorecard [{track}]: {result['verdict']}"
                + (f" (was {prev_verdict})" if prev_verdict else "")
                + (f" — missing {', '.join(result['missing'])}" if result["missing"] else "")
            )
            results.append(result)
        except Exception:
            logger.error(f"Scorecard: {track} track failed to evaluate", exc_info=True)
    return results


def _format(result: dict[str, Any]) -> str:
    """Human-readable CLI rendering of one track's grade."""
    lines = [
        f"── {result['track']} ─────────────────────────────",
        f"verdict : {result['verdict']}",
        f"ready   : {result['ready']}",
    ]
    if result["missing"]:
        lines.append(f"missing : {', '.join(result['missing'])}")
        if result["eta_days"]:
            lines.append(f"eta     : ~{result['eta_days']} days")
    lines += [
        f"exec    : {result['exec_axis']}  adj {(result['exec_cum_excess_adj'] or 0) * 100:+.2f}%"
        f"  raw {(result['exec_cum_excess_raw'] or 0) * 100:+.2f}%"
        f"  IR {result['exec_ir_annual']}  n={result['nav_days']}",
        f"picks   : {result['pick_axis']}  clustered {result['pick_clustered_excess']}%"
        f"  hit {result['pick_hit_rate']}  rows={result['matured_decisions']}"
        f"  names={result['distinct_tickers']}  weeks={result['distinct_decision_weeks']}",
        f"source  : {result['bench_source']}",
    ]
    for reason in result["reasons"]:
        lines.append(f"  · {reason}")
    return "\n".join(lines)


def main() -> None:
    import argparse

    from src import config
    from src.logger import setup as setup_logging

    p = argparse.ArgumentParser(description="ASTRA scorecard — grade the track record (read-only)")
    p.add_argument("--dry-run", action="store_true",
                   help="compute and print; write nothing and send nothing")
    p.add_argument("--as-of", metavar="YYYY-MM-DD",
                   help="replay the grade as it would have read on this date")
    p.add_argument("--track", choices=TRACKS, help="restrict output to one track")
    # Gate overrides exist to exercise the decisive branches on real data long before the real
    # bar is met. They are --dry-run only: a relaxed gate must never be able to write a verdict.
    for name, kind in (("min-nav-days", int), ("min-closed-trips", int),
                       ("min-matured-decisions", int), ("min-distinct-tickers", int),
                       ("min-decision-weeks", int)):
        p.add_argument(f"--{name}", type=kind,
                       help=f"override scorecard.{name.replace('-', '_')} (requires --dry-run)")
    args = p.parse_args()

    setup_logging()

    overrides = {
        key: getattr(args, key)
        for key in ("min_nav_days", "min_closed_trips", "min_matured_decisions",
                    "min_distinct_tickers", "min_decision_weeks")
        if getattr(args, key) is not None
    }
    if overrides and not args.dry_run:
        p.error("gate overrides are only allowed with --dry-run")
    for key, value in overrides.items():
        setattr(config.scorecard, key, value)
        print(f"[override] scorecard.{key} = {value}")

    results = run_scorecard(
        as_of=args.as_of, persist=not args.dry_run, notify=not args.dry_run,
    )
    for result in results:
        if args.track and result["track"] != args.track:
            continue
        print(_format(result))
    if args.dry_run:
        print("\n(dry run — nothing was written or sent)")


if __name__ == "__main__":
    main()
