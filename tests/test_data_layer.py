"""
Tests for src/data_layer.get_portfolio — it must PREFER the live account snapshot and only
fall back to the trades ledger when there's genuinely no snapshot. Regression guard for the
tz-aware/naive age bug that silently dropped every cloud run onto stale ledger cost bases.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src import data_layer
from src.data_layer import get_portfolio

_SNAP_POSITIONS = {"NVDA": {"shares": 10.0, "avg_cost": 100.0}}


def _snapshot(ts: str):
    return {"snapshot_time": ts, "positions": _SNAP_POSITIONS}


class TestGetPortfolioPrefersSnapshot:
    def test_uses_tz_aware_snapshot_without_falling_back(self):
        # Supabase returns an offset-aware timestamp ("+00:00"); the age calc must not raise
        # and silently drop us to the ledger (the bug).
        ts = datetime.now(timezone.utc).isoformat()
        with patch("src.memory.get_latest_portfolio_snapshot", return_value=_snapshot(ts)), \
             patch.object(data_layer, "get_cost_basis_from_db", side_effect=AssertionError("must not fall back")):
            assert get_portfolio() == _SNAP_POSITIONS

    def test_stale_snapshot_still_wins_over_ledger(self):
        ts = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        with patch("src.memory.get_latest_portfolio_snapshot", return_value=_snapshot(ts)), \
             patch.object(data_layer, "get_cost_basis_from_db", side_effect=AssertionError("must not fall back")):
            assert get_portfolio() == _SNAP_POSITIONS

    def test_unparseable_snapshot_time_still_returns_snapshot(self):
        with patch("src.memory.get_latest_portfolio_snapshot", return_value=_snapshot("not-a-date")), \
             patch.object(data_layer, "get_cost_basis_from_db", side_effect=AssertionError("must not fall back")):
            assert get_portfolio() == _SNAP_POSITIONS

    def test_falls_back_to_ledger_only_when_no_snapshot(self):
        ledger = {"AMZN": {"shares": 5.0, "avg_cost": 150.0}}
        with patch("src.memory.get_latest_portfolio_snapshot", return_value=None), \
             patch.object(data_layer, "get_cost_basis_from_db", return_value=ledger):
            assert get_portfolio() == ledger
