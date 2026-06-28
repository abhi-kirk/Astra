"""
Unit tests for src/mcp.py — pure utility functions only.
"""

from unittest.mock import MagicMock

import pytest

from anthropic.types import TextBlock
from anthropic.types.beta import BetaTextBlock

from src.mcp import extract_text, search_context


# ---------------------------------------------------------------------------
# extract_text
# ---------------------------------------------------------------------------

class TestExtractText:
    def _make_message(self, blocks):
        msg = MagicMock()
        msg.content = blocks
        return msg

    def test_standard_text_block(self):
        block = MagicMock(spec=TextBlock)
        block.text = "Standard advisor note."
        msg = self._make_message([block])
        assert extract_text(msg) == "Standard advisor note."

    def test_beta_text_block_strips_xml(self):
        raw = (
            "<tool_call>search RKLB news</tool_call>"
            "<tool_response>some results</tool_response>"
            "## ASTRA Daily Note\nActual content here."
        )
        block = MagicMock(spec=BetaTextBlock)
        block.text = raw
        msg = self._make_message([block])
        result = extract_text(msg)
        assert "<tool_call>" not in result
        assert "<tool_response>" not in result
        assert "Actual content here." in result

    def test_beta_drops_preamble_before_heading(self):
        raw = "Sure, let me search...\nOkay found it.\n## ## ASTRA Daily Note\nReal content."
        block = MagicMock(spec=BetaTextBlock)
        block.text = raw
        msg = self._make_message([block])
        result = extract_text(msg)
        assert "Sure, let me search" not in result
        assert "Real content." in result

    def test_beta_takes_last_text_block(self):
        # First block is a thinking block, last is the actual note
        block1 = MagicMock(spec=BetaTextBlock)
        block1.text = "Thinking..."
        block2 = MagicMock(spec=BetaTextBlock)
        block2.text = "## ASTRA Daily Note\nFinal note."
        msg = self._make_message([block1, block2])
        result = extract_text(msg)
        assert "Final note." in result

    def test_empty_content(self):
        msg = self._make_message([])
        assert extract_text(msg) == ""

    def test_non_text_blocks_ignored(self):
        # Blocks that are neither TextBlock nor BetaTextBlock should be skipped
        other = MagicMock()  # no spec — not an instance of either
        msg = self._make_message([other])
        assert extract_text(msg) == ""


# ---------------------------------------------------------------------------
# search_context
# ---------------------------------------------------------------------------

