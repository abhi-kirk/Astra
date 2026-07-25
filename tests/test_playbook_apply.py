"""
Unit tests for src/brain/playbook.py apply_proposals — the deterministic, invariant-guarded applier
that turns bounded proposals into signal changes. This is the highest-risk piece: a conviction_nudge
can re-gate a name, so the hard invariants (excluded / do_not_add / hard-block / caps) must hold.
"""

import copy

import pytest

from src.brain import params, playbook

# A strong-quality market_data (H ≈ 0.94), so C·H lands predictably around the gate.
GOOD_MD = {
    "current_price": 85.0, "revenue_growth_yoy": 0.25, "gross_margins": 0.55,
    "debt_to_equity": 40.0, "current_ratio": 1.6, "free_cashflow": 5e8, "peg_ratio": 1.0,
    "forward_pe": 15.0, "pct_below_52w_high": 20.0, "rsi_14": 35.0, "price_vs_ma50_pct": -3.0,
    "ma_50": 87.6, "ma_200": 70.0, "atr_14": 3.0, "recent_swing_high": 95.0,
    "mom_12_1": 0.30, "revisions": {"up": 4, "down": 1},
}


@pytest.fixture
def convs():
    # ENPH → medium theme (C=0.6): C·H≈0.565 → a WATCH that a +0.08 nudge lifts over the 0.60 buy bar.
    # PLTR → high theme (C=0.8): C·H≈0.753 → a BUY (used for size tests).
    # BADCO → do_not_add. TSLA → excluded.
    return {
        "exclusions": [{"ticker": "TSLA", "reason": "blackout"}],
        "individual_holdings": {},
        "themes": {
            "solar": {"conviction": "medium", "approved": ["ENPH"], "preferred": [],
                      "hold_only": [], "do_not_add": ["BADCO"], "notes": {}},
            "ai": {"conviction": "high", "approved": ["PLTR"], "preferred": [],
                   "hold_only": [], "do_not_add": [], "notes": {}},
        },
        "ticker_metadata": {},
    }


def _sig(ticker, action, theme, **kw):
    base = {"ticker": ticker, "action": action, "theme": theme, "source": "position_screen",
            "reasons": [], "risk_flags": [], "suggested_position_pct": None, "score_buy": None}
    base.update(kw)
    return base


def _p(ticker, kind, value, play="macro_dip"):
    return {"ticker": ticker, "play": play, "kind": kind, "value": value,
            "rationale": "", "evidence": {}, "source": "quant", "applied": None}


class TestConvictionNudge:
    def test_promotes_a_near_threshold_watch_to_buy(self, convs):
        sig = _sig("ENPH", "watch", "solar", score_buy=0.565)
        signals = [sig]
        md = {"ENPH": GOOD_MD}
        pr = _p("ENPH", "conviction_nudge", 0.08)
        playbook.apply_proposals(signals, [pr], portfolio={}, market_data=md, convictions=convs)
        assert sig["action"] == "buy"                 # 0.68 · 0.94 ≈ 0.64 ≥ 0.60
        assert sig["score_buy"] >= params.CP_BUY_THRESHOLD
        assert sig["suggested_position_pct"] is not None  # a promoted buy gets sized
        assert pr["applied"] is True

    def test_nudge_is_clamped_to_the_hard_cap(self, convs):
        # A wildly large nudge cannot exceed CONTEXTUAL_NUDGE_CAP — so a deeply-below-bar watch
        # (C=0.6·H) can only rise to (0.6+cap)·H, still a watch here, never an unbounded jump.
        sig = _sig("ENPH", "watch", "solar", score_buy=0.565)
        pr = _p("ENPH", "conviction_nudge", 5.0)      # absurd; must be clamped to +0.10
        playbook.apply_proposals([sig], [pr], portfolio={}, market_data={"ENPH": GOOD_MD}, convictions=convs)
        # (0.6 + 0.10) · 0.9417 ≈ 0.659 ≥ 0.60 → buy, but via the clamped cap, not the raw 5.0.
        assert sig["score_buy"] <= (0.6 + params.CONTEXTUAL_NUDGE_CAP) * 0.9417 + 1e-6

    def test_cannot_promote_an_excluded_name(self, convs):
        sig = _sig("TSLA", "watch", None, score_buy=0.565)
        pr = _p("TSLA", "conviction_nudge", 0.10)
        playbook.apply_proposals([sig], [pr], portfolio={}, market_data={"TSLA": GOOD_MD}, convictions=convs)
        assert sig["action"] == "watch"
        assert pr["applied"] is False

    def test_cannot_promote_a_do_not_add_name(self, convs):
        sig = _sig("BADCO", "watch", "solar", score_buy=0.565)
        pr = _p("BADCO", "conviction_nudge", 0.10)
        playbook.apply_proposals([sig], [pr], portfolio={}, market_data={"BADCO": GOOD_MD}, convictions=convs)
        assert sig["action"] == "watch"
        assert pr["applied"] is False

    def test_cannot_promote_a_hard_blocked_name(self, convs):
        sig = _sig("ENPH", "watch", "solar", score_buy=0.565, hard_rule_block="averaging-down cap")
        pr = _p("ENPH", "conviction_nudge", 0.10)
        playbook.apply_proposals([sig], [pr], portfolio={}, market_data={"ENPH": GOOD_MD}, convictions=convs)
        assert sig["action"] == "watch"
        assert pr["applied"] is False


