"""
Brain version stamping — one source of truth for tagging every logged decision/trade
with the exact brain that produced it, so future attribution can segment cleanly by
version (see docs/attribution.md). Shared by BOTH tracks: the paper writers and the
Autotrader writers stamp through the same helpers here.

Two identifiers travel with every row:
    brain_code_version — the git SHA (which code)
    brain_config_hash  — a hash of the effective BRAIN_* tunables + the conviction_primary
                         flag (which knobs — almost every one is env-overridable at runtime,
                         so the SHA alone cannot tell two runs apart)

The full config behind a hash is recorded once in the `brain_versions` registry table
(keyed on the hash), so the per-row tag stays a compact pair and nothing is duplicated.

Only brain_code_version is cached — a git subprocess is worth memoizing, while the config
hash is recomputed each call (trivially cheap) so tests that monkeypatch a tunable in place
see the change reflected immediately.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
from functools import cache

from src import config

# Repo root — two levels up from src/brain/version.py — for the `git rev-parse` fallback.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def brain_config_snapshot() -> dict:
    """The effective BRAIN_* tunables as a flat dict (every field of config.brain)."""
    return dataclasses.asdict(config.brain)


def brain_config_hash() -> str:
    """Short deterministic hash of the brain config — changes iff a tunable/flag changes."""
    blob = json.dumps(brain_config_snapshot(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


@cache
def brain_code_version() -> str:
    """Git SHA of the running code: GITHUB_SHA in CI, else `git rev-parse`, else 'unknown'."""
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        return sha[:12]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, timeout=5, cwd=_REPO_ROOT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else "unknown"


def brain_version_fields() -> dict[str, str]:
    """The two columns stamped onto every logged decision/trade row (both tracks)."""
    return {
        "brain_code_version": brain_code_version(),
        "brain_config_hash": brain_config_hash(),
    }


def brain_version_registry_row() -> dict:
    """One row for the `brain_versions` registry, keyed on config_hash (upsert-idempotent)."""
    return {
        "config_hash": brain_config_hash(),
        "code_version": brain_code_version(),
        "config": brain_config_snapshot(),
    }
