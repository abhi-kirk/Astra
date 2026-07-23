"""
Unit tests for src/brain/version.py — the shared brain-version stamping used by both the
paper and Autotrader writers to tag every logged decision/trade with the exact brain that
produced it (migration 017; see docs/attribution.md).
"""

from src import config
from src.brain import version


class TestConfigHash:
    def test_hash_is_stable_and_short(self):
        h1 = version.brain_config_hash()
        h2 = version.brain_config_hash()
        assert h1 == h2
        assert len(h1) == 12

    def test_hash_changes_when_a_tunable_changes(self, monkeypatch):
        before = version.brain_config_hash()
        monkeypatch.setattr(config.brain, "cp_buy_threshold", config.brain.cp_buy_threshold + 0.05)
        assert version.brain_config_hash() != before

    def test_hash_changes_when_the_gate_flag_flips(self, monkeypatch):
        before = version.brain_config_hash()
        monkeypatch.setattr(config.brain, "conviction_primary", not config.brain.conviction_primary)
        assert version.brain_config_hash() != before

    def test_snapshot_covers_every_brain_field(self):
        snap = version.brain_config_snapshot()
        assert "cp_buy_threshold" in snap
        assert "conviction_primary" in snap
        assert "f_global" in snap


class TestCodeVersion:
    def test_prefers_github_sha(self, monkeypatch):
        version.brain_code_version.cache_clear()
        monkeypatch.setenv("GITHUB_SHA", "abcdef0123456789")
        assert version.brain_code_version() == "abcdef012345"  # truncated to 12
        version.brain_code_version.cache_clear()

    def test_never_empty(self, monkeypatch):
        version.brain_code_version.cache_clear()
        monkeypatch.delenv("GITHUB_SHA", raising=False)
        v = version.brain_code_version()
        assert v and isinstance(v, str)  # a real SHA in-repo, else 'unknown'
        version.brain_code_version.cache_clear()


class TestVersionFields:
    def test_fields_shape(self):
        f = version.brain_version_fields()
        assert set(f) == {"brain_code_version", "brain_config_hash"}
        assert f["brain_config_hash"] == version.brain_config_hash()

    def test_registry_row_shape(self):
        row = version.brain_version_registry_row()
        assert set(row) == {"config_hash", "code_version", "config"}
        assert row["config_hash"] == version.brain_config_hash()
        assert isinstance(row["config"], dict)
