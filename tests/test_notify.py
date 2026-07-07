"""
Tests for src/notify.py — message composition (via telegramify-markdown) and
Telegram send gating. No network: requests.post is mocked.
"""

from unittest.mock import MagicMock, patch

from src import notify

# ---------------------------------------------------------------------------
# format_message
# ---------------------------------------------------------------------------

class TestFormatMessage:
    def test_includes_tickers(self):
        msg = notify.format_message(["MSFT", "NVDA"], ["DAL"], "All good here")
        assert "MSFT" in msg and "NVDA" in msg
        assert "DAL" in msg
        assert "All good here" in msg

    def test_empty_signals_show_none(self):
        msg = notify.format_message([], [], "note text")
        assert "BUY" in msg and "SELL" in msg
        assert "none" in msg

    def test_no_watching_line(self):
        msg = notify.format_message([], ["DAL"], "note")
        assert "watching" not in msg

    def test_no_redundant_astra_date_header(self):
        # The bot name + Telegram timestamp cover this; message must not add its own.
        msg = notify.format_message([], ["DAL"], "Sell now")
        assert "ASTRA —" not in msg and "ASTRA \\—" not in msg

    def test_markdown_note_is_converted(self):
        msg = notify.format_message([], ["DAL"], "## Priority\n\nSell **DAL** now")
        assert "##" not in msg          # header converted, not literal
        assert "**DAL**" not in msg     # bold converted to MarkdownV2
        assert "Priority" in msg and "Sell" in msg

    def test_empty_note_placeholder(self):
        msg = notify.format_message(["MSFT"], [], "")
        assert "No advisor note" in msg

    def test_includes_dashboard_link(self):
        msg = notify.format_message([], [], "n")
        assert "abhi-kirk.github.io" in msg

    def test_truncates_long_note_within_limit(self):
        msg = notify.format_message([], [], "x" * 6000)
        assert len(msg) <= notify._MAX_LEN

    def test_live_mode_tagged(self):
        msg = notify.format_message([], [], "n", mode="live")
        assert "mode: live" in msg

    def test_output_is_markdownv2_string(self):
        msg = notify.format_message(["MSFT"], [], "hello")
        assert isinstance(msg, str) and msg

    def test_gist_leads_before_note_body(self):
        note = "## Title\n\n---\n\n### 1. PRIORITY ACTIONS\n\nThe five must sell now. More detail follows."
        msg = notify.format_message([], ["DAL"], note)
        # gist (first prose sentence) appears before the note's section header
        assert msg.index("must sell now") < msg.index("PRIORITY")


# ---------------------------------------------------------------------------
# _extract_gist
# ---------------------------------------------------------------------------

class TestExtractGist:
    def test_skips_headers_and_rules(self):
        note = "## Daily Note\n\n---\n\n### Section\n\nHello world. Second sentence."
        assert notify._extract_gist(note) == "Hello world. Second sentence."

    def test_clips_long_paragraph_at_sentence_boundary(self):
        para = "First sentence is short. " + "then a very long tail " * 40
        gist = notify._extract_gist(para)
        assert len(gist) <= notify._GIST_MAX
        assert gist.startswith("First sentence is short.")

    def test_empty_when_no_prose(self):
        assert notify._extract_gist("## Only\n\n---\n\n### Headers") == ""


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------

class TestSend:
    def test_skips_when_unconfigured(self):
        with patch.object(notify.config.telegram, "bot_token", ""), \
             patch.object(notify.config.telegram, "chat_id", ""), \
             patch.object(notify.requests, "post") as post:
            assert notify.send("hi") is False
            post.assert_not_called()

    def test_skips_when_only_token_set(self):
        with patch.object(notify.config.telegram, "bot_token", "t"), \
             patch.object(notify.config.telegram, "chat_id", ""), \
             patch.object(notify.requests, "post") as post:
            assert notify.send("hi") is False
            post.assert_not_called()

    def test_posts_and_returns_true(self):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        with patch.object(notify.config.telegram, "bot_token", "tok"), \
             patch.object(notify.config.telegram, "chat_id", "123"), \
             patch.object(notify.requests, "post", return_value=resp) as post:
            assert notify.send("hi") is True
            post.assert_called_once()
            kwargs = post.call_args.kwargs
            assert kwargs["json"]["chat_id"] == "123"
            assert kwargs["json"]["parse_mode"] == "MarkdownV2"
            assert "tok" in post.call_args.args[0]

    def test_returns_false_on_exception(self):
        with patch.object(notify.config.telegram, "bot_token", "tok"), \
             patch.object(notify.config.telegram, "chat_id", "123"), \
             patch.object(notify.requests, "post", side_effect=RuntimeError("boom")):
            assert notify.send("hi") is False


# ---------------------------------------------------------------------------
# notify_run
# ---------------------------------------------------------------------------

class TestNotifyRun:
    def test_formats_then_sends(self):
        with patch.object(notify, "send", return_value=True) as send:
            assert notify.notify_run(["MSFT"], [], "note text") is True
            sent_text = send.call_args.args[0]
            assert "MSFT" in sent_text and "note text" in sent_text
