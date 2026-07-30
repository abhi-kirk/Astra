"""
Tests for the I/O half of src/scorecard.py — reading the record, persisting the verdict.

The most important test in this file is `test_writes_only_scorecard_runs`: the module's core
promise is that it can observe the trading system but never influence it, and that promise is
worth an executable assertion rather than a comment. The fake client below records every
mutating call by table so the contract is checked, not assumed.
"""

from datetime import date, timedelta
from unittest.mock import patch

import pytest

from src import config, scorecard

D0 = date(2026, 1, 5)
_WRITE_METHODS = {"insert", "update", "upsert", "delete"}


class _Chain:
    """A Supabase query builder stand-in: every method returns self, `.data` is the payload.

    Mutating calls are recorded against the table they were made on, which is what lets the
    read-only contract be asserted directly.
    """

    def __init__(self, table: str, writes: list, data: list):
        self.__dict__["_table"] = table
        self.__dict__["_writes"] = writes
        self.__dict__["data"] = data

    def __getattr__(self, name):
        if name in _WRITE_METHODS:
            self._writes.append((self._table, name))
        return self

    def __call__(self, *args, **kwargs):
        return self


class _FakeClient:
    def __init__(self, data_by_table: dict[str, list]):
        self._data = data_by_table
        self.writes: list[tuple[str, str]] = []
        self.reads: list[str] = []

    def table(self, name: str) -> _Chain:
        self.reads.append(name)
        return _Chain(name, self.writes, self._data.get(name, []))


def _bench_map(n: int = 200) -> dict[date, float]:
    """A benchmark that rises steadily, so the book is never flattered by a falling market."""
    return {D0 + timedelta(days=i): 500.0 * (1.001 ** i) for i in range(n)}


def _paper_equity(n: int) -> list[dict]:
    return [
        {
            "snapshot_time": f"{(D0 + timedelta(days=i)).isoformat()}T13:00:00+00:00",
            "nav_index": 100.0 * (1.0005 ** i),
            "total_equity": 10_000.0,
            "market_value": 10_000.0,
        }
        for i in range(n)
    ]


def _closed_trades(n: int) -> list[dict]:
    return [
        {"ticker": f"T{i}", "is_open": False, "price_at_signal": 100.0,
         "close_price": 110.0, "virtual_shares": 1.0}
        for i in range(n)
    ]


def _outcomes(rows: int, tickers: int, weeks: int = 1) -> list[dict]:
    out = []
    for i in range(rows):
        run_day = D0 + timedelta(days=7 * (i % weeks))
        out.append({
            "decision_id": i,
            "outcome_date": (run_day + timedelta(days=31)).isoformat(),
            "pct_change": 3.0,
            "notes": "30d outcome",
            "decisions": {
                "ticker": f"T{i % tickers}", "action": "buy",
                "run_date": run_day.isoformat(), "price_at_decision": 100.0,
            },
        })
    return out


@pytest.fixture(autouse=True)
def _clear_benchmark_cache():
    scorecard._SPY_CACHE.clear()
    yield
    scorecard._SPY_CACHE.clear()


@pytest.fixture
def fake_db():
    """A client preloaded with roughly today's real data shape: a short NAV series, a handful
    of closed trades, and many outcome rows concentrated on very few names and one week."""
    client = _FakeClient({
        "paper_equity": _paper_equity(13),
        "agent_account_snapshots": _paper_equity(27),
        "paper_trades": _closed_trades(8),
        "agent_trades": [],
        "outcomes": _outcomes(329, tickers=14, weeks=1),
        "scorecard_runs": [],
    })
    with patch("src.memory.get_client", return_value=client), \
         patch.object(scorecard, "_benchmark_by_day", return_value=(_bench_map(), "yfinance")):
        yield client


class TestEvaluateTrack:
    def test_today_is_insufficient_data_with_the_deficits_named(self, fake_db):
        result = scorecard.evaluate_track(scorecard.PAPER)
        assert result["verdict"] == scorecard.INSUFFICIENT_DATA
        assert result["ready"] is False
        assert result["missing"]
        # 13 snapshots is nowhere near the 90-day bar, and 8 closed trades nowhere near 20.
        assert any(m.startswith("nav days") for m in result["missing"])
        assert any(m.startswith("finished trades") for m in result["missing"])

    def test_row_volume_alone_does_not_clear_the_gate(self, fake_db):
        # 329 outcome rows clear min_matured_decisions=60 outright. They must still fail on
        # calendar diversity, because they sit on one week — this is the whole point of the
        # extra gate dimensions.
        result = scorecard.evaluate_track(scorecard.PAPER)
        assert result["matured_decisions"] == 329
        assert result["distinct_tickers"] == 14
        assert result["distinct_decision_weeks"] == 1
        assert any(m.startswith("different weeks") for m in result["missing"])

    def test_both_tracks_are_evaluated_from_their_own_series(self, fake_db):
        paper = scorecard.evaluate_track(scorecard.PAPER)
        auto = scorecard.evaluate_track(scorecard.AUTOTRADER)
        assert paper["nav_days"] < auto["nav_days"]  # 13 vs 27 snapshots

    def test_as_of_replay_truncates_the_series(self, fake_db):
        full = scorecard.evaluate_track(scorecard.PAPER)
        replay = scorecard.evaluate_track(scorecard.PAPER,
                                          as_of=(D0 + timedelta(days=5)).isoformat())
        assert replay["nav_days"] < full["nav_days"]
        assert replay["as_of_date"] <= (D0 + timedelta(days=5)).isoformat()


