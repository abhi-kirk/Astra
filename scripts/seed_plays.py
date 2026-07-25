"""
Seed the Playbook (the `plays` table) with the initial strategy plays.

Run once after applying migration 018:  python -m scripts.seed_plays
Re-running upserts the single `plays` row and appends a `plays_history` snapshot (idempotent
in effect — it overwrites with the same content). Requires the Supabase SERVICE key (writes),
so this is run by a human/CI, not by the read-only Supabase MCP.

The Playbook mirrors convictions: strategies as editable data. Add/remove/reweight a play by
editing PLAYS below (or, later, the dashboard drawer) and re-running. `macro_dip` is row one —
"buy quality at a beta-driven discount" (docs/contextual_reasoning.md).
"""

from __future__ import annotations

from src import memory

PLAYS = {
    "plays": [
        {
            "name": "macro_dip",
            "enabled": True,
            "type": "hybrid",   # quant arm arms it (beta + selloff), the LLM confirms/vetoes
            "weight": 1.0,
            "rationale": (
                "Buy quality at a beta-driven discount — when a healthy conviction name is down "
                "mainly because the whole market is down (not its own thesis), size up into the dip."
            ),
            "evaluates": (
                "The name's recent drop is mostly market-beta (low idiosyncratic residual) AND the "
                "broad market is in a genuine sentiment-driven selloff (not a company-specific "
                "problem), AND this company's thesis is intact — so the discount is market noise, "
                "not a broken business. Confirm the selloff is macro (rates/geopolitics/risk-off), "
                "and veto if the weakness is actually about this company."
            ),
            "params": {
                "beta_min": 0.8,
                "residual_max_pct": 5.0,
                "min_drop_pct": 8.0,
                "size_mult_max": 1.5,
                "nudge_max": 0.08,
            },
        }
    ]
}


def main() -> None:
    memory.save_plays_snapshot(PLAYS)
    names = [p["name"] for p in PLAYS["plays"]]
    print(f"Seeded plays: {names}")


if __name__ == "__main__":
    main()
