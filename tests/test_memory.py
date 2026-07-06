"""
Unit tests for the pure decision-log dedup gate in src/memory.py.

A persistent signal (e.g. a profit-take that stays >60% up for weeks) must re-log
weekly, not on every daily run — otherwise `decisions` fills with duplicates that
pollute the advisor's history context and any future outcome stats.
"""

from src.memory import should_log_decision

NOW = "2026-07-06T13:00:00+00:00"


def test_no_prior_decision_logs():
    assert should_log_decision(None, "sell", NOW) is True


def test_different_action_logs():
    last = {"action": "watch", "run_date": "2026-07-05T13:00:00+00:00"}
    assert should_log_decision(last, "buy", NOW) is True


def test_same_action_yesterday_skips():
    last = {"action": "sell", "run_date": "2026-07-05T13:00:00+00:00"}
    assert should_log_decision(last, "sell", NOW) is False


def test_same_action_a_week_ago_relogs():
    last = {"action": "sell", "run_date": "2026-06-29T13:00:00+00:00"}
    assert should_log_decision(last, "sell", NOW) is True


def test_naive_and_z_suffixed_timestamps_are_handled():
    last = {"action": "watch", "run_date": "2026-07-05T13:00:00Z"}
    assert should_log_decision(last, "watch", "2026-07-06T13:00:00") is False


def test_unparseable_timestamp_fails_open_and_logs():
    last = {"action": "sell", "run_date": "not-a-date"}
    assert should_log_decision(last, "sell", NOW) is True
