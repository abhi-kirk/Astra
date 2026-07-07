"""Unit tests for the brain exit stack."""

from src.brain import exit as exit_mod


def _held(**overrides):
    md = {
        "current_price": 100.0, "ma_50": 90.0, "ma_200": 70.0, "rsi_14": 60.0,
        "price_vs_ma50_pct": 11.0, "atr_14": 2.0, "recent_swing_high": 101.0,
        "revenue_growth_yoy": 0.20, "gross_margins": 0.45, "revisions": {"up": 3, "down": 1},
    }
    md.update(overrides)
    return md


_OPP = {"intent": "opportunistic"}
_THESIS = {"intent": "thesis_hold"}
_WRITTEN = {"intent": "written_off"}
_POS = {"shares": 50, "avg_cost": 60.0, "num_buys": 1}


class TestThesisInvalidation:
    def test_revenue_collapse_sells(self):
        ex = exit_mod.evaluate(_POS, _held(revenue_growth_yoy=-0.25), _OPP)
        assert ex.action == "sell" and ex.close_reason == exit_mod.THESIS_INVALIDATION

    def test_margin_collapse_sells(self):
        ex = exit_mod.evaluate(_POS, _held(gross_margins=-0.05), _OPP)
        assert ex.action == "sell"

    def test_revisions_alone_never_sells(self):
        # Analyst revisions are a SOFT signal — even a strong downward cluster must NOT
        # force a sell while fundamentals are intact (RKLB conviction-hold guardrail).
        ex = exit_mod.evaluate(_POS, _held(revisions={"up": 0, "down": 8}), _OPP)
        assert ex.action is None

    def test_thesis_hold_only_layer1(self):
        # Intact thesis_hold winner: no trailing/trim ever, thesis fine → no exit.
        ex = exit_mod.evaluate(_POS, _held(), _THESIS)
        assert ex.action is None
        # but a broken thesis still sells
        ex2 = exit_mod.evaluate(_POS, _held(revenue_growth_yoy=-0.25), _THESIS)
        assert ex2.action == "sell"


class TestTrailingStop:
    def test_winner_at_highs_not_stopped(self):
        # price at the recent high, above 50-MA → Chandelier not tripped
        ex = exit_mod.evaluate(_POS, _held(), _OPP)
        assert ex.action is None

    def test_trend_break_sells(self):
        # price collapsed below both the Chandelier stop and the 50-MA
        md = _held(current_price=80.0, recent_swing_high=110.0, atr_14=3.0, ma_50=95.0)
        ex = exit_mod.evaluate(_POS, md, _OPP)
        assert ex.action == "sell" and ex.close_reason == exit_mod.TRAILING_STOP

    def test_below_stop_but_above_ma_holds(self):
        # dropped below the raw stop but still above the 50-MA → not confirmed, hold
        md = _held(current_price=103.0, recent_swing_high=112.0, atr_14=2.0, ma_50=100.0)
        ex = exit_mod.evaluate(_POS, md, _OPP)
        assert ex.action is None


class TestParabolicTrim:
    def test_parabolic_trims(self):
        md = _held(current_price=150.0, ma_50=100.0, rsi_14=82.0, price_vs_ma50_pct=50.0,
                   atr_14=3.0, recent_swing_high=151.0)
        pos = {"shares": 20, "avg_cost": 60.0, "num_buys": 1}
        ex = exit_mod.evaluate(pos, md, _OPP)
        assert ex.action == "trim"
        assert ex.trim_fraction and 0 < ex.trim_fraction < 1
        assert ex.close_reason == exit_mod.PARABOLIC_TRIM

    def test_calm_winner_not_trimmed(self):
        # big gain but RSI not overbought and not extended → runs, no trim
        md = _held(current_price=150.0, ma_50=140.0, rsi_14=58.0, price_vs_ma50_pct=7.0,
                   atr_14=3.0, recent_swing_high=151.0)
        pos = {"shares": 20, "avg_cost": 60.0, "num_buys": 1}
        ex = exit_mod.evaluate(pos, md, _OPP)
        assert ex.action is None


class TestWrittenOff:
    def test_nudge_only(self):
        ex = exit_mod.evaluate(_POS, _held(), _WRITTEN)
        assert ex.action is None
        assert any("redeploy" in f.lower() for f in ex.risk_flags)
