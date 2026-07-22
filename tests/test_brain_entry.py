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
        g = {"status": "approved", "theme_conviction": "high",
             "hold_only": False, "do_not_add": False, "intent": "opportunistic"}
        d = entry.decide(_md(), g)
        assert d.action == "buy"
        assert d.score_buy >= params.CP_BUY_THRESHOLD

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


class TestConvictionPrimaryGate:
    """OwnScore = C·H — the market never votes on whether to own (docs/conviction_primary.md)."""

    # A conviction name with a healthy business but ugly tape (deep dip, bad revisions, absurd
    # P/E) — the RKLB case. Silenced under the legacy C·S gate; a BUY under conviction-primary.
    def _healthy_business_bad_tape(self):
        return _md(current_price=44.0, ma_50=70.0, ma_200=80.0,  # deep downtrend
                   price_vs_ma50_pct=-37.0, rsi_14=28.0, mom_12_1=-0.5,
                   revenue_growth_yoy=0.63, gross_margins=0.55, debt_to_equity=10.0,
                   current_ratio=2.0, free_cashflow=5e8,
                   forward_pe=2000.0, peg_ratio=None, revisions={"up": 1, "down": 7})

    def test_conviction_primary_buys_intact_thesis_on_bad_tape(self, monkeypatch):
        g = {"status": "approved", "theme_conviction": "very_high",
             "hold_only": False, "do_not_add": False, "intent": "opportunistic"}
        md = self._healthy_business_bad_tape()

        monkeypatch.setattr(params, "CONVICTION_PRIMARY", False)
        assert entry.decide(md, g).action != "buy"  # legacy: silenced by the market

        monkeypatch.setattr(params, "CONVICTION_PRIMARY", True)
        d = entry.decide(md, g)
        assert d.action == "buy"                        # C·H clears — business decides
        assert d.score_buy >= params.CP_BUY_THRESHOLD

    def test_broken_thesis_gated_even_at_top_conviction(self, monkeypatch):
        # Negative gross margin → quality catastrophic-capped (0.15) → the thesis-break veto.
        monkeypatch.setattr(params, "CONVICTION_PRIMARY", True)
        g = {"status": "preferred", "hold_only": False, "do_not_add": False, "intent": "opportunistic"}
        lcid = _md(gross_margins=-0.96, revenue_growth_yoy=0.20, free_cashflow=-3e9)
        d = entry.decide(lcid, g)
        assert d.action == "hold"                     # gated by the business, not the price
        assert d.score_buy < params.WATCH_THRESHOLD

    def test_missing_fundamentals_falls_back_to_neutral_H(self, monkeypatch):
        # ETF/ADR with no company fundamentals → neutral H; conviction (theme label) carries it.
        monkeypatch.setattr(params, "CONVICTION_PRIMARY", True)
        etf = {"current_price": 50.0, "ma_50": 49.0, "ma_200": 48.0, "rsi_14": 50.0,
               "price_vs_ma50_pct": 2.0, "atr_14": 1.0, "recent_swing_high": 52.0}
        g = {"status": "approved", "theme_conviction": "very_high",
             "hold_only": False, "do_not_add": False, "intent": "opportunistic"}
        d = entry.decide(etf, g)
        assert d.score_buy == params.THESIS_NEUTRAL * params.CONVICTION_THEME_WEIGHTS["very_high"]
        # 1.0·0.5 = 0.5 — a no-fundamentals name clears WATCH on conviction but not the selective BUY bar.
        assert d.action == "watch"
