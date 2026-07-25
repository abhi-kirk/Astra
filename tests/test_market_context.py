"""
Unit tests for src/brain/market_context.py — the pure quant signals behind the contextual
layer's macro_dip play (docs/contextual_reasoning.md §6): beta decomposition (macro-vs-
idiosyncratic drop split) and selloff velocity/breadth.
"""

import math

from src.brain import market_context


def _noise(n: int) -> list[float]:
    """Deterministic zero-ish-mean series with real variance (so beta is estimable)."""
    return [0.01 * math.sin(i) for i in range(n)]


class TestBetaDecomposition:
    def test_pure_market_beta_drop_has_tiny_residual(self):
        # SPY: mild noise then a broad ~2%/day drop; the name moves as exactly 1.2× SPY.
        spy = _noise(50) + [-0.02] * 10
        name = [1.2 * r for r in spy]
        d = market_context.beta_decomposition(name, spy)
        assert d is not None
        assert d["beta"] == 1.2 or abs(d["beta"] - 1.2) < 0.05
        assert d["r2"] > 0.99                      # SPY explains ~all of the name's variance
        assert d["name_move_pct"] < 0              # it did drop
        assert abs(d["idiosyncratic_residual_pct"]) < 3.0   # the drop is (almost) all market

    def test_idiosyncratic_drop_has_large_residual(self):
        # Beta is normal (estimated on the pre-drop baseline), but in the recent window the name
        # drops far more than the market (its own -3%/day weakness) → large residual, not macro.
        spy = _noise(50) + [-0.02] * 10
        name = list(spy)
        for i in range(50, 60):
            name[i] = spy[i] - 0.03            # company-specific weakness on top of the market drop
        d = market_context.beta_decomposition(name, spy)
        assert d is not None
        assert d["name_move_pct"] < 0
        assert abs(d["idiosyncratic_residual_pct"]) > 5.0   # NOT explained by the market

    def test_missing_or_short_history_returns_none(self):
        assert market_context.beta_decomposition(None, _noise(60)) is None
        assert market_context.beta_decomposition(_noise(60), None) is None
        assert market_context.beta_decomposition(_noise(5), _noise(5)) is None    # < window + drop
        assert market_context.beta_decomposition(_noise(60), [0.0] * 60) is None  # SPY has no variance


class TestSelloffState:
    def test_broad_fast_drop_is_panic(self):
        spy = [0.001] * 40 + [-0.012] * 5          # SPY ~ -6% over the last 5 days
        market_data = {t: {"returns_recent": [0.001] * 40 + [-0.02] * 5} for t in ("A", "B", "C", "D")}
        s = market_context.selloff_state(spy, market_data)
        assert s["spy_nday_return_pct"] < -4.0
        assert s["breadth_pct_down"] == 100.0
        assert s["is_panic"] is True

    def test_slow_grind_is_not_panic(self):
        spy = [-0.001] * 45                         # a slow drift, not a fast risk-off move
        market_data = {"A": {"returns_recent": [-0.001] * 45}}
        s = market_context.selloff_state(spy, market_data)
        assert s["is_panic"] is False

    def test_no_spy_history_is_not_panic(self):
        s = market_context.selloff_state(None, {"A": {"returns_recent": [-0.02] * 10}})
        assert s["is_panic"] is False
        assert s["spy_nday_return_pct"] is None