class TestSearchContext:
    def _patch_none(self, monkeypatch):
        monkeypatch.setattr("src.mcp.TAVILY_MCP_URL", "")
        monkeypatch.setattr("src.mcp.ALPHA_VANTAGE_API_KEY", "")
        monkeypatch.setattr("src.mcp.SEC_EDGAR_MCP_URL", "")
        monkeypatch.setattr("src.mcp.FMP_API_KEY", "")

    def test_no_search_when_both_empty(self, monkeypatch):
        self._patch_none(monkeypatch)
        ctx = search_context()
        assert ctx["has_search"] is False
        assert ctx["has_tavily"] is False
        assert ctx["has_alpha_vantage"] is False

    def test_has_search_tavily_only(self, monkeypatch):
        monkeypatch.setattr("src.mcp.TAVILY_MCP_URL", "https://mcp.tavily.com/mcp/?tavilyApiKey=test")
        monkeypatch.setattr("src.mcp.ALPHA_VANTAGE_API_KEY", "")
        ctx = search_context()
        assert ctx["has_search"] is True
        assert ctx["has_tavily"] is True
        assert ctx["has_alpha_vantage"] is False

    def test_has_search_alpha_vantage_only(self, monkeypatch):
        monkeypatch.setattr("src.mcp.TAVILY_MCP_URL", "")
        monkeypatch.setattr("src.mcp.ALPHA_VANTAGE_API_KEY", "testkey")
        ctx = search_context()
        assert ctx["has_search"] is True
        assert ctx["has_tavily"] is False
        assert ctx["has_alpha_vantage"] is True

    def test_has_search_both_configured(self, monkeypatch):
        monkeypatch.setattr("src.mcp.TAVILY_MCP_URL", "https://mcp.tavily.com/mcp/?tavilyApiKey=test")
        monkeypatch.setattr("src.mcp.ALPHA_VANTAGE_API_KEY", "testkey")
        ctx = search_context()
        assert ctx["has_search"] is True
        assert ctx["has_tavily"] is True
        assert ctx["has_alpha_vantage"] is True
        assert ctx["max_av_calls"] > 0

    def test_max_searches_override(self, monkeypatch):
        monkeypatch.setattr("src.mcp.TAVILY_MCP_URL", "https://example.com")
        ctx = search_context(max_searches=3)
        assert ctx["max_searches"] == 3

    def test_alpha_vantage_key_in_url(self, monkeypatch):
        monkeypatch.setattr("src.mcp.TAVILY_MCP_URL", "")
        monkeypatch.setattr("src.mcp.ALPHA_VANTAGE_API_KEY", "my-secret-key")
        monkeypatch.setattr("src.mcp.SEC_EDGAR_MCP_URL", "")
        monkeypatch.setattr("src.mcp.FMP_API_KEY", "")
        from src.mcp import build_servers
        servers = build_servers()
        assert len(servers) == 1
        assert "my-secret-key" in servers[0]["url"]
        assert servers[0]["name"] == "alpha_vantage"

    def test_sec_edgar_included_when_url_set(self, monkeypatch):
        monkeypatch.setattr("src.mcp.TAVILY_MCP_URL", "")
        monkeypatch.setattr("src.mcp.ALPHA_VANTAGE_API_KEY", "")
        monkeypatch.setattr("src.mcp.SEC_EDGAR_MCP_URL", "https://secedgar.caseyjhand.com/mcp")
        ctx = search_context()
        assert ctx["has_search"] is True
        assert ctx["has_sec_edgar"] is True
        assert ctx["has_tavily"] is False
        assert ctx["has_alpha_vantage"] is False

    def test_sec_edgar_excluded_when_url_empty(self, monkeypatch):
        monkeypatch.setattr("src.mcp.TAVILY_MCP_URL", "")
        monkeypatch.setattr("src.mcp.ALPHA_VANTAGE_API_KEY", "")
        monkeypatch.setattr("src.mcp.SEC_EDGAR_MCP_URL", "")
        monkeypatch.setattr("src.mcp.FMP_API_KEY", "")
        ctx = search_context()
        assert ctx["has_search"] is False
        assert ctx["has_sec_edgar"] is False

    def test_sec_edgar_allowed_tools(self, monkeypatch):
        monkeypatch.setattr("src.mcp.TAVILY_MCP_URL", "")
        monkeypatch.setattr("src.mcp.ALPHA_VANTAGE_API_KEY", "")
        monkeypatch.setattr("src.mcp.SEC_EDGAR_MCP_URL", "https://secedgar.caseyjhand.com/mcp")
        monkeypatch.setattr("src.mcp.FMP_API_KEY", "")
        from src.mcp import build_servers
        servers = build_servers()
        edgar = next(s for s in servers if s["name"] == "sec_edgar")
        assert edgar["tool_configuration"]["allowed_tools"] == ["secedgar_get_insider_transactions"]

    def test_fmp_included_when_key_set(self, monkeypatch):
        monkeypatch.setattr("src.mcp.TAVILY_MCP_URL", "")
        monkeypatch.setattr("src.mcp.ALPHA_VANTAGE_API_KEY", "")
        monkeypatch.setattr("src.mcp.SEC_EDGAR_MCP_URL", "")
        monkeypatch.setattr("src.mcp.FMP_API_KEY", "testkey123")
        ctx = search_context()
        assert ctx["has_search"] is True
        assert ctx["has_fmp"] is True
        assert ctx["has_tavily"] is False
        assert ctx["has_alpha_vantage"] is False

    def test_fmp_excluded_when_key_empty(self, monkeypatch):
        self._patch_none(monkeypatch)
        ctx = search_context()
        assert ctx["has_fmp"] is False

    def test_fmp_key_in_url_and_allowed_tools(self, monkeypatch):
        monkeypatch.setattr("src.mcp.TAVILY_MCP_URL", "")
        monkeypatch.setattr("src.mcp.ALPHA_VANTAGE_API_KEY", "")
        monkeypatch.setattr("src.mcp.SEC_EDGAR_MCP_URL", "")
        monkeypatch.setattr("src.mcp.FMP_API_KEY", "mykey456")
        from src.mcp import build_servers
        servers = build_servers()
        assert len(servers) == 1
        fmp = servers[0]
        assert fmp["name"] == "fmp"
        assert "mykey456" in fmp["url"]
        assert set(fmp["tool_configuration"]["allowed_tools"]) == {"analyst", "calendar"}

    def test_fmp_max_calls_in_context(self, monkeypatch):
        monkeypatch.setattr("src.mcp.TAVILY_MCP_URL", "")
        monkeypatch.setattr("src.mcp.ALPHA_VANTAGE_API_KEY", "")
        monkeypatch.setattr("src.mcp.SEC_EDGAR_MCP_URL", "")
        monkeypatch.setattr("src.mcp.FMP_API_KEY", "testkey123")
        ctx = search_context()
        assert ctx["max_fmp_calls"] > 0
