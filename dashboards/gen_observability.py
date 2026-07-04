"""
Generate the ASTRA Observability Grafana dashboard JSON from concise code.

Run:  .venv/bin/python dashboards/gen_observability.py   →  dashboards/observability.json
Then push to Grafana (via the mcp-grafana update_dashboard tool, overwrite=true) — the
datasource UID is hardcoded (DS_UID), so no per-import datasource binding is needed.

Reads Supabase tables run_metrics + service_health via a read-only Postgres datasource.
Queries validated against Supabase before writing this file.
"""

from pathlib import Path

from grafana_foundation_sdk.builders import common, stat, statetimeline, table, timeseries
from grafana_foundation_sdk.builders.dashboard import Dashboard
from grafana_foundation_sdk.cog.encoder import JSONEncoder
from grafana_foundation_sdk.models.common import StackingMode
from grafana_foundation_sdk.models.dashboard import (
    DataSourceRef,
    GridPos,
    Threshold,
    ThresholdsConfig,
    ThresholdsMode,
    ValueMap,
    ValueMappingResult,
)

PG_TYPE = "grafana-postgresql-datasource"
DS_UID = "afr1qkaakhudca"                 # hardcoded — Supabase read-only Postgres datasource
DS_REF = DataSourceRef(type_val=PG_TYPE, uid=DS_UID)


class Sql:
    """Raw-SQL target for a Postgres datasource (the SDK has no native pg query builder)."""
    def __init__(self, raw: str, fmt: str = "time_series", ref: str = "A"):
        self.raw, self.fmt, self.ref = raw, fmt, ref

    def build(self):
        return {"refId": self.ref, "format": self.fmt, "rawSql": self.raw,
                "datasource": {"type": PG_TYPE, "uid": DS_UID}}


class _Built:
    """Wrap a built model where the SDK expects a cog builder (has .build())."""
    def __init__(self, model):
        self._model = model

    def build(self):
        return self._model


# --- simple 24-col grid layout tracker ---
_cur = {"x": 0, "y": 0, "row_h": 0}

def place(panel, w: int, h: int):
    if _cur["x"] + w > 24:
        _cur["x"] = 0
        _cur["y"] += _cur["row_h"]
        _cur["row_h"] = 0
    panel.grid_pos(GridPos(h=h, w=w, x=_cur["x"], y=_cur["y"]))
    _cur["x"] += w
    _cur["row_h"] = max(_cur["row_h"], h)
    return panel


def stat_panel(title, sql, unit="", fmt="table", w=6, h=4, thresholds=None, mappings=None, all_fields=False):
    p = stat.Panel().title(title).datasource(DS_REF).unit(unit).with_target(Sql(sql, fmt=fmt))
    if all_fields:  # let a non-numeric (text) field be the displayed value
        p = p.reduce_options(common.ReduceDataOptions().calcs(["lastNotNull"]).fields("/.*/"))
    if mappings:
        p = p.mappings(mappings).color_mode("background")
    if thresholds:
        p = p.thresholds(thresholds)
    return place(p, w, h)

def ts_panel(title, sql, unit="", w=12, h=8, stack=False):
    p = timeseries.Panel().title(title).datasource(DS_REF).unit(unit).fill_opacity(10) \
        .with_target(Sql(sql, fmt="time_series"))
    if stack:
        p = p.stacking(common.StackingConfig().mode(StackingMode.NORMAL))
    return place(p, w, h)

def table_panel(title, sql, w=12, h=8, mappings=None):
    p = table.Panel().title(title).datasource(DS_REF).with_target(Sql(sql, fmt="table"))
    if mappings:
        p = p.mappings(mappings)
    return place(p, w, h)

def state_panel(title, sql, w=24, h=6):
    return place(statetimeline.Panel().title(title).datasource(DS_REF)
                 .with_target(Sql(sql, fmt="time_series")), w, h)


# thresholds for run duration (seconds): green normal, amber approaching the 600s advisor cap, red at/over
DURATION_THRESHOLDS = _Built(ThresholdsConfig(mode=ThresholdsMode.ABSOLUTE, steps=[
    Threshold(color="green"), Threshold(value=480, color="orange"), Threshold(value=600, color="red"),
]))
STATUS_MAPPINGS = [ValueMap(options={
    "success": ValueMappingResult(color="green", index=0),
    "partial": ValueMappingResult(color="orange", index=1),
    "failed":  ValueMappingResult(color="red", index=2),
})]
OK_MAPPINGS = [ValueMap(options={
    "1": ValueMappingResult(text="✓ up", color="green", index=0),
    "0": ValueMappingResult(text="✗ down", color="red", index=1),
})]

TF = "$__timeFilter(run_date)"

d = (
    Dashboard("ASTRA — Observability")
    .uid("astra-observability")
    .tags(["astra", "observability"])
    .refresh("5m")
    .time("now-30d", "now")
    # Row 1 — stat tiles
    .with_panel(stat_panel("Last run status",
        "select status from run_metrics order by run_date desc limit 1",
        all_fields=True, mappings=STATUS_MAPPINGS))
    .with_panel(stat_panel("Last duration",
        "select duration_s from run_metrics order by run_date desc limit 1",
        unit="s", thresholds=DURATION_THRESHOLDS))
    .with_panel(stat_panel("Last advisor cost",
        "select advisor_cost_usd from run_metrics order by run_date desc limit 1", unit="currencyUSD"))
    .with_panel(stat_panel("Runs (7d)",
        "select count(*) as runs from run_metrics where run_date > now() - interval '7 days'"))
    # Row 2
    .with_panel(ts_panel("Run duration",
        f'select run_date as "time", duration_s from run_metrics where {TF} order by 1', unit="s"))
    .with_panel(ts_panel("Phase latency",
        f'select run_date as "time",'
        f" (phase_timings->>'robinhood_sync')::float as robinhood,"
        f" (phase_timings->>'market_data')::float as market_data,"
        f" (phase_timings->>'screening')::float as screening,"
        f" (phase_timings->>'advisor')::float as advisor"
        f" from run_metrics where {TF} order by 1", unit="s", stack=True))
    # Row 3
    .with_panel(ts_panel("Advisor cost per run + cumulative",
        f'select run_date as "time", advisor_cost_usd as per_run,'
        f" sum(coalesce(advisor_cost_usd,0)) over (order by run_date) as cumulative"
        f" from run_metrics where {TF} order by 1", unit="currencyUSD"))
    .with_panel(ts_panel("Signals per run",
        f'select run_date as "time", buy_count as buy, sell_count as sell, watch_count as watch'
        f" from run_metrics where {TF} order by 1"))
    # Row 4
    .with_panel(ts_panel("Service latency",
        f'select run_date as "time", service, latency_ms from service_health'
        f" where latency_ms is not null and {TF} order by 1", unit="ms"))
    .with_panel(table_panel("Service health (latest per service)",
        "select distinct on (service) service, ok::int as ok, latency_ms, detail"
        " from service_health order by service, run_date desc", mappings=OK_MAPPINGS))
    # Row 5
    .with_panel(state_panel("Run status history",
        f'select run_date as "time", status from run_metrics where {TF} order by 1'))
)

out = Path(__file__).parent / "observability.json"
out.write_text(JSONEncoder(sort_keys=True, indent=2).encode(d.build()))
print(f"wrote {out}  ({out.stat().st_size} bytes)")
