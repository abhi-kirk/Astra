"""Unit tests for brain entry composite + decision."""

from src.brain import entry, params


def _md(**overrides):
    base = {
        "current_price": 92.0, "ma_50": 90.0, "ma_200": 70.0, "rsi_14": 45.0,
        "price_vs_ma50_pct": 2.2, "atr_14": 2.0, "recent_swing_high": 96.0, "mom_12_1": 0.30,
        "revenue_growth_yoy": 0.25, "gross_margins": 0.55, "debt_to_equity": 30.0,
        "current_ratio": 1.6, "free_cashflow": 8e8, "peg_ratio": 1.0, "forward_pe": 18.0,
        "revisions": {"up": 5, "down": 0},
    }
    base.update(overrides)
    return base


class TestComposite:
    def test_renormalizes_over_available(self):
        # With no revisions coverage, the composite must still be well-formed (0..1-ish).
        comp = entry.compute_composite(_md(revisions=None))
        assert "revisions" not in comp.pillars
        assert 0.0 <= comp.score <= 1.0

    def test_trend_only_positive_part(self):
        # A downtrend should not add a negative trend term beyond removing the tailwind.
        down = _md(current_price=60, ma_50=70, ma_200=80, price_vs_ma50_pct=-14, mom_12_1=-0.3)
        comp = entry.compute_composite(down)
        assert comp.pillars["trend"] == 0.0  # max(T, 0)


class TestDecide:
    def test_strong_pullback_buys(self, convictions):
        g = {"status": "approved", "hold_only": False, "do_not_add": False, "intent": "opportunistic"}
        d = entry.decide(_md(), g)
        assert d.action == "buy"
        assert d.score_buy >= params.BUY_THRESHOLD

    def test_hold_only_never_buys(self):
        g = {"status": "hold", "hold_only": True, "do_not_add": False, "intent": "opportunistic"}
        d = entry.decide(_md(), g)
        assert d.action == "hold"

    def test_unknown_conviction_holds(self):
        g = {"status": "unknown", "hold_only": False, "do_not_add": False, "intent": "opportunistic"}
        d = entry.decide(_md(), g)
        assert d.conviction_weight == 0.0
        assert d.action == "hold"

    def test_weak_setup_watch_or_hold(self):
        g = {"status": "approved", "hold_only": False, "do_not_add": False, "intent": "opportunistic"}
        weak = _md(revenue_growth_yoy=0.02, gross_margins=0.12, mom_12_1=-0.1,
                   peg_ratio=3.0, forward_pe=40.0, revisions={"up": 0, "down": 3},
                   rsi_14=68.0, price_vs_ma50_pct=9.0)
        d = entry.decide(weak, g)
        assert d.action in ("watch", "hold")
        assert d.action != "buy"