class TestReadOnlyContract:
    def test_writes_only_scorecard_runs(self, fake_db):
        scorecard.run_scorecard(notify=False)
        assert fake_db.writes, "expected the verdict to be persisted"
        written_tables = {table for table, _ in fake_db.writes}
        assert written_tables == {"scorecard_runs"}

    def test_reads_do_not_touch_the_control_or_order_tables(self, fake_db):
        scorecard.run_scorecard(notify=False)
        forbidden = {"agent_control", "settings", "convictions", "plays"}
        assert forbidden.isdisjoint(set(fake_db.reads))

    def test_nothing_is_written_when_persist_is_false(self, fake_db):
        scorecard.run_scorecard(persist=False, notify=False)
        assert fake_db.writes == []


class TestRunScorecard:
    def test_disabled_is_a_no_op(self, fake_db):
        config.scorecard.enabled = False
        assert scorecard.run_scorecard() == []
        assert fake_db.writes == []

    def test_one_broken_track_does_not_block_the_other(self, fake_db):
        real = scorecard.evaluate_track

        def flaky(track, **kwargs):
            if track == scorecard.PAPER:
                raise RuntimeError("paper read exploded")
            return real(track, **kwargs)

        with patch.object(scorecard, "evaluate_track", side_effect=flaky):
            results = scorecard.run_scorecard(notify=False)
        assert [r["track"] for r in results] == [scorecard.AUTOTRADER]

    def test_evaluation_failure_never_propagates(self, fake_db):
        with patch.object(scorecard, "evaluate_track", side_effect=RuntimeError("boom")):
            assert scorecard.run_scorecard(notify=False) == []


class TestNotification:
    def test_silent_when_the_verdict_is_unchanged(self, fake_db):
        fake_db._data["scorecard_runs"] = [{"verdict": scorecard.INSUFFICIENT_DATA}]
        with patch("src.notify.notify_scorecard") as sender:
            scorecard.run_scorecard()
        sender.assert_not_called()

    def test_silent_on_a_first_row_with_nothing_to_say(self, fake_db):
        with patch("src.notify.notify_scorecard") as sender:
            results = scorecard.run_scorecard()
        sender.assert_not_called()
        assert all(r["notified"] is False for r in results)

    def test_fires_when_leaving_insufficient_data(self, fake_db):
        fake_db._data["scorecard_runs"] = [{"verdict": scorecard.STEADY}]
        with patch("src.notify.notify_scorecard") as sender:
            scorecard.run_scorecard()
        assert sender.call_count == 2  # both tracks transitioned STEADY → INSUFFICIENT_DATA

    def test_notify_disabled_still_records_the_verdict(self, fake_db):
        config.scorecard.notify_enabled = False
        fake_db._data["scorecard_runs"] = [{"verdict": scorecard.STEADY}]
        with patch("src.notify.notify_scorecard") as sender:
            scorecard.run_scorecard()
        sender.assert_not_called()
        assert {t for t, _ in fake_db.writes} == {"scorecard_runs"}

    def test_persist_happens_before_notify(self, fake_db):
        # If Telegram fails after the row is stored the transition is merely unannounced;
        # notifying first and failing to store would re-fire the same alert every day.
        order: list[str] = []
        fake_db._data["scorecard_runs"] = [{"verdict": scorecard.STEADY}]
        with patch("src.memory.save_scorecard_run", side_effect=lambda row: order.append("persist")), \
             patch("src.notify.notify_scorecard", side_effect=lambda *a: order.append("notify")):
            scorecard.run_scorecard()
        assert order[:2] == ["persist", "notify"]


class TestBenchmarkSource:
    def test_falls_back_to_the_stored_series_when_yfinance_fails(self):
        rows = [{"price_date": (D0 + timedelta(days=i)).isoformat(), "close": 500.0 + i}
                for i in range(11)]
        with patch("yfinance.Ticker", side_effect=RuntimeError("rate limited")), \
             patch("src.memory.get_benchmark_series", return_value=rows):
            series, source = scorecard._benchmark_by_day("SPY", 400)
        assert source == "benchmark_prices"
        assert series[D0] == 500.0

    def test_result_is_memoized_per_process(self):
        rows = [{"price_date": D0.isoformat(), "close": 500.0}]
        with patch("yfinance.Ticker", side_effect=RuntimeError("boom")), \
             patch("src.memory.get_benchmark_series", return_value=rows) as reader:
            scorecard._benchmark_by_day("SPY", 400)
            scorecard._benchmark_by_day("SPY", 400)
        assert reader.call_count == 1
