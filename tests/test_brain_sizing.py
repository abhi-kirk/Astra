"""Unit tests for brain sizing + iterative constrained allocation."""

from src.brain import params, sizing


class TestTargetWeight:
    def test_scales_with_score(self):
        md = {"current_price": 100, "atr_14": 3.0}
        low = sizing.target_weight(0.5, md)
        high = sizing.target_weight(0.9, md)
        assert high > low
        assert low <= params.SIZE_MAX_PCT

    def test_high_vol_gets_smaller(self):
        calm = sizing.target_weight(0.7, {"current_price": 100, "atr_14": 2.0})
        wild = sizing.target_weight(0.7, {"current_price": 100, "atr_14": 10.0})
        assert wild < calm

    def test_capped_at_max(self):
        w = sizing.target_weight(1.0, {"current_price": 100, "atr_14": 1.0})
        assert w <= params.SIZE_MAX_PCT

    def test_floor_when_positive(self):
        w = sizing.target_weight(0.5, {"current_price": 100, "atr_14": 10.0})
        assert w == 0.0 or w >= params.SIZE_MIN_PCT

    def test_legacy_ignores_timing(self, monkeypatch):
        # With conviction-primary off, target_weight must not apply the timing multiplier.
        monkeypatch.setattr(params, "CONVICTION_PRIMARY", False)
        assert sizing.timing_multiplier({"current_price": 100, "atr_14": 3.0}) == 1.0


class TestStage2Timing:
    """Under conviction-primary, timing paces the lot (same OwnScore, different deploy)."""

    _good = {"current_price": 92.0, "ma_50": 90.0, "ma_200": 70.0, "rsi_14": 45.0,
             "price_vs_ma50_pct": 2.2, "atr_14": 3.0, "recent_swing_high": 96.0, "mom_12_1": 0.30,
             "peg_ratio": 1.0, "forward_pe": 18.0, "revisions": {"up": 5, "down": 0}}
    _bad = {"current_price": 60.0, "ma_50": 70.0, "ma_200": 80.0, "rsi_14": 68.0,
            "price_vs_ma50_pct": -14.0, "atr_14": 3.0, "recent_swing_high": 96.0, "mom_12_1": -0.30,
            "peg_ratio": 3.0, "forward_pe": 40.0, "revisions": {"up": 0, "down": 4}}

    def test_good_timing_deploys_more_than_bad(self, monkeypatch):
        monkeypatch.setattr(params, "CONVICTION_PRIMARY", True)
        own = 0.6  # same OwnScore (C·H) for both — only timing differs
        assert sizing.target_weight(own, self._good) > sizing.target_weight(own, self._bad)

    def test_multiplier_within_bounds(self, monkeypatch):
        monkeypatch.setattr(params, "CONVICTION_PRIMARY", True)
        for md in (self._good, self._bad, {"current_price": 50.0}):
            mult = sizing.timing_multiplier(md)
            assert params.SIZE_TIMING_MIN <= mult <= params.SIZE_TIMING_MAX

    def test_no_timing_data_is_neutral(self, monkeypatch):
        # No timing pillars → M falls back to neutral 0.5 → multiplier ×1.0 (midpoint of 0.5..1.5).
        monkeypatch.setattr(params, "CONVICTION_PRIMARY", True)
        assert sizing.timing_multiplier({"current_price": 50.0}) == 1.0


