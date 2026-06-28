"""
Unit tests for src/strategy.py — all pure functions, no I/O.
"""

import pytest

from src.strategy import (
    check_hard_rules,
    check_profit_take,
    get_ticker_guidance,
    is_excluded,
    quality_filter,
    screen_all_positions,
    screen_position,
    technical_signal,
)


# ---------------------------------------------------------------------------
# is_excluded
# ---------------------------------------------------------------------------

class TestIsExcluded:
    def test_excluded_ticker(self, convictions):
        reason = is_excluded("TSLA", convictions)
        assert reason is not None
        assert "blackout" in reason.lower()

    def test_not_excluded(self, convictions):
        assert is_excluded("RKLB", convictions) is None

    def test_empty_exclusions(self):
        assert is_excluded("TSLA", {"exclusions": []}) is None


# ---------------------------------------------------------------------------
# get_ticker_guidance
# ---------------------------------------------------------------------------

class TestGetTickerGuidance:
    def test_individual_holding(self, convictions):
        g = get_ticker_guidance("NIO", convictions)
        assert g["status"] == "hold"
        assert g["theme"] is None
        assert g["hold_only"] is True

    def test_theme_approved(self, convictions):
        g = get_ticker_guidance("RKLB", convictions)
        assert g["status"] == "approved"
        assert g["theme"] == "space"
        assert g["do_not_add"] is False

    def test_theme_do_not_add(self, convictions):
        g = get_ticker_guidance("SPCE", convictions)
        assert g["do_not_add"] is True
        assert g["theme"] == "space"

    def test_core_tech_theme(self, convictions):
        g = get_ticker_guidance("NVDA", convictions)
        assert g["theme"] == "core_tech"
        assert g["do_not_add"] is False

    def test_unknown_ticker(self, convictions):
        g = get_ticker_guidance("XYZ", convictions)
        assert g["status"] == "unknown"
        assert g["theme"] is None


# ---------------------------------------------------------------------------
# check_hard_rules
# ---------------------------------------------------------------------------

class TestCheckHardRules:
    def test_tsla_hard_exclusion(self, convictions, base_position, good_market_data, portfolio_summary):
        result = check_hard_rules("TSLA", base_position, good_market_data, convictions, portfolio_summary)
        assert result is not None
        assert "EXCLUSION" in result

    def test_do_not_add(self, convictions, base_position, good_market_data, portfolio_summary):
        result = check_hard_rules("SPCE", base_position, good_market_data, convictions, portfolio_summary)
        assert result is not None
        assert "DO NOT ADD" in result

    def test_averaging_down_blocked(self, convictions, good_market_data, portfolio_summary):
        # avg_cost=100, current_price=60 → 40% drawdown, 4 buys → blocked
        position = {"shares": 100.0, "avg_cost": 100.0, "num_buys": 4}
        market_data = {**good_market_data, "current_price": 60.0}
        result = check_hard_rules("RKLB", position, market_data, convictions, portfolio_summary)
        assert result is not None
        assert "AVERAGING DOWN" in result

    def test_averaging_down_not_yet_blocked(self, convictions, good_market_data, portfolio_summary):
        # Same drawdown but only 2 buys — below the 3-buy threshold
        position = {"shares": 100.0, "avg_cost": 100.0, "num_buys": 2}
        market_data = {**good_market_data, "current_price": 60.0}
        result = check_hard_rules("RKLB", position, market_data, convictions, portfolio_summary)
        assert result is None

    def test_position_size_limit(self, convictions, good_market_data):
        # position worth $12K in a $100K portfolio = 12% > 10% cap
        position = {"shares": 141.2, "avg_cost": 85.0, "num_buys": 1}  # 141.2 * $85 ≈ $12K
        summary = {"total_value": 100_000.0, "theme_allocations": {"space": 0.10}}
        result = check_hard_rules("RKLB", position, good_market_data, convictions, summary)
        assert result is not None
        assert "POSITION LIMIT" in result

    def test_theme_concentration_blocked(self, convictions, base_position, good_market_data):
        summary = {"total_value": 100_000.0, "theme_allocations": {"space": 0.18}}
        result = check_hard_rules("RKLB", base_position, good_market_data, convictions, summary)
        assert result is not None
        assert "THEME LIMIT" in result

    def test_core_tech_exempt_from_theme_cap(self, convictions, base_position, good_market_data):
        # core_tech at 40% should NOT trigger the theme limit
        summary = {"total_value": 100_000.0, "theme_allocations": {"core_tech": 0.40}}
        result = check_hard_rules("NVDA", base_position, good_market_data, convictions, summary)
        assert result is None

    def test_clean_position_no_block(self, convictions, base_position, good_market_data, portfolio_summary):
        result = check_hard_rules("RKLB", base_position, good_market_data, convictions, portfolio_summary)
        assert result is None


