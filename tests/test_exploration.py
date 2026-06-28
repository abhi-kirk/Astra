"""
Tests for src/exploration.py pure functions.

No network, no Supabase, no Anthropic — all I/O mocked or untouched.
"""

import json
import pytest

from src.exploration import build_theme_queries, filter_known_tickers, parse_candidates


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
# build_theme_queries
# ---------------------------------------------------------------------------

class TestBuildThemeQueries:
    def test_includes_high_and_very_high(self, convictions):
        queries = build_theme_queries(convictions)
        assert "space" in queries
        assert "ev_transition" in queries

    def test_excludes_low_conviction(self, convictions):
        queries = build_theme_queries(convictions)
        assert "cannabis" not in queries

    def test_includes_core_tech(self, convictions):
        queries = build_theme_queries(convictions)
        assert "core_tech" in queries

    def test_query_contains_theme_label(self, convictions):
        queries = build_theme_queries(convictions)
        assert "space" in queries["space"]
        assert "ev transition" in queries["ev_transition"]

    def test_empty_themes(self):
        queries = build_theme_queries({"themes": {}})
        assert queries == {}

    def test_no_themes_key(self):
        queries = build_theme_queries({})
        assert queries == {}


# ---------------------------------------------------------------------------
# parse_candidates
# ---------------------------------------------------------------------------

_VALID_CANDIDATE = {
    "ticker": "LUNR",
    "source_theme": "space",
    "rationale": "Lunar surface delivery contracts.",
    "quality_summary": "Pre-revenue but >2yr runway.",
    "analyst_summary": "No coverage found.",
    "claude_conviction": "medium",
    "conviction_rationale": "Early stage but direct thesis fit.",
}


class TestParseCandidates:
    def test_parses_json_code_block(self):
        text = f"```json\n{json.dumps([_VALID_CANDIDATE])}\n```"
        result = parse_candidates(text)
        assert len(result) == 1
        assert result[0]["ticker"] == "LUNR"

    def test_parses_bare_json_array(self):
        text = json.dumps([_VALID_CANDIDATE])
        result = parse_candidates(text)
        assert len(result) == 1

    def test_returns_empty_on_no_json(self):
        result = parse_candidates("No candidates found this week.")
        assert result == []

    def test_returns_empty_on_invalid_json(self):
        result = parse_candidates("```json\n{broken\n```")
        assert result == []

    def test_returns_empty_list_from_json(self):
        result = parse_candidates("```json\n[]\n```")
        assert result == []

    def test_multiple_candidates(self):
        second = {**_VALID_CANDIDATE, "ticker": "RDW", "source_theme": "space"}
        text = f"```json\n{json.dumps([_VALID_CANDIDATE, second])}\n```"
        result = parse_candidates(text)
        assert len(result) == 2
        assert {c["ticker"] for c in result} == {"LUNR", "RDW"}

    def test_preamble_before_block_is_ignored(self):
        text = "Here are my findings after research:\n\n```json\n" + json.dumps([_VALID_CANDIDATE]) + "\n```"
        result = parse_candidates(text)
        assert len(result) == 1


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
