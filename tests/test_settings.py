"""
Tests for the settings registry + override layer.

The completeness tests are the important ones: they make registry drift impossible. A new
config field, or a registry entry for a field that no longer exists, fails the suite — so
nothing can silently become invisible to (or unclassifiable by) the dashboard.
"""

from __future__ import annotations

import dataclasses

import pytest

from src import config, settings
from src.brain import params
from src.settings_schema import BY_KEY, DIAL, PLUMBING, REGISTRY, SECRET, SHAPE

# ── Registry completeness (drift guards) ──────────────────────────────────────

def test_every_config_field_is_classified():
    """Every field on every config singleton has a registry entry with a tier."""
    missing = sorted(settings._all_field_keys() - set(BY_KEY))
    assert not missing, (
        f"{len(missing)} config field(s) missing from src/settings_schema.py: {missing}. "
        "Classify each one (SECRET / DIAL / SHAPE / PLUMBING)."
    )


def test_registry_has_no_orphans():
    """Every registry entry points at a config field that actually exists."""
    orphans = sorted(set(BY_KEY) - settings._all_field_keys())
    assert not orphans, f"registry entries for non-existent fields: {orphans}"


def test_registry_keys_are_unique():
    keys = [k.key for k in REGISTRY]
    assert len(keys) == len(set(keys)), "duplicate keys in the settings registry"


def test_user_facing_knobs_are_documented():
    """Anything shown in the UI carries plain-language copy and a display group."""
    for knob in REGISTRY:
        if knob.tier in (DIAL, SHAPE):
            assert knob.label, f"{knob.key}: missing label"
            assert knob.help, f"{knob.key}: missing help text"
            assert knob.impact, f"{knob.key}: missing impact text"
            assert knob.group, f"{knob.key}: missing group"


