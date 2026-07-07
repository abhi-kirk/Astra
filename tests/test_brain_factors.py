"""Unit tests for brain normalization + factor pillars."""

from src.brain import factors, regime
from src.brain.normalize import clamp, mean_defined, smooth, squash, tent


class TestNormalize:
    def test_smooth_ramp(self):
        assert smooth(0, 0, 10) == 0.0
        assert smooth(10, 0, 10) == 1.0
        assert smooth(5, 0, 10) == 0.5
        assert smooth(-1, 0, 10) == 0.0  # clamped

    def test_smooth_descending(self):
        # a > b : descends
        assert smooth(100, 300, 100) == 1.0
        assert smooth(300, 300, 100) == 0.0

    def test_tent_sweet_spot(self):
        assert tent(0, 1, 3, 5) == 0.0
        assert tent(1, 1, 3, 5) == 1.0
        assert tent(2, 1, 3, 5) == 1.0   # inside plateau
        assert tent(3, 1, 3, 5) == 1.0
        assert tent(5, 1, 3, 5) == 0.0
        assert tent(4, 1, 3, 5) == 0.5

    def test_squash_bounds(self):
        assert squash(0, 0.25) == 0.0
        assert 0 < squash(0.25, 0.25) < 1
        assert squash(100, 0.25) <= 1.0  # tanh saturates to 1.0

    def test_mean_defined(self):
        assert mean_defined([None, None]) is None
        assert mean_defined([1.0, None, 0.0]) == 0.5

    def test_clamp(self):
        assert clamp(5, 0, 1) == 1
        assert clamp(-5, 0, 1) == 0


class TestQuality:
    def test_strong(self):
        md = {"revenue_growth_yoy": 0.3, "gross_margins": 0.6, "debt_to_equity": 30,
              "current_ratio": 1.8, "free_cashflow": 1e8}
        f = factors.quality(md)
        assert f.value is not None and f.value > 0.8

    def test_catastrophic_capped(self):
        md = {"revenue_growth_yoy": -0.3, "gross_margins": 0.6, "free_cashflow": 1e8}
        f = factors.quality(md)
        assert f.value is not None and f.value <= 0.15

    def test_all_missing(self):
        assert factors.quality({}).value is None


class TestValuation:
    def test_missing_is_neutral(self):
        assert factors.valuation({}).value == 0.5

    def test_cheap(self):
        f = factors.valuation({"peg_ratio": 1.0, "forward_pe": 15.0})
        assert f.value is not None and f.value > 0.9

    def test_expensive(self):
        f = factors.valuation({"peg_ratio": 3.0, "forward_pe": 40.0})
        assert f.value is not None and f.value < 0.1


class TestTrend:
    def test_uptrend_positive(self):
        md = {"current_price": 100, "ma_50": 90, "ma_200": 70, "mom_12_1": 0.3, "price_vs_ma50_pct": 11}
        assert regime.classify(md) == regime.UPTREND
        assert factors.trend(md).value > 0

    def test_downtrend_negative(self):
        md = {"current_price": 60, "ma_50": 70, "ma_200": 80, "mom_12_1": -0.3, "price_vs_ma50_pct": -14}
        assert regime.classify(md) == regime.DOWNTREND
        assert factors.trend(md).value < 0

    def test_no_price(self):
        assert factors.trend({}).value is None


class TestEntry:
    def test_healthy_pullback_scores(self):
        md = {"current_price": 92, "ma_50": 90, "ma_200": 70, "rsi_14": 45,
              "price_vs_ma50_pct": 2.2, "atr_14": 2.0, "recent_swing_high": 96}
        assert factors.entry(md).value > 0.5

    def test_no_data(self):
        # price alone (no atr/swing/rsi/ma) → no entry-timing signal
        assert factors.entry({"current_price": 10}).value is None
        assert factors.entry({}).value is None
        # rsi present → a score emerges
        assert factors.entry({"current_price": 10, "rsi_14": 45}).value is not None


class TestRevisions:
    def test_no_coverage(self):
        assert factors.revisions({}).value is None
        assert factors.revisions({"revisions": {"up": 0, "down": 0}}).value is None

    def test_positive(self):
        assert factors.revisions({"revisions": {"up": 5, "down": 1}}).value > 0

    def test_negative_lowers_score(self):
        assert factors.revisions({"revisions": {"up": 0, "down": 4}}).value < 0
