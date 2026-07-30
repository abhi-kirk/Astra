"""
Unit tests for the pure self-evaluation math in src/scorecard.py.

No mocking anywhere — every function under test is I/O-free by construction (the same split
src/performance.py uses), so these run on hand-built series.

Several tests exist specifically to pin down the three properties that make the verdict honest
rather than merely confident, and are labelled as such:

  • cash-drag adjustment — a partially-invested book is not scored as though it chose to be
    fully invested (TestExcessReturns)
  • a point estimate never acts alone — an information ratio of exactly the SCALE threshold,
    over the minimum sample, must NOT produce SCALE (TestAxisVerdict / TestBootstrapCI)
  • sample size is counted in bets, not rows — 329 rows on 14 tickers is 14 observations
    (TestClusterByTicker)
"""

from datetime import date, timedelta

import pytest

from src.scorecard import (
    BAD,
    FLAT,
    GOOD,
    INSUFFICIENT_DATA,
    REVIEW,
    SCALE,
    STEADY,
    TUNE,
    ExecutionMetrics,
    Obs,
    Pair,
    Pick,
    PickMetrics,
    agent_round_trips,
    attach_benchmark,
    axis_verdict,
    block_bootstrap_ci,
    classify,
    cluster_by_ticker,
    compound,
    decision_weeks,
    excess_returns,
    execution_metrics,
    information_ratio,
    matured_picks,
    nearest_close,
    pair_series,
    period_returns,
    periods_per_year,
    pick_metrics,
    readiness,
    round_trips,
    should_notify,
    sign_test,
    tune_diagnostic,
)

D0 = date(2026, 1, 5)  # a Monday


def _day(n: int) -> str:
    return (D0 + timedelta(days=n)).isoformat()


def _nav(n: int, nav: float, equity: float = 100.0, market_value: float = 100.0) -> dict:
    return {
        "snapshot_time": f"{_day(n)}T13:00:00+00:00",
        "nav_index": nav,
        "total_equity": equity,
        "market_value": market_value,
    }


def _bench(days_and_closes: dict[int, float]) -> dict[date, float]:
    return {D0 + timedelta(days=n): c for n, c in days_and_closes.items()}


def _pair(n: int, nav: float, bench: float, invested: float | None = 1.0) -> Pair:
    return Pair(day=D0 + timedelta(days=n), nav=nav, bench=bench, invested_frac=invested)


class TestCompound:
    def test_chains_geometrically(self):
        assert compound([0.10, 0.10]) == pytest.approx(0.21)

    def test_empty_is_flat(self):
        assert compound([]) == 0.0


class TestPairSeries:
    def test_inner_join_drops_unmatched_dates_both_ways(self):
        rows = [_nav(0, 100), _nav(1, 101), _nav(2, 102)]
        # benchmark is missing day 1, and has a day 5 the book never snapshotted
        pairs = pair_series(rows, _bench({0: 500.0, 2: 510.0, 5: 520.0}))
        assert [p.day.day for p in pairs] == [5, 7]  # Jan 5 and Jan 7

    def test_duplicate_snapshots_on_one_day_keep_the_last(self):
        # A re-run writes a second row for the same date; carrying both would manufacture a
        # phantom 0% interval between them.
        rows = [_nav(0, 100), _nav(0, 105), _nav(1, 110)]
        pairs = pair_series(rows, _bench({0: 500.0, 1: 505.0}))
        assert len(pairs) == 2
        assert pairs[0].nav == 105

    def test_invested_fraction_computed_from_market_value(self):
        rows = [_nav(0, 100, equity=200.0, market_value=140.0)]
        pairs = pair_series(rows, _bench({0: 500.0}))
        assert pairs[0].invested_frac == pytest.approx(0.7)

    def test_zero_equity_leaves_invested_fraction_unknown(self):
        rows = [_nav(0, 100, equity=0.0, market_value=0.0)]
        pairs = pair_series(rows, _bench({0: 500.0}))
        assert pairs[0].invested_frac is None

    def test_rows_missing_nav_are_skipped(self):
        rows = [{"snapshot_time": f"{_day(0)}T13:00:00+00:00", "nav_index": None}, _nav(1, 100)]
        assert len(pair_series(rows, _bench({0: 500.0, 1: 505.0}))) == 1