# ---------------------------------------------------------------------------
# quality_filter
# ---------------------------------------------------------------------------

class TestQualityFilter:
    def test_all_passing(self, good_market_data):
        passed, reasons, flags = quality_filter("RKLB", good_market_data)
        assert passed is True
        assert len(reasons) >= 2

    def test_revenue_declining_catastrophic(self):
        data = {"revenue_growth_yoy": -0.05, "gross_margins": 0.55,
                "debt_to_equity": 40.0, "free_cashflow": 100_000}
        passed, _, flags = quality_filter("X", data)
        assert passed is False
        assert any("declining" in f.lower() for f in flags)

    def test_negative_gross_margin_catastrophic(self):
        data = {"revenue_growth_yoy": 0.20, "gross_margins": -0.10,
                "debt_to_equity": 40.0, "free_cashflow": 100_000}
        passed, _, flags = quality_filter("X", data)
        assert passed is False
        assert any("negative gross margin" in f.lower() for f in flags)

    def test_only_one_check_passes(self):
        # Only revenue growth passes; margin failing + high debt + negative FCF
        data = {"revenue_growth_yoy": 0.20, "gross_margins": 0.10,
                "debt_to_equity": 200.0, "free_cashflow": -1_000_000}
        passed, reasons, _ = quality_filter("X", data)
        assert passed is False
        assert len(reasons) < 2

    def test_all_none_missing_data(self):
        data = {"revenue_growth_yoy": None, "gross_margins": None,
                "debt_to_equity": None, "free_cashflow": None}
        passed, reasons, flags = quality_filter("X", data)
        assert passed is False
        assert len(reasons) == 0
        assert all("unavailable" in f for f in flags)


# ---------------------------------------------------------------------------
# technical_signal
# ---------------------------------------------------------------------------

class TestTechnicalSignal:
    def test_both_signals_met(self):
        data = {"pct_below_52w_high": 20.0, "rsi_14": 35.0, "price_vs_ma50_pct": -5.0}
        signal, reasons = technical_signal(data)
        assert signal is True

    def test_only_rsi_met(self):
        data = {"pct_below_52w_high": 5.0, "rsi_14": 35.0, "price_vs_ma50_pct": 0.0}
        signal, _ = technical_signal(data)
        assert signal is False

    def test_only_dip_met(self):
        data = {"pct_below_52w_high": 20.0, "rsi_14": 55.0, "price_vs_ma50_pct": 0.0}
        signal, _ = technical_signal(data)
        assert signal is False

    def test_neither_met(self):
        data = {"pct_below_52w_high": 5.0, "rsi_14": 60.0, "price_vs_ma50_pct": 5.0}
        signal, _ = technical_signal(data)
        assert signal is False

    def test_missing_data(self):
        signal, _ = technical_signal({})
        assert signal is False


# ---------------------------------------------------------------------------
# check_profit_take
# ---------------------------------------------------------------------------

