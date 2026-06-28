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
    def test_has_search_when_url_set(self, monkeypatch):
        monkeypatch.setattr("src.mcp.TAVILY_MCP_URL", "https://mcp.tavily.com/mcp/?tavilyApiKey=test")
        ctx = search_context()
        assert ctx["has_search"] is True
        assert ctx["max_searches"] > 0

    def test_no_search_when_url_empty(self, monkeypatch):
        monkeypatch.setattr("src.mcp.TAVILY_MCP_URL", "")
        monkeypatch.setattr("src.mcp.ALPHA_VANTAGE_API_KEY", "")
        ctx = search_context()
        assert ctx["has_search"] is False

    def test_max_searches_override(self, monkeypatch):
        monkeypatch.setattr("src.mcp.TAVILY_MCP_URL", "https://example.com")
        ctx = search_context(max_searches=3)
        assert ctx["max_searches"] == 3
