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
