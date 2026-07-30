"""
The settings override layer: values you change from the dashboard, applied at run start.

Precedence is `code default → environment → Supabase override`, resolved once per process by
`load_and_apply()` before any decision code runs. The DB layer wins for tunables, so the
dashboard is the single place you turn a dial; secrets stay env-only and are never touched.

Two safety properties, both enforced here rather than in the UI:

  • Every numeric override is clamped to the range declared in `src/settings_schema.py`.
    A value outside the range is clamped and reported, never rejected silently — the same
    discipline `brain/playbook.apply_proposals` uses on LLM output.

  • On/off switches use AND semantics: effective = baseline AND override. A switch that is
    off in the environment can never be turned on from the dashboard. In practice this means
    the dashboard can always DISABLE real-money trading but can never ENABLE it — arming the
    Autotrader still requires the AGENT_TRADING_ENABLED secret.

Failure is never fatal. If Supabase is unreachable or the blob is malformed, the run proceeds
on the environment/code baseline and says so in the log — the same fail-open posture as the
contextual layer.

Attribution comes free: `brain_config_hash` already hashes the effective BRAIN_* values, so a
dial changed here automatically segments every decision and trade row logged afterwards.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from typing import Any

from src import config
from src.settings_schema import BY_KEY, GROUP_ORDER, REGISTRY, SECRET, Knob

logger = logging.getLogger(__name__)

# section name → the config singleton it lives on
SECTIONS: dict[str, Any] = {
    "supabase": config.supabase,
    "services": config.services,
    "reasoning": config.reasoning,
    "tool_loop": config.tool_loop,
    "paper": config.paper,
    "market_data": config.market_data,
    "rules": config.rules,
    "quality": config.quality,
    "tech": config.tech,
    "brain": config.brain,
    "robinhood": config.robinhood,
    "agent": config.agent,
    "exploration": config.exploration,
    "contextual": config.contextual,
    "timeouts": config.timeouts,
    "telegram": config.telegram,
    "scorecard": config.scorecard,
}


def _all_field_keys() -> set[str]:
    """Every 'section.field' key across the config singletons."""
    return {
        f"{section}.{f.name}"
        for section, obj in SECTIONS.items()
        for f in dataclasses.fields(obj)
    }


def _get(key: str) -> Any:
    section, field = key.split(".", 1)
    return getattr(SECTIONS[section], field)


def _set(key: str, value: Any) -> None:
    section, field = key.split(".", 1)
    setattr(SECTIONS[section], field, value)


def code_default(key: str) -> Any:
    """The literal default declared on the dataclass field — what env/DB layer over."""
    section, field = key.split(".", 1)
    for f in dataclasses.fields(SECTIONS[section]):
        if f.name == field:
            return f.default
    raise KeyError(key)


# Values as of import — after the environment has been read, before any DB override is
# applied. Captured once so `effective()` can attribute each value to its true source.
_BASELINE: dict[str, Any] = {k: _get(k) for k in _all_field_keys()}


def baseline(key: str) -> Any:
    return _BASELINE[key]


# ── Coercion + clamping ───────────────────────────────────────────────────────

class Rejected(ValueError):
    """An override that cannot be applied at all (unknown key, wrong type, not editable)."""


def coerce(knob: Knob, raw: Any) -> tuple[Any, str | None]:
    """Coerce `raw` to the field's type and clamp it into the declared range.

    Returns (value, note) where `note` describes a clamp that was applied, else None.
    Raises Rejected when the value cannot be interpreted as the field's type at all.
    """
    current = _BASELINE[knob.key]
    note: str | None = None

    if isinstance(current, bool):
        if isinstance(raw, str):
            raw = raw.strip().lower() in ("true", "1", "yes", "on")
        value: Any = bool(raw)
        # AND semantics — a switch off in the environment stays off.
        if current is False and value is True:
            return False, "kept off: disabled in the environment, which always wins"
        return value, None

    if isinstance(current, str):
        return str(raw), None

    if isinstance(current, (int, float)):
        try:
            value = float(raw)
        except (TypeError, ValueError) as e:
            raise Rejected(f"{knob.key}: {raw!r} is not a number") from e
        if knob.min is not None and value < knob.min:
            note = f"raised to the minimum {knob.min:g} (asked for {value:g})"
            value = knob.min
        if knob.max is not None and value > knob.max:
            note = f"lowered to the maximum {knob.max:g} (asked for {value:g})"
            value = knob.max
        if isinstance(current, int):
            value = int(round(value))
        return value, note

    raise Rejected(f"{knob.key}: unsupported field type {type(current).__name__}")


# ── Applying ──────────────────────────────────────────────────────────────────

def apply_overrides(overrides: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply a {key: value} override map onto the config singletons.

    Every entry is validated against the registry: unknown keys, secrets, locked hard rules
    and plumbing are all refused. Returns one audit record per entry — applied or not — so
    the caller can log exactly what a saved settings blob actually did.
    """
    report: list[dict[str, Any]] = []
    for key, raw in sorted(overrides.items()):
        knob = BY_KEY.get(key)
        if knob is None:
            report.append({"key": key, "applied": False, "reason": "unknown setting"})
            continue
        if not knob.editable:
            reason = (
                "locked — change it in code" if knob.locked
                else "not user-configurable"
            )
            report.append({"key": key, "applied": False, "reason": reason})
            continue
        try:
            value, note = coerce(knob, raw)
        except Rejected as e:
            report.append({"key": key, "applied": False, "reason": str(e)})
            continue
        before = _get(key)
        _set(key, value)
        report.append({
            "key": key, "applied": True, "from": before, "to": value, "note": note,
        })
    return report