class TestPeriodReturns:
    def test_consecutive_pairs_only(self):
        pairs = [_pair(0, 100, 500), _pair(1, 110, 550), _pair(2, 121, 605)]
        obs, dropped = period_returns(pairs, max_gap_days=4)
        assert dropped == 0
        assert [round(o.nav_ret, 6) for o in obs] == [0.1, 0.1]

    def test_weekend_gap_is_retained_as_one_longer_observation(self):
        pairs = [_pair(0, 100, 500), _pair(3, 110, 550)]
        obs, dropped = period_returns(pairs, max_gap_days=4)
        assert dropped == 0
        assert obs[0].gap_days == 3

    def test_long_gap_is_dropped_not_forward_filled(self):
        # The no-fabrication guarantee: a hole yields n-2 observations, not n-1.
        pairs = [_pair(0, 100, 500), _pair(1, 101, 505), _pair(21, 150, 600), _pair(22, 151, 605)]
        obs, dropped = period_returns(pairs, max_gap_days=4)
        assert len(obs) == len(pairs) - 2
        assert dropped == 1

    def test_zero_previous_nav_is_dropped_not_divided_by(self):
        pairs = [_pair(0, 0.0, 500), _pair(1, 100, 505)]
        obs, dropped = period_returns(pairs, max_gap_days=4)
        assert obs == [] and dropped == 1

    def test_invested_fraction_taken_at_interval_start(self):
        pairs = [_pair(0, 100, 500, invested=0.5), _pair(1, 110, 550, invested=0.9)]
        obs, _ = period_returns(pairs, max_gap_days=4)
        assert obs[0].invested_frac == 0.5


class TestExcessReturns:
    def _obs(self, nav_ret, bench_ret, invested):
        return [Obs(day=D0, nav_ret=nav_ret, bench_ret=bench_ret, gap_days=1,
                    invested_frac=invested)]

    def test_raw_excess_is_plain_difference(self):
        assert excess_returns(self._obs(0.0, 0.02, 0.7), beta_adjust=False)[0] == pytest.approx(-0.02)

    def test_cash_drag_adjustment_scales_the_benchmark_leg(self):
        # THE regression test for finding 1. A book that is 70% invested by design and went
        # nowhere while the market rose 2% is behind by 1.4%, not 2% — it never had the other
        # 30% exposed. Scoring it at -2% would be a market-direction detector, not a skill test.
        assert excess_returns(self._obs(0.0, 0.02, 0.7))[0] == pytest.approx(-0.014)

    def test_fully_invested_book_is_unaffected_by_the_adjustment(self):
        adj = excess_returns(self._obs(0.03, 0.02, 1.0))[0]
        raw = excess_returns(self._obs(0.03, 0.02, 1.0), beta_adjust=False)[0]
        assert adj == pytest.approx(raw)

    def test_unknown_invested_fraction_falls_back_to_raw(self):
        assert excess_returns(self._obs(0.0, 0.02, None))[0] == pytest.approx(-0.02)


class TestPeriodsPerYear:
    def test_daily_cadence(self):
        obs = [Obs(D0, 0.0, 0.0, 1, 1.0)] * 10
        assert periods_per_year(obs) == pytest.approx(365.25)

    def test_sparser_cadence_annualizes_lower(self):
        # A track that only snapshots on days it ran must not be annualized as if daily.
        obs = [Obs(D0, 0.0, 0.0, 3, 1.0)] * 10
        assert periods_per_year(obs) == pytest.approx(365.25 / 3)

    def test_empty_is_none(self):
        assert periods_per_year([]) is None


class TestInformationRatio:
    def test_hand_computed(self):
        values = [0.01, 0.02, 0.03]
        # mean 0.02, sample stdev 0.01 → 2.0, annualized by sqrt(4) = 4.0
        assert information_ratio(values, 4.0) == pytest.approx(4.0)

    def test_zero_volatility_is_none_not_infinite(self):
        assert information_ratio([0.01, 0.01, 0.01], 252) is None

    def test_single_observation_is_none(self):
        assert information_ratio([0.01], 252) is None


class TestBootstrapCI:
    def test_fixed_seed_is_reproducible(self):
        # A verdict that changed between two runs on identical data would be indefensible.
        values = [0.01, -0.02, 0.03, -0.01, 0.02, 0.0, 0.01, -0.005]
        assert block_bootstrap_ci(values, seed=0, samples=300) == \
               block_bootstrap_ci(values, seed=0, samples=300)

    def test_strongly_positive_series_excludes_zero(self):
        lo, _ = block_bootstrap_ci([0.02] * 40, samples=300, seed=0)
        assert lo is not None and lo > 0

    def test_pure_noise_interval_straddles_zero(self):
        values = [0.02 if i % 2 else -0.02 for i in range(60)]
        lo, hi = block_bootstrap_ci(values, samples=500, seed=0)
        assert lo is not None and hi is not None
        assert lo < 0 < hi

    def test_too_few_observations_is_none(self):
        assert block_bootstrap_ci([0.01]) == (None, None)


