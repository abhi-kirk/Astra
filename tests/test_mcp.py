"""
Unit tests for src/mcp.py — server specs, text extraction, prompt context.
"""

from unittest.mock import MagicMock

from anthropic.types import TextBlock

from src import config
from src.mcp import ServerSpec, advisor_specs, exploration_specs, extract_text, search_context

# ---------------------------------------------------------------------------
# extract_text
# ---------------------------------------------------------------------------

class TestExtractText:
    def _make_message(self, blocks):
        msg = MagicMock()
        msg.content = blocks
        return msg

    def test_single_text_block(self):
        block = MagicMock(spec=TextBlock)
        block.text = "Standard advisor note."
        assert extract_text(self._make_message([block])) == "Standard advisor note."

    def test_joins_multiple_text_blocks(self):
        b1 = MagicMock(spec=TextBlock)
        b1.text = "## Note"
        b2 = MagicMock(spec=TextBlock)
        b2.text = "Body content."
        assert extract_text(self._make_message([b1, b2])) == "## Note\nBody content."

    def test_empty_content(self):
        assert extract_text(self._make_message([])) == ""

    def test_non_text_blocks_ignored(self):
        other = MagicMock()  # no spec — not a TextBlock instance
        assert extract_text(self._make_message([other])) == ""


# ---------------------------------------------------------------------------
# advisor_specs / exploration_specs
# ---------------------------------------------------------------------------

class TestSpecs:
    def _patch_none(self, monkeypatch):
        monkeypatch.setattr(config.services, "tavily_mcp_url", "")
        monkeypatch.setattr(config.services, "alpha_vantage_api_key", "")
        monkeypatch.setattr(config.services, "sec_edgar_mcp_url", "")
        monkeypatch.setattr(config.services, "fmp_api_key", "")

    def test_empty_when_unconfigured(self, monkeypatch):
        self._patch_none(monkeypatch)
        assert advisor_specs() == []
        assert exploration_specs() == []

    def test_tavily_only(self, monkeypatch):
        self._patch_none(monkeypatch)
        monkeypatch.setattr(config.services, "tavily_mcp_url", "https://mcp.tavily.com/mcp/?tavilyApiKey=t")
        specs = advisor_specs()
        assert [s.name for s in specs] == ["tavily"]
        assert specs[0].allowed_tools == ["tavily_search"]

    def test_alpha_vantage_key_in_url(self, monkeypatch):
        self._patch_none(monkeypatch)
        monkeypatch.setattr(config.services, "alpha_vantage_api_key", "my-secret-key")
        specs = advisor_specs()
        assert [s.name for s in specs] == ["alpha_vantage"]
        assert "my-secret-key" in specs[0].url

    def test_both_configured(self, monkeypatch):
        self._patch_none(monkeypatch)
        monkeypatch.setattr(config.services, "tavily_mcp_url", "https://tavily")
        monkeypatch.setattr(config.services, "alpha_vantage_api_key", "k")
        assert {s.name for s in advisor_specs()} == {"tavily", "alpha_vantage"}

    def test_sec_edgar_and_fmp_excluded_from_live_specs(self, monkeypatch):
        # Dormant servers must never appear in the live loop, even when configured.
        self._patch_none(monkeypatch)
        monkeypatch.setattr(config.services, "sec_edgar_mcp_url", "https://secedgar.example/mcp")
        monkeypatch.setattr(config.services, "fmp_api_key", "fmpkey")
        assert advisor_specs() == []
        assert exploration_specs() == []

    def test_dormant_definitions_still_build(self, monkeypatch):
        # The _fmp/_sec_edgar builders are kept for a future follow-up.
        from src.mcp import _fmp, _sec_edgar
        monkeypatch.setattr(config.services, "fmp_api_key", "mykey456")
        monkeypatch.setattr(config.services, "sec_edgar_mcp_url", "https://secedgar.example/mcp")
        fmp = _fmp()
        edgar = _sec_edgar()
        assert fmp is not None and "mykey456" in fmp.url and set(fmp.allowed_tools) == {"analyst", "calendar"}
        assert edgar is not None and edgar.allowed_tools == ["secedgar_get_insider_transactions"]


# ---------------------------------------------------------------------------
# search_context
# ---------------------------------------------------------------------------

class TestSearchContext:
    def test_no_search_when_empty(self):
        ctx = search_context(servers=[])
        assert ctx["has_search"] is False
        assert ctx["has_tavily"] is False
        assert ctx["has_alpha_vantage"] is False

    def test_flags_track_specs(self):
        specs = [ServerSpec("tavily", "u", ["tavily-search"])]
        ctx = search_context(servers=specs)
        assert ctx["has_search"] is True
        assert ctx["has_tavily"] is True
        assert ctx["has_alpha_vantage"] is False
        # Dormant servers are never advertised.
        assert ctx["has_sec_edgar"] is False
        assert ctx["has_fmp"] is False

    def test_max_searches_override(self):
        ctx = search_context(max_searches=3, servers=[ServerSpec("tavily", "u")])
        assert ctx["max_searches"] == 3
        assert ctx["max_av_calls"] > 0