def load_overrides() -> dict[str, Any]:
    """The saved override map from Supabase. Missing/malformed → empty (never raises)."""
    try:
        from src.memory import get_latest_settings
        blob = get_latest_settings() or {}
    except Exception as e:  # noqa: BLE001 — settings must never break a run
        logger.warning(f"settings: could not load overrides, using env/code baseline ({e})")
        return {}
    values = blob.get("values") if isinstance(blob, dict) else None
    if not isinstance(values, dict):
        return {}
    return values


def load_and_apply() -> list[dict[str, Any]]:
    """Fetch the saved overrides and apply them. Call once at process start, before the
    brain is used. Fail-open: any error leaves the env/code baseline in place."""
    overrides = load_overrides()
    report = apply_overrides(overrides) if overrides else []
    applied = [r for r in report if r["applied"]]
    refused = [r for r in report if not r["applied"]]
    for r in applied:
        suffix = f" — {r['note']}" if r.get("note") else ""
        logger.info(f"settings: {r['key']} {r['from']} → {r['to']}{suffix}")
    for r in refused:
        logger.warning(f"settings: ignored {r['key']} ({r['reason']})")
    logger.info(f"settings: {len(applied)} override(s) applied, {len(refused)} ignored")
    _record_effective()
    return report


def _record_effective() -> None:
    """Persist what the tunables resolved to, so the dashboard can show the truth this run
    used rather than re-deriving it without the environment layer.

    ONLY scheduled/production runs may write this. A developer machine has a different
    environment — an empty AGENT_TRADING_ENABLED in a local .env resolves the Autotrader
    switch to off — and the snapshot is shared state, so a local run would otherwise
    overwrite what the dashboard reports about production. Best-effort either way.
    """
    if not os.environ.get("GITHUB_ACTIONS"):
        logger.debug("settings: not a CI run — leaving the effective snapshot untouched")
        return
    try:
        from src.memory import save_settings_effective
        save_settings_effective(
            {row["key"]: row["value"] for row in effective(overrides={}) if row["tier"] != 3},
            source=os.environ.get("GITHUB_WORKFLOW") or "github-actions",
        )
    except Exception as e:  # noqa: BLE001 — display metadata, never break a run
        logger.warning(f"settings: could not record the effective snapshot ({e})")


# ── Introspection (drives the dashboard + the CLI view) ───────────────────────

def _source(key: str, overrides: dict[str, Any]) -> str:
    if key in overrides:
        return "dashboard"
    if _BASELINE[key] != code_default(key):
        return "environment"
    return "default"


def effective(overrides: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every non-secret setting with its effective value, origin and metadata.

    `overrides` defaults to what is saved in the DB; pass {} to describe a pristine run.
    """
    if overrides is None:
        overrides = load_overrides()
    out: list[dict[str, Any]] = []
    for knob in REGISTRY:
        if knob.tier == SECRET:
            continue
        out.append({
            "key": knob.key,
            "section": knob.section,
            "field": knob.field,
            "tier": knob.tier,
            "group": knob.group,
            "label": knob.label or knob.field.replace("_", " "),
            "help": knob.help,
            "impact": knob.impact,
            "unit": knob.unit,
            "min": knob.min,
            "max": knob.max,
            "step": knob.step,
            "locked": knob.locked,
            "editable": knob.editable,
            "type": type(_BASELINE[knob.key]).__name__,
            "value": _get(knob.key),
            "baseline": _BASELINE[knob.key],
            "default": code_default(knob.key),
            "source": _source(knob.key, overrides),
        })
    return out


def export_schema() -> dict[str, Any]:
    """The full settings description the dashboard renders, as a JSON-ready dict.

    Values here are the code defaults, not live values — the dashboard reads current values
    from the `settings` table so this file can be served statically from GitHub Pages.
    """
    return {
        "groups": list(GROUP_ORDER),
        "settings": [
            {k: v for k, v in row.items() if k not in ("value", "baseline", "source")}
            for row in effective(overrides={})
        ],
    }


if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path

    _p = argparse.ArgumentParser(description="ASTRA settings — export the schema or show effective values")
    _p.add_argument("--export", metavar="PATH", help="write the dashboard settings schema to PATH")
    _p.add_argument("--tier", type=int, action="append",
                    help="restrict the printed table to these tiers (repeatable; default 1)")
    _args = _p.parse_args()

    if _args.export:
        _out = Path(_args.export)
        _out.write_text(json.dumps(export_schema(), indent=2) + "\n")
        print(f"wrote {_out} ({len(export_schema()['settings'])} settings)")
    else:
        _tiers = set(_args.tier or [1])
        _rows = [r for r in effective() if r["tier"] in _tiers]
        _w = max(len(r["key"]) for r in _rows)
        print(f"{'SETTING'.ljust(_w)}  {'VALUE':>12}  {'DEFAULT':>12}  SOURCE")
        for _r in _rows:
            _flag = "" if _r["source"] == "default" else "  *"
            print(f"{_r['key'].ljust(_w)}  {str(_r['value']):>12}  "
                  f"{str(_r['default']):>12}  {_r['source']}{_flag}")