class TestSignTest:
    def test_hand_computed_two_sided_p(self):
        wins, n, p = sign_test([1] * 8 + [-1] * 2)
        assert (wins, n) == (8, 10)
        assert p == pytest.approx(0.109375, abs=1e-6)

    def test_zeros_are_discarded(self):
        wins, n, _ = sign_test([1, -1, 0, 0])
        assert (wins, n) == (1, 2)

    def test_empty_is_guarded(self):
        assert sign_test([]) == (0, 0, None)


# ── pick axis ─────────────────────────────────────────────────────────────────

def _outcome(ticker: str, run_offset: int, realized_days: int, pct: float,
             action: str = "buy", decision_id: int = 1) -> dict:
    run_day = D0 + timedelta(days=run_offset)
    return {
        "decision_id": decision_id,
        "outcome_date": (run_day + timedelta(days=realized_days)).isoformat(),
        "pct_change": pct,
        "notes": "30d outcome",
        "decisions": {
            "ticker": ticker, "action": action,
            "run_date": run_day.isoformat(), "price_at_decision": 100.0,
        },
    }


class TestMaturedPicks:
    def test_row_realized_far_outside_the_window_is_dropped(self):
        # The `notes` label says 30d; the realized horizon says 97. Grading it as a 30-day
        # result would silently mix horizons, so it is dropped and counted.
        picks, dropped = matured_picks([_outcome("AMD", 0, 97, 5.0)], window_days=30,
                                       tolerance_days=15)
        assert picks == [] and dropped == 1

    def test_row_inside_the_tolerance_is_kept(self):
        picks, dropped = matured_picks([_outcome("AMD", 0, 34, 5.0)], window_days=30,
                                       tolerance_days=15)
        assert len(picks) == 1 and dropped == 0
        assert picks[0].realized_days == 34

    def test_row_realized_early_is_dropped(self):
        picks, dropped = matured_picks([_outcome("AMD", 0, 29, 5.0)], window_days=30,
                                       tolerance_days=15)
        assert picks == [] and dropped == 1

    def test_non_buy_actions_are_filtered_not_counted_as_dropped(self):
        rows = [_outcome("AMD", 0, 31, 5.0, action="watch")]
        picks, dropped = matured_picks(rows, window_days=30, tolerance_days=15)
        assert picks == [] and dropped == 0

    def test_embedded_decision_as_list_is_handled(self):
        row = _outcome("AMD", 0, 31, 5.0)
        row["decisions"] = [row["decisions"]]
        picks, _ = matured_picks([row], window_days=30, tolerance_days=15)
        assert len(picks) == 1

    def test_malformed_row_is_dropped_not_crashed(self):
        row = _outcome("AMD", 0, 31, 5.0)
        row["pct_change"] = None
        picks, dropped = matured_picks([row], window_days=30, tolerance_days=15)
        assert picks == [] and dropped == 1


class TestNearestClose:
    def test_exact_day_wins(self):
        assert nearest_close(_bench({0: 500.0, 1: 505.0}), D0 + timedelta(days=1)) == 505.0

    def test_walks_back_over_a_weekend(self):
        assert nearest_close(_bench({0: 500.0}), D0 + timedelta(days=2)) == 500.0

    def test_beyond_the_window_is_none(self):
        assert nearest_close(_bench({0: 500.0}), D0 + timedelta(days=9)) is None


class TestAttachBenchmark:
    def _pick(self, pct: float, run_offset: int = 0, realized: int = 31) -> Pick:
        run_day = D0 + timedelta(days=run_offset)
        return Pick(ticker="AMD", decision_id=1, run_day=run_day,
                    outcome_day=run_day + timedelta(days=realized),
                    realized_days=realized, pct_change=pct)

    def test_excess_is_measured_against_the_same_window(self):
        bench = _bench({0: 100.0, 31: 105.0})
        out = attach_benchmark([self._pick(8.0)], bench)
        assert out[0].bench_pct == pytest.approx(5.0)
        assert out[0].excess == pytest.approx(3.0)

    def test_unpriceable_window_excludes_the_pick_rather_than_zero_filling(self):
        # Crediting a +8% call as +8% excess because the benchmark was unknown would flatter
        # every gap in the data.
        assert attach_benchmark([self._pick(8.0)], _bench({0: 100.0})) == []


