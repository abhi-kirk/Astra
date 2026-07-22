"""Unit tests for src/brain/conviction.buyable_tickers — the daily screening universe."""

from src.brain import params
from src.brain.conviction import buyable_tickers, conviction_weight


def test_buyable_is_preferred_and_approved_only(convictions):
    # approved: GOOGL, NVDA (core_tech) + RKLB, ASTS (space)
    assert buyable_tickers(convictions) == ["ASTS", "GOOGL", "NVDA", "RKLB"]


def test_hold_only_and_do_not_add_are_excluded(convictions):
    buyable = buyable_tickers(convictions)
    assert "NIO" not in buyable    # individual holding, status=hold → hold-only
    assert "SPCE" not in buyable   # space do_not_add


def test_hard_excluded_never_buyable(convictions):
    convictions["themes"]["core_tech"]["approved"].append("TSLA")
    assert "TSLA" not in buyable_tickers(convictions)


def test_preferred_bucket_included(convictions):
    convictions["themes"]["core_tech"]["preferred"] = ["MSFT"]
    assert "MSFT" in buyable_tickers(convictions)


def test_empty_convictions_yields_nothing():
    assert buyable_tickers({}) == []


class TestConvictionWeightTiering:
    """Conviction-primary: C from the THEME label (very_high>high>low), not preferred/approved."""

    def _g(self, **over):
        base = {"status": "approved", "theme_conviction": "high",
                "hold_only": False, "do_not_add": False}
        base.update(over)
        return base

    def test_theme_label_drives_weight(self, monkeypatch):
        monkeypatch.setattr(params, "CONVICTION_PRIMARY", True)
        assert conviction_weight(self._g(theme_conviction="very_high")) == params.CONVICTION_THEME_WEIGHTS["very_high"]
        assert conviction_weight(self._g(theme_conviction="high")) == params.CONVICTION_THEME_WEIGHTS["high"]
        assert conviction_weight(self._g(theme_conviction="low")) == params.CONVICTION_THEME_WEIGHTS["low"]

    def test_very_high_outranks_high(self, monkeypatch):
        # The whole point: a very_high theme (space) must weigh above a high theme (big tech),
        # regardless of preferred/approved bucket.
        monkeypatch.setattr(params, "CONVICTION_PRIMARY", True)
        space = conviction_weight(self._g(status="approved", theme_conviction="very_high"))
        tech = conviction_weight(self._g(status="preferred", theme_conviction="high"))
        assert space > tech

    def test_bucket_ignored_under_conviction_primary(self, monkeypatch):
        # preferred vs approved no longer matters — only the theme label does (H differentiates names).
        monkeypatch.setattr(params, "CONVICTION_PRIMARY", True)
        assert conviction_weight(self._g(status="preferred", theme_conviction="high")) == \
               conviction_weight(self._g(status="approved", theme_conviction="high"))

    def test_behavioral_gates_still_apply(self, monkeypatch):
        monkeypatch.setattr(params, "CONVICTION_PRIMARY", True)
        assert conviction_weight(self._g(status="hold_only", theme_conviction="very_high")) == params.CONVICTION_WEIGHTS["hold"]
        assert conviction_weight(self._g(status="do_not_add", theme_conviction="very_high")) == 0.0
        assert conviction_weight(self._g(status="unknown", theme_conviction=None)) == 0.0

    def test_legacy_still_bucket_based(self, monkeypatch):
        monkeypatch.setattr(params, "CONVICTION_PRIMARY", False)
        assert conviction_weight(self._g(status="preferred")) == params.CONVICTION_WEIGHTS["preferred"]
        assert conviction_weight(self._g(status="approved")) == params.CONVICTION_WEIGHTS["approved"]
