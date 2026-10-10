"""Tests for src/notify.py. No network: requests.post is mocked."""

from unittest.mock import MagicMock, patch

from src import notify

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
# notify_exploration
# ---------------------------------------------------------------------------

class TestNotifyExploration:
    def test_lists_tickers_without_the_writeup(self):
        candidates = [
            {"ticker": "RKLB", "source_theme": "space_infra",
             "claude_conviction": "high", "rationale": "Neutron cadence ramping."},
            {"ticker": "ASTS", "source_theme": "space_infra",
             "claude_conviction": "medium", "rationale": "Direct-to-cell coverage."},
        ]
        with patch.object(notify, "send", return_value=True) as send:
            assert notify.notify_exploration(candidates, themes_explored=3, already_tracked=5) is True
            sent = send.call_args.args[0]
            assert "RKLB" in sent and "ASTS" in sent
            assert "Neutron cadence" not in sent   # the daily note carries the reason
            assert "2" in sent
            assert "3 theme" in sent
            assert "5 already known" in sent

    def test_empty_still_sends_nothing_new(self):
        with patch.object(notify, "send", return_value=True) as send:
            assert notify.notify_exploration([], themes_explored=4, already_tracked=7) is True
            sent = send.call_args.args[0]
            assert "no new names" in sent.lower()
            assert "4 theme" in sent

    def test_output_is_string(self):
        with patch.object(notify, "send", side_effect=lambda t: t):
            out = notify.notify_exploration(
                [{"ticker": "AA", "source_theme": "b", "claude_conviction": "high"}],
                themes_explored=1, already_tracked=0,
            )
            assert isinstance(out, str) and "AA" in out


# ---------------------------------------------------------------------------
# notify_scorecard
# ---------------------------------------------------------------------------

def _result(verdict="STEADY", **kw):
    base = {
        "track": "autotrader",
        "verdict": verdict,
        "ready": True,
        "nav_days": 118,
        "missing": [],
        "eta_days": None,
        "exec_cum_excess_adj": 0.032,
        "exec_cum_excess_raw": -0.004,
        "exec_ci_low": 0.004,
        "exec_ci_high": 0.061,
        "exec_ir_annual": 0.61,
        "pick_hit_rate": 0.63,
        "pick_window_days": 30,
        "pick_clustered_excess": 2.9,
        "distinct_tickers": 14,
        "reasons": ["Because the numbers said so."],
        "diagnostic": None,
    }
    base.update(kw)
    return base


class TestNotifyScorecard:
    def test_renders_the_transition_and_the_track(self):
        with patch.object(notify, "send", side_effect=lambda t: t):
            out = notify.notify_scorecard(_result("SCALE"), "STEADY")
        assert "STEADY" in out and "SCALE" in out
        assert "Autotrader" in out

    def test_carries_the_confidence_interval_not_just_the_point_estimate(self):
        # A verdict here argues for moving real money; a bare number would overstate what is known.
        with patch.object(notify, "send", side_effect=lambda t: t):
            out = notify.notify_scorecard(_result("SCALE"), "STEADY")
        plain = out.replace("\\", "")  # MarkdownV2 escapes '+' and '.'
        assert "+3.2%" in plain
        assert "+0.4%" in plain and "+6.1%" in plain

    def test_insufficient_data_lists_what_is_missing_and_an_eta(self):
        result = _result("INSUFFICIENT_DATA", missing=["nav days 13/90"], eta_days=112)
        with patch.object(notify, "send", side_effect=lambda t: t):
            out = notify.notify_scorecard(result, "STEADY")
        assert "nav days 13/90" in out
        assert "112" in out

    def test_tune_states_that_nothing_was_changed(self):
        # Load-bearing: the module's promise is that it observes and never self-tunes.
        result = _result("TUNE", diagnostic={"fingerprints": [
            {"name": "cash-drag", "detail": "Only 65% of the account was invested."},
        ]})
        with patch.object(notify, "send", side_effect=lambda t: t):
            out = notify.notify_scorecard(result, "STEADY")
        assert "cash" in out.lower()
        assert "no settings were changed" in out.lower()

    def test_non_tune_verdicts_omit_the_disclaimer(self):
        with patch.object(notify, "send", side_effect=lambda t: t):
            out = notify.notify_scorecard(_result("SCALE"), "STEADY")
        assert "no settings were changed" not in out.lower()

    def test_markdown_is_converted_for_telegram(self):
        with patch.object(notify, "send", side_effect=lambda t: t):
            out = notify.notify_scorecard(_result("SCALE"), "STEADY")
        assert "**" not in out

    def test_first_verdict_renders_without_a_previous(self):
        with patch.object(notify, "send", side_effect=lambda t: t):
            out = notify.notify_scorecard(_result("SCALE"), None)
        assert "SCALE" in out

    def test_missing_statistics_do_not_crash_the_message(self):
        result = _result("STEADY", exec_cum_excess_adj=None, pick_hit_rate=None,
                         pick_clustered_excess=None, exec_ci_low=None, exec_ci_high=None)
        with patch.object(notify, "send", side_effect=lambda t: t):
            out = notify.notify_scorecard(result, "SCALE")
        assert "n/a" in out