class TestCheckProfitTake:
    def test_triggers_at_65_pct_gain(self):
        position = {"avg_cost": 50.0}
        market_data = {"current_price": 82.5}  # +65%
        sig = check_profit_take("RKLB", position, market_data)
        assert sig is not None
        assert sig["action"] == "sell"

    def test_no_trigger_at_55_pct(self):
        position = {"avg_cost": 50.0}
        market_data = {"current_price": 77.5}  # +55%
        assert check_profit_take("RKLB", position, market_data) is None

    def test_no_trigger_missing_avg_cost(self):
        assert check_profit_take("RKLB", {"avg_cost": 0}, {"current_price": 100.0}) is None

    def test_no_trigger_missing_price(self):
        assert check_profit_take("RKLB", {"avg_cost": 50.0}, {"current_price": 0}) is None


# ---------------------------------------------------------------------------
# screen_position
# ---------------------------------------------------------------------------

class TestScreenPosition:
    def test_tsla_blocked(self, convictions, base_position, good_market_data, portfolio_summary):
        sig = screen_position("TSLA", base_position, good_market_data, convictions, portfolio_summary)
        assert sig["action"] == "blocked"

    def test_nio_hold_only(self, convictions, base_position, good_market_data, portfolio_summary):
        sig = screen_position("NIO", base_position, good_market_data, convictions, portfolio_summary)
        assert sig["action"] == "hold"

    def test_rklb_buy_signal(self, convictions, base_position, good_market_data, portfolio_summary):
        sig = screen_position("RKLB", base_position, good_market_data, convictions, portfolio_summary)
        assert sig["action"] == "buy"
        assert sig["suggested_position_pct"] is not None

    def test_rklb_watch_weak_technicals(self, convictions, base_position, portfolio_summary):
        # Good quality but no technical entry signal
        weak_tech = {
            "current_price": 85.0,
            "revenue_growth_yoy": 0.25,
            "gross_margins": 0.55,
            "debt_to_equity": 40.0,
            "free_cashflow": 500_000_000,
            "pct_below_52w_high": 5.0,   # < 15% threshold
            "rsi_14": 55.0,               # > 40 threshold
            "price_vs_ma50_pct": 2.0,
        }
        sig = screen_position("RKLB", base_position, weak_tech, convictions, portfolio_summary)
        assert sig["action"] == "watch"

    def test_profit_take_overrides_buy(self, convictions, portfolio_summary):
        # Position up 70% → sell even if all other signals say buy
        position = {"shares": 100.0, "avg_cost": 50.0, "num_buys": 1}
        market_data = {
            "current_price": 85.0,  # +70% from 50
            "revenue_growth_yoy": 0.25, "gross_margins": 0.55,
            "debt_to_equity": 40.0, "free_cashflow": 500_000_000,
            "pct_below_52w_high": 20.0, "rsi_14": 35.0, "price_vs_ma50_pct": -12.0,
        }
        sig = screen_position("RKLB", position, market_data, convictions, portfolio_summary)
        assert sig["action"] == "sell"


# ---------------------------------------------------------------------------
# screen_all_positions
# ---------------------------------------------------------------------------

class TestScreenAllPositions:
    def test_sort_order(self, convictions, good_market_data, portfolio_summary):
        portfolio = {
            "TSLA": {"shares": 10.0, "avg_cost": 200.0, "num_buys": 1},
            "NIO":  {"shares": 100.0, "avg_cost": 6.0,  "num_buys": 1},
            "RKLB": {"shares": 50.0,  "avg_cost": 100.0, "num_buys": 1},
        }
        market_data = {t: good_market_data for t in portfolio}
        signals = screen_all_positions(portfolio, market_data, convictions)
        actions = [s["action"] for s in signals]
        priority = {"buy": 0, "sell": 1, "watch": 2, "hold": 3, "blocked": 4}
        assert actions == sorted(actions, key=lambda a: priority.get(a, 5))

    def test_all_tickers_screened(self, convictions, good_market_data):
        portfolio = {"RKLB": {"shares": 50.0, "avg_cost": 100.0, "num_buys": 1},
                     "NVDA": {"shares": 20.0, "avg_cost": 200.0, "num_buys": 1}}
        market_data = {t: good_market_data for t in portfolio}
        signals = screen_all_positions(portfolio, market_data, convictions)
        assert len(signals) == 2
        assert {s["ticker"] for s in signals} == {"RKLB", "NVDA"}
