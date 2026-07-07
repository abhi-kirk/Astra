"""
Tests for src/observability.py — cost estimation, RunObserver collection, and flush.
No network / DB: get_client and requests are mocked.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src import observability
from src.observability import RunObserver, estimate_cost

# ---------------------------------------------------------------------------
# estimate_cost
# ---------------------------------------------------------------------------

class TestEstimateCost:
    def test_known_model(self):
        # Sonnet 4.6: $3/M in, $15/M out
        assert estimate_cost("claude-sonnet-4-6", 1_000_000, 1_000_000) == pytest.approx(18.0)

    def test_opus_pricing(self):
        assert estimate_cost("claude-opus-4-8", 1_000_000, 0) == pytest.approx(5.0)

    def test_unknown_model_falls_back(self):
        # unknown → Sonnet-tier default
        assert estimate_cost("mystery-model", 1_000_000, 0) == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# RunObserver collection
# ---------------------------------------------------------------------------

class TestRunObserver:
    def test_phase_records_timing(self):
        obs = RunObserver("2026-07-03", "simulation")
        with obs.phase("market_data"):
            pass
        assert "market_data" in obs.phases
        assert obs.phases["market_data"] >= 0

    def test_record_merges_metrics(self):
        obs = RunObserver("2026-07-03", "simulation")
        obs.record(num_signals=5, buy_count=1)
        obs.record(sell_count=4)
        assert obs.metrics == {"num_signals": 5, "buy_count": 1, "sell_count": 4}

    def test_record_advisor_with_usage(self):
        obs = RunObserver("2026-07-03", "simulation")
        usage = SimpleNamespace(input_tokens=100, output_tokens=200)
        obs.record_advisor("claude-sonnet-4-6", usage)
        assert obs.metrics["advisor_model"] == "claude-sonnet-4-6"
        assert obs.metrics["advisor_tokens_in"] == 100
        assert obs.metrics["advisor_tokens_out"] == 200
        assert obs.metrics["advisor_cost_usd"] == pytest.approx(0.0033)

    def test_record_advisor_without_usage(self):
        obs = RunObserver("2026-07-03", "simulation")
        obs.record_advisor("claude-sonnet-4-6", None)
        assert obs.metrics["advisor_model"] == "claude-sonnet-4-6"
        assert "advisor_tokens_in" not in obs.metrics

    def test_record_service(self):
        obs = RunObserver("2026-07-03", "simulation")
        obs.record_service("supabase", ok=True, latency_ms=42)
        obs.record_service("fmp_mcp", ok=False, detail="boom")
        assert obs.services[0] == {"service": "supabase", "ok": True, "latency_ms": 42, "detail": None}
        assert obs.services[1]["ok"] is False and obs.services[1]["detail"] == "boom"

    def test_fail_sets_status(self):
        obs = RunObserver("2026-07-03", "simulation")
        obs.fail(RuntimeError("kaboom"))
        assert obs.status == "failed"
        assert "kaboom" in obs.error


# ---------------------------------------------------------------------------
# flush
# ---------------------------------------------------------------------------

class TestFlush:
    def test_flush_inserts_metrics_and_health(self):
        obs = RunObserver("2026-07-03", "simulation")
        obs.record(num_signals=3)
        obs.record_service("supabase", ok=True, latency_ms=10)

        client = MagicMock()
        with patch.object(observability, "get_client", return_value=client):
            obs.flush()

        # two tables written: run_metrics + service_health
        tables = [c.args[0] for c in client.table.call_args_list]
        assert "run_metrics" in tables and "service_health" in tables

    def test_flush_never_raises(self):
        obs = RunObserver("2026-07-03", "simulation")
        with patch.object(observability, "get_client", side_effect=RuntimeError("db down")):
            obs.flush()  # must not raise

    def test_flush_skips_health_when_empty(self):
        obs = RunObserver("2026-07-03", "simulation")
        client = MagicMock()
        with patch.object(observability, "get_client", return_value=client):
            obs.flush()
        tables = [c.args[0] for c in client.table.call_args_list]
        assert tables == ["run_metrics"]  # no service_health insert


# ---------------------------------------------------------------------------
# probe_endpoints
# ---------------------------------------------------------------------------

class TestProbeEndpoints:
    def test_records_supabase_and_reachable_mcp(self):
        obs = RunObserver("2026-07-03", "simulation")
        resp = MagicMock(status_code=200)
        with patch.object(observability, "get_client", return_value=MagicMock()), \
             patch.object(observability.requests, "get", return_value=resp), \
             patch.object(observability.config.services, "tavily_mcp_url", "https://tavily.example/mcp"), \
             patch.object(observability.config.services, "alpha_vantage_api_key", ""), \
             patch.object(observability.config.services, "fmp_api_key", ""), \
             patch.object(observability.config.services, "sec_edgar_mcp_url", ""):
            observability.probe_endpoints(obs)
        names = {s["service"] for s in obs.services}
        assert "supabase" in names and "tavily_mcp" in names
        assert next(s for s in obs.services if s["service"] == "tavily_mcp")["ok"] is True

    def test_supabase_failure_recorded(self):
        obs = RunObserver("2026-07-03", "simulation")
        with patch.object(observability, "get_client", side_effect=RuntimeError("no db")), \
             patch.object(observability.config.services, "tavily_mcp_url", ""), \
             patch.object(observability.config.services, "alpha_vantage_api_key", ""), \
             patch.object(observability.config.services, "fmp_api_key", ""), \
             patch.object(observability.config.services, "sec_edgar_mcp_url", ""):
            observability.probe_endpoints(obs)
        supa = next(s for s in obs.services if s["service"] == "supabase")
        assert supa["ok"] is False