class TestClusterByTicker:
    def test_rows_collapse_to_one_observation_per_name(self):
        # THE regression test for finding 2: the live table holds 329 outcome rows across 14
        # tickers. Treating that as n=329 would produce a confident verdict from ~14 bets.
        picks = []
        for i in range(329):
            p = Pick(ticker=f"T{i % 14}", decision_id=i, run_day=D0,
                     outcome_day=D0 + timedelta(days=31), realized_days=31,
                     pct_change=1.0, bench_pct=0.0)
            picks.append(p)
        assert len(cluster_by_ticker(picks)) == 14

    def test_clustered_mean_differs_from_row_mean_when_unbalanced(self):
        picks = [
            Pick("A", 1, D0, D0, 31, 10.0, 0.0),
            Pick("B", 2, D0, D0, 31, 0.0, 0.0),
            Pick("B", 3, D0, D0, 31, 0.0, 0.0),
            Pick("B", 4, D0, D0, 31, 0.0, 0.0),
        ]
        clustered = cluster_by_ticker(picks)
        assert sum(clustered) / len(clustered) == pytest.approx(5.0)   # A and B weigh equally
        assert sum(p.excess for p in picks) / len(picks) == pytest.approx(2.5)


class TestDecisionWeeks:
    def test_calls_in_one_week_count_once(self):
        picks = [Pick("A", i, D0 + timedelta(days=i), D0, 31, 1.0, 0.0) for i in range(3)]
        assert decision_weeks(picks) == 1

    def test_calls_across_weeks_count_separately(self):
        picks = [Pick("A", 1, D0, D0, 31, 1.0, 0.0),
                 Pick("A", 2, D0 + timedelta(days=14), D0, 31, 1.0, 0.0)]
        assert decision_weeks(picks) == 2


class TestPickMetrics:
    def test_statistics_are_clustered_not_row_level(self):
        picks = [Pick(f"T{i}", i, D0, D0, 31, 6.0, 1.0) for i in range(12)]
        m = pick_metrics(picks, samples=200, good_threshold=2.0, bad_threshold=-2.0)
        assert m.n_picks == 12 and m.n_tickers == 12
        assert m.clustered_excess == pytest.approx(5.0)
        assert m.axis == GOOD

    def test_empty_picks_are_flat(self):
        assert pick_metrics([], dropped_horizon=7).axis == FLAT
        assert pick_metrics([], dropped_horizon=7).dropped_horizon == 7


# ── round trips ───────────────────────────────────────────────────────────────

class TestRoundTrips:
    def test_realized_pnl_recomputed_from_stored_prices(self):
        # paper_trades stores no realized_pnl — close_paper_trade computes it inline and only logs.
        rows = [{"ticker": "AMD", "is_open": False, "price_at_signal": 100.0,
                 "close_price": 120.0, "virtual_shares": 3.0}]
        trip = round_trips(rows)[0]
        assert trip["realized_pnl"] == pytest.approx(60.0)
        assert trip["return_pct"] == pytest.approx(20.0)

    def test_open_lots_excluded(self):
        rows = [{"ticker": "AMD", "is_open": True, "price_at_signal": 100.0,
                 "close_price": None, "virtual_shares": 3.0}]
        assert round_trips(rows) == []

    def test_malformed_close_is_skipped_not_crashed(self):
        rows = [{"ticker": "AMD", "is_open": False, "price_at_signal": 100.0,
                 "close_price": None, "virtual_shares": 3.0}]
        assert round_trips(rows) == []

    def test_agent_trips_use_the_stored_pnl(self):
        rows = [{"ticker": "AMD", "side": "sell", "realized_pnl": 12.5,
                 "price_at_signal": 100.0, "close_price": 110.0}]
        trip = agent_round_trips(rows)[0]
        assert trip["realized_pnl"] == 12.5
        assert trip["return_pct"] == pytest.approx(10.0)

    def test_agent_buys_are_not_round_trips(self):
        rows = [{"ticker": "AMD", "side": "buy", "realized_pnl": None}]
        assert agent_round_trips(rows) == []


# ── readiness, verdicts ───────────────────────────────────────────────────────

def _gate(**kw):
    base = dict(
        nav_days=100, closed_trips=30, matured_decisions=100,
        distinct_tickers=20, distinct_decision_weeks=12,
        min_nav_days=90, min_closed_trips=20, min_matured_decisions=60,
        min_distinct_tickers=10, min_decision_weeks=8,
        nav_span_days=120, decision_span_days=120,
    )
    base.update(kw)
    return readiness(**base)