def test_numeric_knobs_declare_a_range():
    """Editable numeric settings must declare bounds — the applier clamps to them, so a
    missing range would mean an unbounded value reaching the brain."""
    for knob in REGISTRY:
        if not knob.editable:
            continue
        value = settings.code_default(knob.key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        assert knob.min is not None, f"{knob.key}: editable number with no min"
        assert knob.max is not None, f"{knob.key}: editable number with no max"
        assert knob.min <= value <= knob.max, (
            f"{knob.key}: default {value} sits outside its own declared range "
            f"[{knob.min}, {knob.max}]"
        )


def test_secrets_and_plumbing_are_never_editable():
    for knob in REGISTRY:
        if knob.tier in (SECRET, PLUMBING):
            assert not knob.editable, f"{knob.key}: tier {knob.tier} must not be editable"


def test_secrets_are_excluded_from_the_exported_schema():
    """The schema is served publicly from GitHub Pages — no secret key may appear in it."""
    exported = {row["key"] for row in settings.export_schema()["settings"]}
    secrets = {k.key for k in REGISTRY if k.tier == SECRET}
    assert not (exported & secrets)
    # and nothing that looks like a credential leaked in under another tier
    assert not [k for k in exported if k.endswith(("_key", "_token", "token_key"))]


def test_locked_hard_rules_cover_the_agent_rules():
    """The rules CLAUDE.md calls hard constraints must be present and locked."""
    for key in ("rules.max_position_pct", "rules.max_theme_pct",
                "rules.averaging_down_drawdown", "rules.averaging_down_max_buys",
                "agent.drawdown_halt_pct"):
        assert BY_KEY[key].locked, f"{key} must be locked"
        assert not BY_KEY[key].editable


# ── Coercion + clamping ───────────────────────────────────────────────────────

def test_numeric_override_is_clamped_to_the_declared_range():
    knob = BY_KEY["brain.f_global"]
    value, note = settings.coerce(knob, 99.0)
    assert value == knob.max
    assert note and "maximum" in note

    value, note = settings.coerce(knob, -5.0)
    assert value == knob.min
    assert note and "minimum" in note


def test_in_range_value_passes_through_unchanged():
    value, note = settings.coerce(BY_KEY["brain.f_global"], 0.12)
    assert value == pytest.approx(0.12)
    assert note is None


def test_int_fields_stay_ints():
    value, _ = settings.coerce(BY_KEY["agent.max_trades_per_day"], 4.7)
    assert value == 5
    assert isinstance(value, int)


def test_non_numeric_value_is_rejected():
    with pytest.raises(settings.Rejected):
        settings.coerce(BY_KEY["brain.f_global"], "not a number")


def test_switch_off_in_the_environment_cannot_be_turned_on(monkeypatch):
    """AND semantics: the dashboard may disable a switch but never enable one the
    environment has turned off. This is what keeps AGENT_TRADING_ENABLED a real kill switch."""
    monkeypatch.setitem(settings._BASELINE, "agent.trading_enabled", False)
    value, note = settings.coerce(BY_KEY["agent.trading_enabled"], True)
    assert value is False
    assert note and "environment" in note


def test_switch_on_in_the_environment_can_be_turned_off(monkeypatch):
    monkeypatch.setitem(settings._BASELINE, "agent.trading_enabled", True)
    value, note = settings.coerce(BY_KEY["agent.trading_enabled"], False)
    assert value is False
    assert note is None


# ── Applying ──────────────────────────────────────────────────────────────────

def test_apply_sets_the_live_config_value(monkeypatch):
    monkeypatch.setattr(config.brain, "f_global", 0.10)
    report = settings.apply_overrides({"brain.f_global": 0.15})
    assert report[0]["applied"] is True
    assert config.brain.f_global == pytest.approx(0.15)


def test_apply_refuses_secrets_locked_rules_and_unknown_keys(monkeypatch):
    before_key = config.supabase.service_key
    before_cap = config.rules.max_position_pct
    report = settings.apply_overrides({
        "supabase.service_key": "leaked",
        "rules.max_position_pct": 0.90,
        "timeouts.advisor": 5,
        "brain.not_a_real_field": 1,
    })
    by_key = {r["key"]: r for r in report}
    assert all(r["applied"] is False for r in report)
    assert config.supabase.service_key == before_key
    assert config.rules.max_position_pct == before_cap
    assert "locked" in by_key["rules.max_position_pct"]["reason"]
    assert by_key["brain.not_a_real_field"]["reason"] == "unknown setting"


def test_apply_reports_the_clamp_it_performed(monkeypatch):
    monkeypatch.setattr(config.brain, "cp_buy_threshold", 0.60)
    report = settings.apply_overrides({"brain.cp_buy_threshold": 5.0})
    assert report[0]["applied"] is True
    assert report[0]["to"] == BY_KEY["brain.cp_buy_threshold"].max
    assert "maximum" in report[0]["note"]


def test_load_and_apply_is_fail_open(monkeypatch):
    """A Supabase failure must leave the run on its env/code baseline, not crash it."""
    def _boom():
        raise RuntimeError("supabase down")
    monkeypatch.setattr("src.memory.get_latest_settings", _boom, raising=False)
    monkeypatch.setattr("src.memory.save_settings_effective", lambda _s: None, raising=False)
    assert settings.load_overrides() == {}
    assert settings.load_and_apply() == []


def test_recording_the_effective_snapshot_never_breaks_a_run(monkeypatch):
    """The snapshot is display metadata. A write failure must be swallowed."""
    def _boom(_snapshot):
        raise RuntimeError("write failed")
    monkeypatch.setattr("src.memory.get_latest_settings", lambda: None, raising=False)
    monkeypatch.setattr("src.memory.save_settings_effective", _boom, raising=False)
    assert settings.load_and_apply() == []


def test_local_runs_never_overwrite_the_reported_settings(monkeypatch):
    """The effective snapshot is shared state the dashboard presents as the truth about
    production. A dev machine resolves a different environment (an empty
    AGENT_TRADING_ENABLED reads as off), so only CI may write it."""
    called: list = []
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr("src.memory.get_latest_settings", lambda: None, raising=False)
    monkeypatch.setattr("src.memory.save_settings_effective",
                        lambda *a, **k: called.append(a), raising=False)
    settings.load_and_apply()
    assert not called, "a local run must not write the effective snapshot"


def test_ci_runs_do_record_the_snapshot_with_their_source(monkeypatch):
    captured: dict = {}
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_WORKFLOW", "Daily Analysis")
    monkeypatch.setattr("src.memory.get_latest_settings", lambda: None, raising=False)
    monkeypatch.setattr("src.memory.save_settings_effective",
                        lambda snap, source="": captured.update({"snap": snap, "source": source}),
                        raising=False)
    settings.load_and_apply()
    assert captured["source"] == "Daily Analysis"
    assert "agent.trading_enabled" in captured["snap"]


def test_effective_snapshot_excludes_plumbing_and_secrets(monkeypatch):
    """What gets written back for the dashboard carries no credentials."""
    captured: dict = {}
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr("src.memory.get_latest_settings", lambda: None, raising=False)
    monkeypatch.setattr("src.memory.save_settings_effective",
                        lambda snap, source="": captured.update(snap), raising=False)
    settings.load_and_apply()
    assert captured, "expected an effective snapshot to be recorded"
    assert not [k for k in captured if k.startswith(("supabase.", "services.", "telegram.",
                                                     "robinhood."))]
    assert "timeouts.advisor" not in captured
    assert captured["brain.cp_buy_threshold"] == config.brain.cp_buy_threshold


def test_load_overrides_tolerates_a_malformed_blob(monkeypatch):
    monkeypatch.setattr("src.memory.get_latest_settings", lambda: {"values": "nonsense"},
                        raising=False)
    assert settings.load_overrides() == {}


# ── The brain actually sees an override ───────────────────────────────────────

def test_brain_params_resolve_live_not_at_import(monkeypatch):
    """The whole point of the lazy params surface: an override applied after the brain
    modules were imported must still reach the decision code."""
    monkeypatch.setattr(config.brain, "f_global", 0.10)
    assert params.F_GLOBAL == pytest.approx(0.10)
    settings.apply_overrides({"brain.f_global": 0.20})
    assert params.F_GLOBAL == pytest.approx(0.20)


def test_brain_params_computed_dicts_resolve_live(monkeypatch):
    monkeypatch.setattr(config.brain, "conviction_very_high", 1.0)
    assert params.CONVICTION_THEME_WEIGHTS["very_high"] == pytest.approx(1.0)
    settings.apply_overrides({"brain.conviction_very_high": 0.85})
    assert params.CONVICTION_THEME_WEIGHTS["very_high"] == pytest.approx(0.85)


def test_brain_params_cross_section_aliases(monkeypatch):
    monkeypatch.setattr(config.rules, "max_position_pct", 0.10)
    assert params.SIZE_MAX_PCT == pytest.approx(0.10)
    monkeypatch.setattr(config.rules, "max_position_pct", 0.08)
    assert params.SIZE_MAX_PCT == pytest.approx(0.08)


def test_unknown_brain_param_raises():
    with pytest.raises(AttributeError):
        _ = params.DEFINITELY_NOT_A_PARAM


def test_overriding_a_tunable_changes_the_brain_version_hash(monkeypatch):
    """Attribution comes free — a dial change must re-key the config hash so decisions
    logged before and after can be told apart."""
    from src.brain import version
    monkeypatch.setattr(config.brain, "cp_buy_threshold", 0.60)
    before = version.brain_config_hash()
    settings.apply_overrides({"brain.cp_buy_threshold": 0.65})
    assert version.brain_config_hash() != before


# ── Introspection ─────────────────────────────────────────────────────────────

def test_effective_attributes_each_value_to_its_source(monkeypatch):
    monkeypatch.setattr(config.brain, "f_global", 0.10)
    monkeypatch.setitem(settings._BASELINE, "brain.f_global",
                        settings.code_default("brain.f_global"))
    rows = {r["key"]: r for r in settings.effective(overrides={})}
    assert rows["brain.f_global"]["source"] == "default"

    rows = {r["key"]: r for r in settings.effective(overrides={"brain.f_global": 0.2})}
    assert rows["brain.f_global"]["source"] == "dashboard"


def test_effective_flags_an_environment_supplied_value(monkeypatch):
    monkeypatch.setitem(settings._BASELINE, "brain.f_global", 0.123)
    rows = {r["key"]: r for r in settings.effective(overrides={})}
    assert rows["brain.f_global"]["source"] == "environment"


def test_export_schema_is_json_serialisable_and_grouped():
    import json
    schema = settings.export_schema()
    json.dumps(schema)   # must not raise
    assert schema["groups"]
    dials = [s for s in schema["settings"] if s["tier"] == DIAL]
    assert dials, "expected at least one dial in the exported schema"
    assert all(s["group"] in schema["groups"] for s in schema["settings"] if s["tier"] != PLUMBING)


def test_committed_dashboard_schema_is_up_to_date():
    """dashboard/settings_schema.json is generated and committed so GitHub Pages can serve it
    statically. Regenerate with:  python -m src.settings --export dashboard/settings_schema.json
    """
    import json
    from pathlib import Path

    path = Path(__file__).parent.parent / "dashboard" / "settings_schema.json"
    assert path.exists(), f"{path} is missing — run the export command in this test's docstring"
    committed = json.loads(path.read_text())
    assert committed == json.loads(json.dumps(settings.export_schema())), (
        "dashboard/settings_schema.json is stale — regenerate it with "
        "`python -m src.settings --export dashboard/settings_schema.json`"
    )


def test_config_sections_map_covers_every_singleton():
    """settings.SECTIONS must list every config dataclass singleton, or fields go unseen."""
    singletons = {
        name for name, value in vars(config).items()
        if dataclasses.is_dataclass(value) and not isinstance(value, type)
    }
    assert singletons == set(settings.SECTIONS)


# ── Persistence: the two writers share one row and must not clobber each other ──

def _fake_settings_table(existing_content):
    """A minimal Supabase client stub capturing what gets written to `settings`."""
    from unittest.mock import MagicMock
    captured = {}
    table = MagicMock()
    table.select.return_value.limit.return_value.execute.return_value.data = (
        [{"id": 1, "content": existing_content}] if existing_content is not None else []
    )

    def _update(payload):
        captured["content"] = payload["content"]
        return MagicMock()
    table.update.side_effect = _update
    table.insert.side_effect = lambda payload: captured.setdefault("content", payload["content"]) and MagicMock()

    client = MagicMock()
    client.table.return_value = table
    return client, captured


def test_saving_overrides_preserves_the_effective_snapshot(monkeypatch):
    """The run writes `effective` and the dashboard writes `values` into the same row.
    A blind overwrite from either side would wipe the other's key."""
    from src import memory
    client, captured = _fake_settings_table({"values": {"brain.f_global": 0.2},
                                             "effective": {"brain.f_global": 0.2},
                                             "effective_at": "2026-07-28T00:00:00"})
    monkeypatch.setattr(memory, "get_client", lambda: client)
    monkeypatch.setattr(memory, "save_settings_history", lambda _c: None)

    memory.save_settings_snapshot({"values": {}})

    assert captured["content"]["values"] == {}
    assert captured["content"]["effective"] == {"brain.f_global": 0.2}
    assert captured["content"]["effective_at"] == "2026-07-28T00:00:00"


def test_recording_effective_preserves_the_override_map(monkeypatch):
    from src import memory
    client, captured = _fake_settings_table({"values": {"brain.f_global": 0.2}})
    monkeypatch.setattr(memory, "get_client", lambda: client)

    memory.save_settings_effective({"brain.f_global": 0.2})

    assert captured["content"]["values"] == {"brain.f_global": 0.2}
    assert captured["content"]["effective"] == {"brain.f_global": 0.2}
    assert captured["content"]["effective_at"]
