"""
Tests for src/fundamentals — SEC EDGAR XBRL extraction, cache read staleness, and the
yfinance overlay. All XBRL parsing is exercised against fixture companyfacts JSON (no
network); the cache/merge paths are mocked at the DB boundary.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src import config, fundamentals


def _dur(start: str, end: str, val: float, form: str = "10-Q") -> dict:
    return {"start": start, "end": end, "val": val, "form": form}


def _inst(end: str, val: float) -> dict:
    return {"end": end, "val": val, "form": "10-Q"}


def _facts(**tags) -> dict:
    """Build a companyfacts-shaped dict: tag -> list of USD rows."""
    return {"facts": {"us-gaap": {t: {"units": {"USD": rows}} for t, rows in tags.items()}}}


# 8 clean quarters of revenue (each ≈91 days) growing 100→170, plus matching gross profit.
def _eight_quarters(tag_base: float, step: float) -> list[dict]:
    ends = ["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
            "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]
    starts = ["2024-01-01", "2024-04-01", "2024-07-01", "2024-10-01",
              "2025-01-01", "2025-04-01", "2025-07-01", "2025-10-01"]
    return [_dur(s, e, tag_base + i * step) for i, (s, e) in enumerate(zip(starts, ends))]


class TestQuarterlySeries:
    def test_keeps_only_quarterly_durations(self):
        gaap = {"Revenues": {"units": {"USD": [
            _dur("2025-01-01", "2025-03-31", 100),   # ~89d quarter — kept
            _dur("2025-01-01", "2025-09-30", 300),   # ~272d YTD — dropped
        ]}}}
        series = fundamentals._quarterly_series(gaap, ["Revenues"])
        assert series == [("2025-03-31", 100.0)]

    def test_dedups_by_end_date_last_wins(self):
        gaap = {"Revenues": {"units": {"USD": [
            _dur("2025-01-01", "2025-03-31", 100),
            _dur("2025-01-01", "2025-03-31", 110),   # restatement, same end
        ]}}}
        assert fundamentals._quarterly_series(gaap, ["Revenues"]) == [("2025-03-31", 110.0)]

    def test_ignores_unparseable_dates(self):
        gaap = {"Revenues": {"units": {"USD": [_dur("bad", "date", 100)]}}}
        assert fundamentals._quarterly_series(gaap, ["Revenues"]) == []


class TestTagMerge:
    def test_falls_back_to_generic_tag_when_specific_empty(self):
        gaap = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": []}},
            "Revenues": {"units": {"USD": [_dur("2025-01-01", "2025-03-31", 42)]}},
        }
        assert fundamentals._quarterly_series(gaap, fundamentals._REVENUE_TAGS) == [("2025-03-31", 42.0)]

    def test_merges_across_tag_migration_keeping_full_history(self):
        # Specific tag holds old quarters, generic tag holds recent ones (NVDA/GOOGL pattern).
        gaap = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
                _dur("2020-01-01", "2020-03-31", 100),
            ]}},
            "Revenues": {"units": {"USD": [
                _dur("2026-01-01", "2026-03-31", 900),
            ]}},
        }
        series = fundamentals._quarterly_series(gaap, fundamentals._REVENUE_TAGS)
        assert series == [("2020-03-31", 100.0), ("2026-03-31", 900.0)]

    def test_preferred_tag_wins_on_same_period(self):
        gaap = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
                _dur("2025-01-01", "2025-03-31", 100),
            ]}},
            "Revenues": {"units": {"USD": [
                _dur("2025-01-01", "2025-03-31", 999),   # same period, less-specific tag
            ]}},
        }
        assert fundamentals._quarterly_series(gaap, fundamentals._REVENUE_TAGS) == [("2025-03-31", 100.0)]


class TestExtractFundamentals:
    def test_full_extraction_matches_yfinance_semantics(self):
        facts = _facts(
            RevenueFromContractWithCustomerExcludingAssessedTax=_eight_quarters(100, 10),
            GrossProfit=_eight_quarters(40, 4),
            OperatingIncomeLoss=_eight_quarters(10, 1),
            AssetsCurrent=[_inst("2025-12-31", 500)],
            LiabilitiesCurrent=[_inst("2025-12-31", 250)],
            StockholdersEquity=[_inst("2025-12-31", 1000)],
            LongTermDebt=[_inst("2025-12-31", 300)],
        )
        out = fundamentals.extract_fundamentals(facts)
        # last 4 quarters revenue = 140+150+160+170 = 620; prior 4 = 100+110+120+130 = 460
        assert out["revenue_ttm"] == 620
        assert abs(out["revenue_growth_yoy"] - (620 / 460 - 1)) < 1e-9
        # gross profit last 4 quarters = 56+60+64+68 = 248 → margin 248/620
        assert abs(out["gross_margins"] - 248 / 620) < 1e-9
        assert abs(out["current_ratio"] - 2.0) < 1e-9
        # debt/equity percentage-scaled: 300/1000*100 = 30
        assert abs(out["debt_to_equity"] - 30.0) < 1e-9
        assert out["period_end"] == "2025-12-31"

    def test_falls_back_to_annual_growth_when_few_quarters(self):
        facts = _facts(Revenues=[
            _dur("2023-01-01", "2023-12-31", 1000),   # FY
            _dur("2024-01-01", "2024-12-31", 1200),   # FY
        ])
        out = fundamentals.extract_fundamentals(facts)
        assert abs(out["revenue_growth_yoy"] - 0.2) < 1e-9

    def test_margin_none_when_gross_profit_periods_dont_match_revenue(self):
        # Amazon pattern: GrossProfit tagged only in old years, revenue is current → no overlap.
        facts = _facts(
            Revenues=_eight_quarters(100, 10),   # 2024-2025 quarters
            GrossProfit=[_dur("2009-01-01", "2009-03-31", 40),
                         _dur("2009-04-01", "2009-06-30", 40),
                         _dur("2009-07-01", "2009-09-30", 40),
                         _dur("2009-10-01", "2009-12-31", 40)],
        )
        out = fundamentals.extract_fundamentals(facts)
        assert out["revenue_growth_yoy"] is not None   # revenue still works
        assert out["gross_margins"] is None            # no period-matched gross profit

    def test_missing_tags_yield_none_not_crash(self):
        out = fundamentals.extract_fundamentals(_facts())
        assert out["revenue_growth_yoy"] is None
        assert out["gross_margins"] is None
        assert out["debt_to_equity"] is None

    def test_negative_equity_leaves_debt_to_equity_none(self):
        facts = _facts(
            StockholdersEquity=[_inst("2025-12-31", -50)],
            LongTermDebt=[_inst("2025-12-31", 300)],
        )
        assert fundamentals.extract_fundamentals(facts)["debt_to_equity"] is None


class TestResolveCik:
    def test_pads_to_ten_digits(self):
        with patch.object(fundamentals, "get_cik_map", return_value={"RKLB": "0001819994"}):
            assert fundamentals.resolve_cik("rklb") == "0001819994"

    def test_unknown_ticker_returns_none(self):
        with patch.object(fundamentals, "get_cik_map", return_value={}):
            assert fundamentals.resolve_cik("BYDDY") is None


class TestFetchSecFundamentals:
    def test_returns_none_for_uncovered_ticker(self):
        with patch.object(fundamentals, "resolve_cik", return_value=None):
            assert fundamentals.fetch_sec_fundamentals("BYDDY") is None

    def test_returns_none_on_fetch_error(self):
        with patch.object(fundamentals, "resolve_cik", return_value="0001819994"), \
             patch.object(fundamentals, "fetch_company_facts", side_effect=RuntimeError("503")):
            assert fundamentals.fetch_sec_fundamentals("RKLB") is None

    def test_stamps_metadata(self):
        facts = _facts(Revenues=_eight_quarters(100, 10))
        with patch.object(fundamentals, "resolve_cik", return_value="0001819994"), \
             patch.object(fundamentals, "fetch_company_facts", return_value=facts):
            out = fundamentals.fetch_sec_fundamentals("rklb")
        assert out is not None
        assert out["ticker"] == "RKLB" and out["cik"] == "0001819994"
        assert out["source"] == "sec_edgar" and out["fetched_at"]


class TestGetCachedFundamentals:
    def _rows(self, ts):
        return [{"ticker": "RKLB", "gross_margins": 0.35, "fetched_at": ts}]

    def test_fresh_row_returned(self):
        ts = datetime.now(timezone.utc).isoformat()
        with patch("src.db.get_client") as gc:
            gc.return_value.table.return_value.select.return_value.in_.return_value.execute.return_value.data = self._rows(ts)
            out = fundamentals.get_cached_fundamentals(["RKLB"])
        assert out["RKLB"]["gross_margins"] == 0.35

    def test_stale_row_dropped(self):
        old = config.market_data.fundamentals_max_age_days + 5
        ts = (datetime.now(timezone.utc) - timedelta(days=old)).isoformat()
        with patch("src.db.get_client") as gc:
            gc.return_value.table.return_value.select.return_value.in_.return_value.execute.return_value.data = self._rows(ts)
            assert fundamentals.get_cached_fundamentals(["RKLB"]) == {}

    def test_empty_tickers_no_db_call(self):
        assert fundamentals.get_cached_fundamentals([]) == {}

    def test_db_failure_degrades_to_empty(self):
        with patch("src.db.get_client", side_effect=RuntimeError("no db")):
            assert fundamentals.get_cached_fundamentals(["RKLB"]) == {}


class TestMergeIntoMarketData:
    def test_sec_overlays_present_fields_only(self):
        market = {"RKLB": {"ticker": "RKLB", "gross_margins": None, "revenue_growth_yoy": 0.05}}
        cached = {"RKLB": {"gross_margins": 0.35, "revenue_growth_yoy": None, "debt_to_equity": 10.0}}
        out = fundamentals.merge_into_market_data(market, cached)
        assert out["RKLB"]["gross_margins"] == 0.35        # SEC filled the None
        assert out["RKLB"]["revenue_growth_yoy"] == 0.05   # SEC None → yfinance kept
        assert out["RKLB"]["debt_to_equity"] == 10.0
        assert out["RKLB"]["fundamentals_source"] == "sec_edgar"

    def test_skips_errored_rows(self):
        market = {"RKLB": {"ticker": "RKLB", "error": "no_data"}}
        out = fundamentals.merge_into_market_data(market, {"RKLB": {"gross_margins": 0.35}})
        assert "fundamentals_source" not in out["RKLB"]

    def test_no_cache_row_is_noop(self):
        market = {"AAPL": {"ticker": "AAPL", "gross_margins": 0.44}}
        out = fundamentals.merge_into_market_data(market, {})
        assert out["AAPL"]["gross_margins"] == 0.44
        assert "fundamentals_source" not in out["AAPL"]


class TestRefreshFundamentals:
    def test_skips_uncovered_and_empty_upserts_rest(self):
        fresh = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
        good = {"ticker": "RKLB", "gross_margins": 0.35, "revenue_growth_yoy": 0.2, "period_end": fresh}
        empty = {"ticker": "ASTS", "gross_margins": None, "revenue_growth_yoy": None,
                 "operating_margins": None, "current_ratio": None, "debt_to_equity": None}

        def fake_fetch(t):
            return {"RKLB": good, "ASTS": empty, "BYDDY": None}[t]

        with patch.object(fundamentals, "fetch_sec_fundamentals", side_effect=fake_fetch), \
             patch("src.db.get_client") as gc:
            summary = fundamentals.refresh_fundamentals(["RKLB", "ASTS", "BYDDY"])
            upsert = gc.return_value.table.return_value.upsert
        assert summary["refreshed"] == ["RKLB"]
        assert set(summary["skipped"]) == {"ASTS", "BYDDY"}
        # only the row with real data is persisted
        assert upsert.call_args[0][0] == [good]

    def test_skips_row_with_stale_reporting_period(self):
        # Freshly fetched, has metrics, but the latest quarter is years old (ASTS-like).
        stale = {"ticker": "ASTS", "gross_margins": 0.1, "revenue_growth_yoy": 0.9,
                 "debt_to_equity": 5943.0, "period_end": "2023-03-31"}
        with patch.object(fundamentals, "fetch_sec_fundamentals", return_value=stale), \
             patch("src.db.get_client") as gc:
            summary = fundamentals.refresh_fundamentals(["ASTS"])
            gc.return_value.table.return_value.upsert.assert_not_called()
        assert summary["skipped"] == ["ASTS"] and summary["refreshed"] == []


class TestPeriodStaleness:
    def test_recent_period_not_stale(self):
        recent = (datetime.now(timezone.utc) - timedelta(days=40)).date().isoformat()
        assert fundamentals._period_is_stale(recent) is False

    def test_old_period_is_stale(self):
        assert fundamentals._period_is_stale("2023-03-31") is True

    def test_missing_or_bad_period_is_stale(self):
        assert fundamentals._period_is_stale(None) is True
        assert fundamentals._period_is_stale("not-a-date") is True