class TestReadiness:
    def test_all_clear(self):
        g = _gate()
        assert g.ok and g.missing == [] and g.eta_days is None

    @pytest.mark.parametrize("field,value,label", [
        ("nav_days", 13, "nav days"),
        ("closed_trips", 8, "finished trades"),
        ("matured_decisions", 10, "matured calls"),
        ("distinct_tickers", 3, "different companies"),
        ("distinct_decision_weeks", 1, "different weeks"),
    ])
    def test_each_dimension_blocks_independently(self, field, value, label):
        g = _gate(**{field: value})
        assert not g.ok
        assert any(m.startswith(label) for m in g.missing)

    def test_volume_alone_does_not_clear_the_gate(self):
        # The live shape today: plenty of rows, but on 14 names across a single week.
        g = _gate(matured_decisions=329, distinct_tickers=14, distinct_decision_weeks=1)
        assert not g.ok
        assert g.missing == ["different weeks 1/8"]

    def test_missing_strings_name_the_deficit_and_target(self):
        assert "nav days 13/90" in _gate(nav_days=13).missing

    def test_eta_projected_from_the_observed_rate(self):
        # 45 obs over 90 days = 0.5/day; 45 more needed → ~90 days.
        g = _gate(nav_days=45, nav_span_days=90)
        assert g.eta_days == 90

    def test_eta_is_none_when_nothing_is_accruing(self):
        # Never invent "ETA: 99999 days" out of a zero rate.
        assert _gate(nav_days=0, nav_span_days=0).eta_days is None


class TestAxisVerdict:
    def test_good_needs_both_the_threshold_and_a_positive_interval(self):
        assert axis_verdict(0.8, 0.1, 1.5, 0.5, -0.5) == GOOD

    def test_threshold_cleared_but_interval_straddles_zero_is_flat(self):
        # THE guard from finding 4: at n=90 an IR of 0.5 is t≈0.30. The point estimate alone
        # must never be enough to recommend moving money.
        assert axis_verdict(0.8, -0.2, 1.9, 0.5, -0.5) == FLAT

    def test_interval_excludes_zero_but_effect_too_small_is_flat(self):
        assert axis_verdict(0.1, 0.01, 0.2, 0.5, -0.5) == FLAT

    def test_bad_needs_both(self):
        assert axis_verdict(-0.9, -1.8, -0.1, 0.5, -0.5) == BAD
        assert axis_verdict(-0.9, -1.8, 0.4, 0.5, -0.5) == FLAT

    def test_missing_statistics_are_flat(self):
        assert axis_verdict(None, None, None, 0.5, -0.5) == FLAT


def _exec(axis=FLAT, adj=0.0):
    return ExecutionMetrics(n_obs=100, axis=axis, cum_excess_adj=adj)


def _picks(axis=FLAT, clustered=0.0):
    return PickMetrics(n_picks=100, n_tickers=14, axis=axis, clustered_excess=clustered)


class TestClassify:
    def test_not_ready_beats_everything(self):
        verdict, reasons = classify(_gate(nav_days=13), _exec(GOOD, 0.05), _picks(GOOD, 6.0))
        assert verdict == INSUFFICIENT_DATA
        assert "nav days 13/90" in reasons[0]

    def test_scale_when_both_axes_are_good(self):
        assert classify(_gate(), _exec(GOOD, 0.05), _picks(GOOD, 6.0))[0] == SCALE

    def test_tune_when_picks_good_execution_bad(self):
        assert classify(_gate(), _exec(BAD, -0.05), _picks(GOOD, 6.0))[0] == TUNE

    def test_review_when_picks_bad_regardless_of_execution(self):
        # No dial fixes a conviction problem, so a bad pick axis outranks a good execution one.
        assert classify(_gate(), _exec(GOOD, 0.05), _picks(BAD, -6.0))[0] == REVIEW
        assert classify(_gate(), _exec(BAD, -0.05), _picks(BAD, -6.0))[0] == REVIEW

    def test_steady_when_ready_but_nothing_significant(self):
        assert classify(_gate(), _exec(FLAT), _picks(FLAT))[0] == STEADY

    def test_steady_when_only_execution_is_good(self):
        assert classify(_gate(), _exec(GOOD, 0.05), _picks(FLAT))[0] == STEADY

    def test_insufficient_data_never_returns_once_the_gate_passes(self):
        for e in (GOOD, BAD, FLAT):
            for p in (GOOD, BAD, FLAT):
                assert classify(_gate(), _exec(e), _picks(p))[0] != INSUFFICIENT_DATA


