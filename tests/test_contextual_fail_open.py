"""
Fail-open tests for the contextual layer: the deterministic quant spine must always survive an
LLM failure. When the LLM reasoning errors, hybrid plays fall back to their quant base; when it
vetoes (returns []), the hybrid base is dropped — but the run never crashes.
"""

import math

import src.contextual as contextual_mod
from src.brain import playbook


def _noise(n):
    return [0.01 * math.sin(i) for i in range(n)]


SPY = _noise(50) + [-0.02] * 10
PANIC_CTX = {"spy_returns": SPY,
             "selloff": {"spy_nday_return_pct": -9.6, "breadth_pct_down": 100.0, "is_panic": True}}
MACRO_PLAY = {"name": "macro_dip", "enabled": True, "type": "hybrid", "weight": 1.0,
              "params": {"beta_min": 0.8, "residual_max_pct": 5.0, "min_drop_pct": 8.0,
                         "size_mult_max": 1.5, "nudge_max": 0.08}}


def _setup():
    signals = [{"ticker": "NVDA", "action": "watch", "source": "position_screen",
                "theme": "core_tech", "score_buy": 0.5, "reasons": [], "risk_flags": []}]
    md = {"NVDA": {"current_price": 85.0, "returns_recent": [1.2 * r for r in SPY]}}
    return signals, md


def test_llm_failure_falls_back_to_the_quant_base(monkeypatch, convictions):
    # The hybrid quant arm fires (macro dip); the LLM raises → its base proposals still stand.
    monkeypatch.setattr(contextual_mod, "reason", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    signals, md = _setup()
    props = playbook.evaluate(signals, md, PANIC_CTX, convictions, use_ai=True, plays={"plays": [MACRO_PLAY]})
    assert props and all(p["source"] == "quant" for p in props)   # quant floor preserved


def test_llm_veto_drops_the_hybrid_base(monkeypatch, convictions):
    # A successful LLM run that emits nothing = veto; the hybrid's quant base is not applied.
    monkeypatch.setattr(contextual_mod, "reason", lambda *a, **k: [])
    signals, md = _setup()
    props = playbook.evaluate(signals, md, PANIC_CTX, convictions, use_ai=True, plays={"plays": [MACRO_PLAY]})
    assert props == []


def test_llm_proposals_are_merged(monkeypatch, convictions):
    # A successful LLM run replaces the hybrid base with its own (confirmed) proposals.
    llm = [{"ticker": "NVDA", "play": "macro_dip", "kind": "size_multiplier", "value": 1.3,
            "rationale": "confirmed macro", "evidence": {}, "source": "llm", "applied": None}]
    monkeypatch.setattr(contextual_mod, "reason", lambda *a, **k: llm)
    signals, md = _setup()
    props = playbook.evaluate(signals, md, PANIC_CTX, convictions, use_ai=True, plays={"plays": [MACRO_PLAY]})
    assert props == llm