class TestAllocate:
    def _buy(self, ticker, score, target, theme=None):
        return {"ticker": ticker, "action": "buy", "score_buy": score,
                "suggested_position_pct": target, "theme": theme, "reasons": []}

    def test_total_deploy_cap_respected(self):
        buys = [self._buy(f"T{i}", 0.9 - i * 0.01, 0.10) for i in range(6)]
        sizing.allocate(buys, {"theme_allocations": {}})
        deployed = sum(b["suggested_position_pct"] or 0 for b in buys if b["action"] == "buy")
        assert deployed <= params.MAX_NEW_DEPLOY_PCT + 1e-9

    def test_theme_cap_respected(self):
        buys = [self._buy("A", 0.9, 0.10, theme="space"),
                self._buy("B", 0.8, 0.10, theme="space")]
        sizing.allocate(buys, {"theme_allocations": {"space": 0.10}})
        space_new = sum(b["suggested_position_pct"] or 0 for b in buys if b["action"] == "buy")
        assert 0.10 + space_new <= params.MAX_THEME_PCT + 1e-9

    def test_core_tech_exempt(self):
        # core_tech already at 40% must not block a new core_tech buy
        buys = [self._buy("NVDA", 0.9, 0.08, theme=params.THEME_CAP_EXEMPT)]
        sizing.allocate(buys, {"theme_allocations": {params.THEME_CAP_EXEMPT: 0.40}})
        assert buys[0]["action"] == "buy"
        assert buys[0]["suggested_position_pct"] > 0

    def test_highest_score_filled_first(self):
        buys = [self._buy("LOW", 0.6, 0.10, theme="space"),
                self._buy("HIGH", 0.95, 0.10, theme="space")]
        sizing.allocate(buys, {"theme_allocations": {"space": 0.12}})  # only 3% theme room
        high = next(b for b in buys if b["ticker"] == "HIGH")
        low = next(b for b in buys if b["ticker"] == "LOW")
        assert high["action"] == "buy"          # top score gets the scarce room
        assert low["action"] == "watch"         # nothing left → deferred

    def test_no_room_downgrades_to_watch(self):
        buys = [self._buy("X", 0.7, 0.10, theme="space")]
        sizing.allocate(buys, {"theme_allocations": {"space": params.MAX_THEME_PCT}})
        assert buys[0]["action"] == "watch"
        assert buys[0]["suggested_position_pct"] is None


class TestReservationAllocate:
    """Conviction-primary: a min-reservation pass keeps high-score megacaps from eating the
    whole deploy budget and crowding a genuine conviction out to watch."""

    def _buy(self, ticker, score, target, theme=None):
        return {"ticker": ticker, "action": "buy", "score_buy": score,
                "suggested_position_pct": target, "theme": theme, "reasons": []}

    def test_low_scorer_keeps_a_starter_lot(self, monkeypatch):
        monkeypatch.setattr(params, "CONVICTION_PRIMARY", True)
        # Three big megacaps at full target would exhaust the 25% deploy cap in legacy mode; a
        # lower-scored conviction must still get at least a floor lot here.
        buys = [self._buy("NVDA", 1.00, 0.10), self._buy("MSFT", 0.84, 0.10),
                self._buy("AMZN", 0.73, 0.10), self._buy("RKLB", 0.48, 0.10)]
        sizing.allocate(buys, {"theme_allocations": {}})
        rklb = next(b for b in buys if b["ticker"] == "RKLB")
        assert rklb["action"] == "buy"
        assert rklb["suggested_position_pct"] >= params.SIZE_MIN_PCT

    def test_total_cap_still_respected(self, monkeypatch):
        monkeypatch.setattr(params, "CONVICTION_PRIMARY", True)
        buys = [self._buy(f"T{i}", 0.9 - i * 0.05, 0.10) for i in range(5)]
        sizing.allocate(buys, {"theme_allocations": {}})
        deployed = sum(b["suggested_position_pct"] or 0 for b in buys if b["action"] == "buy")
        assert deployed <= params.MAX_NEW_DEPLOY_PCT + 1e-9

    def test_strongest_still_fills_most(self, monkeypatch):
        monkeypatch.setattr(params, "CONVICTION_PRIMARY", True)
        buys = [self._buy("NVDA", 1.00, 0.10), self._buy("RKLB", 0.48, 0.10)]
        sizing.allocate(buys, {"theme_allocations": {}})
        nvda = next(b for b in buys if b["ticker"] == "NVDA")
        rklb = next(b for b in buys if b["ticker"] == "RKLB")
        assert nvda["suggested_position_pct"] >= rklb["suggested_position_pct"]
