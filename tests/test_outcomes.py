"""
Tests for src/outcomes.py.

DB and yfinance calls are mocked — tests cover the diff/attribution logic only.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.outcomes import (
    _parse_snapshot_date,
    detect_portfolio_changes,
)

# ---------------------------------------------------------------------------
# _parse_snapshot_date
# ---------------------------------------------------------------------------

class TestParseSnapshotDate:
    def test_iso_timestamp(self):
        assert _parse_snapshot_date("2026-06-30T13:00:00+00:00") == "2026-06-30"

    def test_postgres_timestamp(self):
        assert _parse_snapshot_date("2026-06-30 13:00:00.123456+00") == "2026-06-30"

    def test_date_only(self):
        assert _parse_snapshot_date("2026-06-30") == "2026-06-30"


# ---------------------------------------------------------------------------
# detect_portfolio_changes — diff logic via mock DB
# ---------------------------------------------------------------------------

def _make_mock_db(snapshots, recent_signals=None):
    """Return a mock Supabase client that returns the given snapshot and signal data.

    Tables are cached so callers can configure return values before passing the
    mock into the function under test (same object returned on every table() call).
    """
    recent_signals = recent_signals or []
    _cache: dict = {}

    def _make_table(name):
        t = MagicMock()
        if name == "portfolio_snapshots":
            t.select.return_value.order.return_value.limit.return_value.execute.return_value.data = snapshots
        elif name == "decisions":
            t.select.return_value.in_.return_value.gte.return_value.order.return_value.execute.return_value.data = recent_signals
        elif name == "user_trades_log":
            t.update.return_value.lt.return_value.eq.return_value.execute.return_value.data = []
            t.upsert.return_value.execute.return_value.data = []
        return t

    def table(name):
        if name not in _cache:
            _cache[name] = _make_table(name)
        return _cache[name]

    mock_db = MagicMock()
    mock_db.table.side_effect = table
    return mock_db


def _snap(positions: dict, time_offset_hours: int = 0):
    ts = (datetime(2026, 6, 30, 14, 0, tzinfo=timezone.utc) + timedelta(hours=time_offset_hours)).isoformat()
    return {"positions": positions, "snapshot_time": ts}


def _capture_upsert(mock_db):
    """Wire capture on the cached user_trades_log mock. Must be called before patch."""
    upserted = []
    mock_db.table("user_trades_log").upsert.side_effect = (
        lambda rows, **kw: (upserted.extend(rows), MagicMock())[1]
    )
    return upserted


class TestDetectPortfolioChanges:
    def test_buy_detected(self):
        prev = _snap({"NVDA": {"shares": 10.0, "equity": 1420.0}}, time_offset_hours=-24)
        curr = _snap({"NVDA": {"shares": 15.0, "equity": 2130.0}})
        mock_db = _make_mock_db([curr, prev])
        upserted = _capture_upsert(mock_db)

        with patch("src.outcomes.get_client", return_value=mock_db):
            detect_portfolio_changes()

        assert len(upserted) == 1
        assert upserted[0]["ticker"] == "NVDA"
        assert upserted[0]["action"] == "buy"
        assert abs(upserted[0]["shares_delta"] - 5.0) < 0.001

    def test_sell_detected(self):
        prev = _snap({"AAPL": {"shares": 20.0, "equity": 3600.0}}, time_offset_hours=-24)
        curr = _snap({"AAPL": {"shares": 5.0, "equity": 900.0}})
        mock_db = _make_mock_db([curr, prev])
        upserted = _capture_upsert(mock_db)

        with patch("src.outcomes.get_client", return_value=mock_db):
            detect_portfolio_changes()

        assert upserted[0]["action"] == "sell"
        assert abs(upserted[0]["shares_delta"] - 15.0) < 0.001

    def test_noise_below_threshold_ignored(self):
        prev = _snap({"NVDA": {"shares": 10.000, "equity": 1420.0}}, time_offset_hours=-24)
        curr = _snap({"NVDA": {"shares": 10.005, "equity": 1420.7}})  # < 0.01 share delta
        mock_db = _make_mock_db([curr, prev])
        upserted = _capture_upsert(mock_db)

        with patch("src.outcomes.get_client", return_value=mock_db):
            detect_portfolio_changes()

        assert upserted == []

    def test_astra_suspicion_set_when_signal_matches(self):
        prev = _snap({"NVDA": {"shares": 10.0}}, time_offset_hours=-24)
        curr = _snap({"NVDA": {"shares": 13.0}})
        signal = {
            "id": 42,
            "ticker": "NVDA",
            "action": "buy",
            "run_date": "2026-06-29T13:00:00+00:00",
            "price_at_decision": 142.50,
        }
        mock_db = _make_mock_db([curr, prev], recent_signals=[signal])
        upserted = _capture_upsert(mock_db)

        with patch("src.outcomes.get_client", return_value=mock_db):
            detect_portfolio_changes()

        assert upserted[0]["astra_suspicion"] is True
        assert upserted[0]["astra_signal_id"] == 42
        assert "$142.50" in upserted[0]["astra_suspicion_reason"]

    def test_no_suspicion_when_no_signal(self):
        prev = _snap({"CRSR": {"shares": 0.0}}, time_offset_hours=-24)
        curr = _snap({"CRSR": {"shares": 50.0}})
        mock_db = _make_mock_db([curr, prev], recent_signals=[])
        upserted = _capture_upsert(mock_db)

        with patch("src.outcomes.get_client", return_value=mock_db):
            detect_portfolio_changes()

        assert upserted[0]["astra_suspicion"] is False
        assert upserted[0]["astra_signal_id"] is None

    def test_new_position_detected_as_buy(self):
        prev = _snap({})
        curr = _snap({"RKLB": {"shares": 100.0, "equity": 1500.0}})
        mock_db = _make_mock_db([curr, prev])
        upserted = _capture_upsert(mock_db)

        with patch("src.outcomes.get_client", return_value=mock_db):
            detect_portfolio_changes()

        assert upserted[0]["ticker"] == "RKLB"
        assert upserted[0]["action"] == "buy"

    def test_closed_position_detected_as_sell(self):
        prev = _snap({"SPCE": {"shares": 3404.0, "equity": 5000.0}})
        curr = _snap({})
        mock_db = _make_mock_db([curr, prev])
        upserted = _capture_upsert(mock_db)

        with patch("src.outcomes.get_client", return_value=mock_db):
            detect_portfolio_changes()

        assert upserted[0]["ticker"] == "SPCE"
        assert upserted[0]["action"] == "sell"

    def test_skips_when_only_one_snapshot(self):
        curr = _snap({"NVDA": {"shares": 10.0}})
        mock_db = _make_mock_db([curr])  # only one snapshot
        upserted = _capture_upsert(mock_db)

        with patch("src.outcomes.get_client", return_value=mock_db):
            detect_portfolio_changes()

        assert upserted == []
