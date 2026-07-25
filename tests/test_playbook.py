"""
Unit tests for src/brain/playbook.py evaluation — the deterministic (quant-arm) side of the
contextual layer. The macro_dip quant evaluator + the two-producer `evaluate` dispatch.
"""

import math

from src.brain import playbook


def _noise(n: int) -> list[float]:
    return [0.01 * math.sin(i) for i in range(n)]


# SPY: mild noise then a broad ~2%/day drop (a real market selloff). A name = 1.2× SPY has a deep,
# almost-entirely-market-beta drop → macro_dip should fire.
SPY = _noise(50) + [-0.02] * 10
MACRO_NAME_RETURNS = [1.2 * r for r in SPY]

PANIC_CTX = {
    "spy_returns": SPY,
    "selloff": {"spy_nday_return_pct": -9.6, "nday": 5, "breadth_pct_down": 100.0, "is_panic": True},
}

MACRO_PLAY = {
    "name": "macro_dip", "enabled": True, "type": "hybrid", "weight": 1.0,
    "params": {"beta_min": 0.8, "residual_max_pct": 5.0, "min_drop_pct": 8.0,
               "size_mult_max": 1.5, "nudge_max": 0.08},
}


def _signal(ticker, action="watch", **kw):
    return {"ticker": ticker, "action": action, "source": "position_screen",
            "theme": "core_tech", "score_buy": 0.5, "reasons": [], "risk_flags": [], **kw}


class TestMacroDipEvaluator:
    def test_fires_on_a_macro_beta_drop_in_a_panic(self):
        signals = [_signal("NVDA", "watch")]
        md = {"NVDA": {"current_price": 85.0, "returns_recent": MACRO_NAME_RETURNS}}
        props = playbook._eval_macro_dip(MACRO_PLAY, signals, md, PANIC_CTX)
        kinds = {p["kind"] for p in props}
        assert "size_multiplier" in kinds
        # watch candidate also gets a (small) conviction nudge to potentially promote it
        assert "conviction_nudge" in kinds
        size = next(p for p in props if p["kind"] == "size_multiplier")
        assert 1.0 < size["value"] <= MACRO_PLAY["params"]["size_mult_max"]
        assert size["source"] == "quant" and size["play"] == "macro_dip"

    def test_unreliable_beta_read_is_gated_by_min_r2(self):
        # Same macro dip, but if the r² floor is set above the achieved fit, the read is deemed
        # unreliable and macro_dip must not fire (SPY doesn't explain enough of the name's moves).
        signals = [_signal("NVDA", "watch")]
        md = {"NVDA": {"current_price": 85.0, "returns_recent": MACRO_NAME_RETURNS}}
        strict = {**MACRO_PLAY, "params": {**MACRO_PLAY["params"], "min_r2": 1.01}}
        assert playbook._eval_macro_dip(strict, signals, md, PANIC_CTX) == []
        loose = {**MACRO_PLAY, "params": {**MACRO_PLAY["params"], "min_r2": 0.0}}
        assert playbook._eval_macro_dip(loose, signals, md, PANIC_CTX) != []

    def test_silent_when_market_is_not_in_a_selloff(self):
        calm = {"spy_returns": SPY, "selloff": {"is_panic": False}}
        signals = [_signal("NVDA", "watch")]
        md = {"NVDA": {"current_price": 85.0, "returns_recent": MACRO_NAME_RETURNS}}
        assert playbook._eval_macro_dip(MACRO_PLAY, signals, md, calm) == []

    def test_silent_on_an_idiosyncratic_drop(self):
        # Even in the panic, this name drops far MORE than its beta predicts (an extra -3%/day of
        # company-specific weakness) → large idiosyncratic residual → macro_dip must NOT fire.
        name = list(SPY)
        for i in range(50, 60):
            name[i] = SPY[i] - 0.03
        signals = [_signal("BADCO", "watch")]
        md = {"BADCO": {"current_price": 20.0, "returns_recent": name}}
        assert playbook._eval_macro_dip(MACRO_PLAY, signals, md, PANIC_CTX) == []

    def test_ignores_exploration_and_non_candidate_signals(self):
        signals = [
            _signal("NVDA", "hold"),                       # not a buy/watch candidate
            _signal("EXPL", "buy", source="exploration"),  # unsized exploration entry
        ]
        md = {t: {"current_price": 85.0, "returns_recent": MACRO_NAME_RETURNS} for t in ("NVDA", "EXPL")}
        assert playbook._eval_macro_dip(MACRO_PLAY, signals, md, PANIC_CTX) == []


class TestEvaluate:
    def test_empty_or_disabled_playbook_yields_nothing(self, convictions):
        signals = [_signal("NVDA", "watch")]
        md = {"NVDA": {"current_price": 85.0, "returns_recent": MACRO_NAME_RETURNS}}
        assert playbook.evaluate(signals, md, PANIC_CTX, convictions, plays={"plays": []}) == []
        disabled = {"plays": [{**MACRO_PLAY, "enabled": False}]}
        assert playbook.evaluate(signals, md, PANIC_CTX, convictions, plays=disabled) == []

    def test_quant_arm_runs_without_ai(self, convictions):
        # A quant-typed play produces proposals deterministically (no LLM), use_ai=False.
        quant_play = {**MACRO_PLAY, "type": "quant"}
        signals = [_signal("NVDA", "watch")]
        md = {"NVDA": {"current_price": 85.0, "returns_recent": MACRO_NAME_RETURNS}}
        props = playbook.evaluate(signals, md, PANIC_CTX, convictions,
                                  use_ai=False, plays={"plays": [quant_play]})
        assert props and all(p["source"] == "quant" for p in props)

    def test_hybrid_base_stands_when_ai_off(self, convictions):
        # A hybrid play with use_ai=False falls back to its deterministic quant base.
        signals = [_signal("NVDA", "watch")]
        md = {"NVDA": {"current_price": 85.0, "returns_recent": MACRO_NAME_RETURNS}}
        props = playbook.evaluate(signals, md, PANIC_CTX, convictions,
                                  use_ai=False, plays={"plays": [MACRO_PLAY]})
        assert props and all(p["source"] == "quant" for p in props)