class TestTuneDiagnostic:
    def test_exits_too_early_fingerprint(self):
        trips = [{"return_pct": 1.0}, {"return_pct": 3.0}]
        d = tune_diagnostic(_exec(BAD, -0.05), _picks(GOOD, 8.0), trips)
        assert any(f["name"] == "exits-too-early" for f in d["fingerprints"])

    def test_cash_drag_fingerprint(self):
        execution = ExecutionMetrics(n_obs=100, axis=BAD, cum_excess_raw=-0.04,
                                     cum_excess_adj=0.001, mean_invested_frac=0.65)
        d = tune_diagnostic(execution, _picks(GOOD, 8.0), [])
        assert any(f["name"] == "cash-drag" for f in d["fingerprints"])

    def test_unattributed_when_nothing_matches(self):
        d = tune_diagnostic(_exec(BAD, -0.05), _picks(GOOD, 1.0), [{"return_pct": 9.0}])
        assert [f["name"] for f in d["fingerprints"]] == ["unattributed"]

    def test_never_emits_a_setting_to_apply(self):
        # The doctrine guard: TUNE describes a symptom and hands it to a human. It must never
        # carry a config key paired with a value, which is what an auto-tuner would produce.
        d = tune_diagnostic(
            ExecutionMetrics(n_obs=100, axis=BAD, cum_excess_raw=-0.04, cum_excess_adj=0.001,
                             mean_invested_frac=0.65),
            _picks(GOOD, 8.0), [{"return_pct": 1.0}],
        )
        blob = repr(d)
        for forbidden in ("BRAIN_", "AGENT_", "reserve_floor_pct", "max_deploy_per_run_pct",
                          "cp_buy_threshold", "settings"):
            assert forbidden not in blob


class TestShouldNotify:
    def test_unchanged_verdict_is_silent(self):
        assert should_notify(STEADY, STEADY) is False

    def test_changed_verdict_fires(self):
        assert should_notify(SCALE, STEADY) is True

    def test_first_row_with_nothing_to_say_is_silent(self):
        assert should_notify(INSUFFICIENT_DATA, None) is False

    def test_first_row_with_a_decisive_verdict_fires(self):
        assert should_notify(SCALE, None) is True

    def test_leaving_insufficient_data_fires(self):
        assert should_notify(STEADY, INSUFFICIENT_DATA) is True


class TestExecutionMetricsEndToEnd:
    def test_flat_book_in_a_rising_market_is_not_condemned_for_holding_cash(self):
        # 65%-invested book, market rises steadily, book tracks its own exposure exactly.
        # Raw excess is clearly negative; the cash-adjusted excess is ~0, which is the truth.
        pairs = []
        nav, bench = 100.0, 500.0
        for i in range(40):
            pairs.append(_pair(i, nav, bench, invested=0.65))
            bench *= 1.002
            nav *= 1.0 + 0.65 * 0.002
        m = execution_metrics(pairs, samples=200, beta_adjust=True)
        assert m.cum_excess_raw is not None and m.cum_excess_raw < -0.01
        assert m.cum_excess_adj == pytest.approx(0.0, abs=1e-6)
        assert m.axis == FLAT

    def test_no_observations_returns_an_empty_shape(self):
        m = execution_metrics([], samples=100)
        assert m.n_obs == 0 and m.axis == FLAT and m.ir_annual is None

    def test_ir_at_exactly_the_threshold_over_the_minimum_sample_is_not_scale(self):
        # Finding 4, end to end: a series constructed to sit right at the SCALE bar over the
        # 90-day gate must still come out FLAT, because the interval cannot exclude zero.
        import random as _random

        rng = _random.Random(7)
        target_ir, ppy, n = 0.5, 365.25, 90
        sd = 0.01
        mean = target_ir * sd / (ppy ** 0.5)
        pairs, nav, bench = [], 100.0, 500.0
        for i in range(n + 1):
            pairs.append(_pair(i, nav, bench, invested=1.0))
            bench_ret = 0.0005
            nav_ret = bench_ret + rng.gauss(mean, sd)
            bench *= 1 + bench_ret
            nav *= 1 + nav_ret
        m = execution_metrics(pairs, samples=400, beta_adjust=True,
                              good_threshold=0.5, bad_threshold=-0.5)
        assert m.axis != GOOD