class TestSizeMultiplier:
    def _final_size(self, mult, convs):
        # High-vol md so the raw target sits below the single-name cap and up-scaling is visible.
        md = {"PLTR": {**GOOD_MD, "atr_14": 8.0}}
        sig = _sig("PLTR", "buy", "ai", score_buy=0.753)
        playbook.apply_proposals([sig], [_p("PLTR", "size_multiplier", mult)],
                                 portfolio={}, market_data=md, convictions=convs)
        return sig["suggested_position_pct"]

    def test_upscaling_increases_size_within_the_cap(self, convs):
        base = self._final_size(1.0, convs)
        up = self._final_size(1.5, convs)
        assert up > base
        assert up <= params.SIZE_MAX_PCT + 1e-9

    def test_downscaling_reduces_size(self, convs):
        base = self._final_size(1.0, convs)
        down = self._final_size(0.5, convs)
        assert down < base

    def test_multiplier_is_clamped(self, convs):
        # An absurd multiplier is clamped to CONTEXTUAL_SIZE_MULT_MAX, never blows past the cap.
        huge = self._final_size(50.0, convs)
        assert huge <= params.SIZE_MAX_PCT + 1e-9


class TestInvariantsAndScope:
    def test_exploration_buys_are_left_untouched(self, convs):
        expl = _sig("EXPL", "buy", "ai", source="exploration", score_buy=None, suggested_position_pct=None)
        before = copy.deepcopy(expl)
        pr = _p("EXPL", "size_multiplier", 1.5)
        playbook.apply_proposals([expl], [pr], portfolio={}, market_data={"EXPL": GOOD_MD}, convictions=convs)
        assert expl == before      # unsized exploration entry is not re-sized by the layer

    def test_thesis_flag_downgrades_a_buy_and_adds_a_risk_flag(self, convs):
        sig = _sig("PLTR", "buy", "ai", score_buy=0.753, suggested_position_pct=0.05)
        pr = _p("PLTR", "thesis_flag", 1.0, play="earnings_risk")
        playbook.apply_proposals([sig], [pr], portfolio={}, market_data={"PLTR": GOOD_MD}, convictions=convs)
        assert sig["action"] == "watch"
        assert sig["suggested_position_pct"] is None
        assert any("earnings_risk" in r for r in sig["risk_flags"])
        assert pr["applied"] is True

    def test_no_op_when_no_proposals_apply(self, convs):
        sig = _sig("PLTR", "buy", "ai", score_buy=0.753, suggested_position_pct=0.05)
        before = copy.deepcopy(sig)
        # A proposal on an unknown ticker is rejected; signals must be untouched.
        pr = _p("UNKNOWN", "size_multiplier", 1.5)
        playbook.apply_proposals([sig], [pr], portfolio={}, market_data={"PLTR": GOOD_MD}, convictions=convs)
        assert sig == before
        assert pr["applied"] is False
