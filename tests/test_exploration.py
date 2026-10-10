"""
Tests for src/exploration.py pure functions.

No network, no Supabase, no Anthropic — all I/O mocked or untouched.
"""

import pytest

from src.exploration import (
    _build_themes_detail,
    candidates_from_payload,
    eligible_themes,
    filter_known_tickers,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def convictions():
    return {
        "exclusions": [{"ticker": "TSLA", "reason": "Employment blackout"}],
        "themes": {
            "space": {
                "conviction": "very_high",
                "thesis": "Artemis + cheap launch costs = orbital infrastructure.",
                "approved": ["RKLB", "ASTS", "ARKX"],
                "do_not_add": ["SPCE"],
            },
            "core_tech": {
                "conviction": "high",
                "thesis": "Core technology holdings.",
                "approved": ["NVDA", "GOOGL"],
                "do_not_add": [],
            },
            "ev_transition": {
                "conviction": "high",
                "thesis": "ICE minority within 20-30 years.",
                "approved": ["BYDDY"],
                "do_not_add": [],
            },
            "cannabis": {
                "conviction": "low",
                "thesis": "US federal legalization thesis stalled.",
                "approved": ["CRON"],
                "do_not_add": [],
            },
        },
    }


# ---------------------------------------------------------------------------
# eligible_themes
# ---------------------------------------------------------------------------

class TestEligibleThemes:
    def test_includes_high_and_very_high(self, convictions):
        themes = eligible_themes(convictions)
        assert "space" in themes
        assert "ev_transition" in themes

    def test_excludes_low_conviction(self, convictions):
        assert "cannabis" not in eligible_themes(convictions)

    def test_skips_a_frozen_theme(self, convictions):
        convictions["themes"]["ev_transition"]["frozen"] = True
        assert "ev_transition" not in eligible_themes(convictions)

    def test_includes_core_tech(self, convictions):
        assert "core_tech" in eligible_themes(convictions)

    def test_empty_themes(self):
        assert eligible_themes({"themes": {}}) == {}

    def test_no_themes_key(self):
        assert eligible_themes({}) == {}

    def test_detail_skips_frozen_and_low_themes(self, convictions):
        convictions["themes"]["ev_transition"]["frozen"] = True
        detail = _build_themes_detail(convictions)
        assert "space" in detail
        assert "core_tech" in detail
        assert "ev_transition" not in detail
        assert "cannabis" not in detail
        assert "in portfolio" not in detail
        assert "Already named, do not recommend: RKLB, ASTS, ARKX" in detail


class TestCandidatesFromPayload:
    def test_maps_conviction_and_drops_bad_rows(self):
        payload = {"candidates": [
            {"ticker": "lunr", "source_theme": "space", "rationale": "Lunar landers.",
             "quality_summary": "Early.", "analyst_summary": "None.",
             "conviction": "medium", "conviction_rationale": "Direct fit."},
            {"ticker": "TSLA", "source_theme": "space", "rationale": "No.",
             "conviction": "high", "quality_summary": "", "analyst_summary": "",
             "conviction_rationale": ""},
            {"ticker": "BYDDY", "source_theme": "ev_transition", "rationale": "Frozen theme.",
             "conviction": "high", "quality_summary": "", "analyst_summary": "",
             "conviction_rationale": ""},
            {"ticker": "NONE", "source_theme": "space", "rationale": "  ",
             "conviction": "low", "quality_summary": "", "analyst_summary": "",
             "conviction_rationale": ""},
        ]}
        rows = candidates_from_payload(payload, {"space"})
        assert len(rows) == 1
        assert rows[0]["ticker"] == "LUNR"
        assert rows[0]["claude_conviction"] == "medium"

    def test_caps_at_three_per_theme(self):
        payload = {"candidates": [
            {"ticker": f"S{i}", "source_theme": "space", "rationale": "Fits.",
             "quality_summary": "", "analyst_summary": "",
             "conviction": "low", "conviction_rationale": "Early."}
            for i in range(5)
        ]}
        rows = candidates_from_payload(payload, {"space"})
        assert [row["ticker"] for row in rows] == ["S0", "S1", "S2"]

    def test_empty_payload(self):
        assert candidates_from_payload({}, {"space"}) == []
        assert candidates_from_payload(None, {"space"}) == []


_VALID_CANDIDATE = {
    "ticker": "LUNR",
    "source_theme": "space",
    "rationale": "Lunar surface delivery contracts.",
    "quality_summary": "Pre-revenue but >2yr runway.",
    "analyst_summary": "No coverage found.",
    "claude_conviction": "medium",
    "conviction_rationale": "Early stage but direct thesis fit.",
}


# ---------------------------------------------------------------------------
# filter_known_tickers
# ---------------------------------------------------------------------------

class TestFilterKnownTickers:
    def _make(self, ticker):
        return {**_VALID_CANDIDATE, "ticker": ticker}

    def test_removes_portfolio_tickers(self):
        candidates = [self._make("LUNR"), self._make("RKLB")]
        result = filter_known_tickers(candidates, {"RKLB"}, set(), set())
        assert len(result) == 1
        assert result[0]["ticker"] == "LUNR"

    def test_removes_exclusions(self):
        candidates = [self._make("TSLA"), self._make("LUNR")]
        result = filter_known_tickers(candidates, set(), {"TSLA"}, set())
        assert len(result) == 1

    def test_removes_already_tracked(self):
        candidates = [self._make("LUNR"), self._make("RDW")]
        result = filter_known_tickers(candidates, set(), set(), {"LUNR"})
        assert len(result) == 1
        assert result[0]["ticker"] == "RDW"

    def test_case_insensitive_match(self):
        candidates = [self._make("lunr")]
        result = filter_known_tickers(candidates, {"LUNR"}, set(), set())
        # lowercase ticker won't match LUNR since filter checks .upper() on the stored ticker
        # But the candidate itself has lowercase — filter uses c.get("ticker","").upper()
        assert len(result) == 0

    def test_empty_candidates(self):
        result = filter_known_tickers([], {"RKLB"}, {"TSLA"}, {"LUNR"})
        assert result == []

    def test_all_pass(self):
        candidates = [self._make("LUNR"), self._make("RDW")]
        result = filter_known_tickers(candidates, set(), set(), set())
        assert len(result) == 2

    def test_all_blocked(self):
        candidates = [self._make("LUNR"), self._make("RDW")]
        result = filter_known_tickers(candidates, {"LUNR"}, set(), {"RDW"})
        assert result == []
