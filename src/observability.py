"""
Run observability — captures per-run engineering metrics and per-service health,
then persists them to Supabase (`run_metrics` + `service_health`).

Designed to extend easily:
  - a scalar metric  → observer.record(key=value)
  - a timed phase    → with observer.phase("name"): ...
  - a service check  → observer.record_service(...) (derived) or probe_endpoints() (active)

RunObserver is instantiated once per pipeline run and flushed at the end — even on
failure. Flushing never raises, so observability can't break the pipeline.
"""

import logging
import time
from contextlib import contextmanager

import requests

from src import config
from src.db import get_client

logger = logging.getLogger(__name__)

# Anthropic pricing, $ per token (input, output). Source: claude-api skill, Jun 2026.
_PRICING = {
    "claude-sonnet-4-6": (3.0e-6, 15.0e-6),
    "claude-sonnet-5":   (3.0e-6, 15.0e-6),
    "claude-opus-4-8":   (5.0e-6, 25.0e-6),
    "claude-opus-4-7":   (5.0e-6, 25.0e-6),
    "claude-haiku-4-5":  (1.0e-6,  5.0e-6),
}
_DEFAULT_PRICE = (3.0e-6, 15.0e-6)  # Sonnet-tier fallback for unknown models


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Approximate $ cost of one Claude call from its token usage."""
    price_in, price_out = _PRICING.get(model, _DEFAULT_PRICE)
    return round(tokens_in * price_in + tokens_out * price_out, 4)


class RunObserver:
    """Collects metrics + service health during a run and persists them on flush()."""

    def __init__(self, run_date: str, mode: str):
        self.run_date = run_date
        self.mode = mode
        self._t0 = time.perf_counter()
        self.phases: dict[str, float] = {}
        self.metrics: dict = {}
        self.services: list[dict] = []
        self.status = "success"
        self.error: str | None = None

    @contextmanager
    def phase(self, name: str):
        """Time a named pipeline phase and log it (replaces logger.timer for phases)."""
        t = time.perf_counter()
        try:
            yield
        finally:
            dt = time.perf_counter() - t
            self.phases[name] = round(dt, 3)
            logger.info(f"TIMING  {name:<40}  {dt:.2f}s")

    def record(self, **kv) -> None:
        """Record scalar metrics (num_signals, positions_screened, ...)."""
        self.metrics.update(kv)

    def record_advisor(self, model: str, usage) -> None:
        """Capture advisor Claude model + token usage + estimated cost."""
        self.metrics["advisor_model"] = model
        tin = getattr(usage, "input_tokens", None) if usage else None
        tout = getattr(usage, "output_tokens", None) if usage else None
        if tin is not None and tout is not None:
            self.metrics["advisor_tokens_in"] = tin
            self.metrics["advisor_tokens_out"] = tout
            self.metrics["advisor_cost_usd"] = estimate_cost(model, tin, tout)

    def record_service(self, service: str, ok: bool,
                       latency_ms: int | None = None, detail: str | None = None) -> None:
        """Record health of one service (external API, MCP, infra)."""
        self.services.append({
            "service": service,
            "ok": bool(ok),
            "latency_ms": latency_ms,
            "detail": (detail or "")[:300] or None,
        })

    def fail(self, error) -> None:
        """Mark the run as failed with an error message."""
        self.status = "failed"
        self.error = str(error)[:500]

    def flush(self) -> None:
        """Persist run_metrics + service_health rows. Never raises."""
        duration = round(time.perf_counter() - self._t0, 2)
        row = {
            "run_date": self.run_date,
            "mode": self.mode,
            "status": self.status,
            "duration_s": duration,
            "phase_timings": self.phases or None,
            "error": self.error,
            **self.metrics,
        }
        try:
            client = get_client()
            client.table("run_metrics").insert(row).execute()
            if self.services:
                for s in self.services:
                    s["run_date"] = self.run_date
                client.table("service_health").insert(self.services).execute()
            ok_count = sum(1 for s in self.services if s["ok"])
            logger.info(f"Observability flushed — status={self.status}  duration={duration}s  "
                        f"services={ok_count}/{len(self.services)} healthy")
        except Exception:
            logger.error("Observability flush failed — non-fatal", exc_info=True)


def probe_endpoints(observer: RunObserver) -> None:
    """Active health checks for infra + MCP endpoints, recorded on the observer.

    Supabase is probed via a cheap REST round-trip; MCP/HTTP endpoints via a
    lightweight reachability request (any HTTP response < 500 = the server is up).
    """
    # Supabase — cheap authenticated read
    t = time.perf_counter()
    try:
        get_client().table("run_summaries").select("id").limit(1).execute()
        observer.record_service("supabase", ok=True,
                                latency_ms=int((time.perf_counter() - t) * 1000))
    except Exception as e:
        observer.record_service("supabase", ok=False, detail=str(e))

    # MCP / HTTP endpoints — reachability only
    endpoints = {
        "tavily_mcp": config.services.tavily_mcp_url,
        "alpha_vantage_mcp": (
            f"https://mcp.alphavantage.co/mcp?apikey={config.services.alpha_vantage_api_key}"
            if config.services.alpha_vantage_api_key else ""
        ),
        "fmp_mcp": (
            f"https://financialmodelingprep.com/mcp?apikey={config.services.fmp_api_key}"
            if config.services.fmp_api_key else ""
        ),
        "sec_edgar_mcp": config.services.sec_edgar_mcp_url,
    }
    for name, url in endpoints.items():
        if not url:
            continue
        t = time.perf_counter()
        try:
            resp = requests.get(url, timeout=8)
            observer.record_service(name, ok=resp.status_code < 500,
                                    latency_ms=int((time.perf_counter() - t) * 1000),
                                    detail=f"HTTP {resp.status_code}")
        except Exception as e:
            observer.record_service(name, ok=False, detail=str(e))
