"""
Unit tests for src/contextual.py — the LLM contextual reasoning layer. The LLM call is mocked
(no live API): we test the proposal validation (the shape gate) and the no-key / parse paths.
The numeric clamping + hard invariants live in playbook.apply_proposals (tested separately).
"""

from src import config, contextual, mcp_loop


def _plays():
    return [{"name": "macro_dip", "enabled": True, "type": "hybrid", "weight": 1.0,
             "rationale": "", "evaluates": "..."}]


def _signals():
    return [{"ticker": "NVDA", "action": "watch", "theme": "core_tech"},
            {"ticker": "MSFT", "action": "buy", "theme": "core_tech"}]


class TestValidate:
    def test_accepts_a_well_formed_proposal(self, convictions):
        payload = {"proposals": [
            {"ticker": "NVDA", "play": "macro_dip", "kind": "size_multiplier", "value": 1.4,
             "rationale": "macro-driven dip"},
        ]}
        out = contextual._validate(payload, _signals(), _plays(), convictions)
        assert len(out) == 1
        assert out[0]["source"] == "llm" and out[0]["ticker"] == "NVDA" and out[0]["value"] == 1.4

    def test_rejects_unknown_ticker_play_and_kind(self, convictions):
        payload = {"proposals": [
            {"ticker": "ZZZZ", "play": "macro_dip", "kind": "size_multiplier", "value": 1.4, "rationale": ""},
            {"ticker": "NVDA", "play": "not_a_play", "kind": "size_multiplier", "value": 1.4, "rationale": ""},
            {"ticker": "NVDA", "play": "macro_dip", "kind": "delete_position", "value": 1.4, "rationale": ""},
            {"ticker": "NVDA", "play": "macro_dip", "kind": "size_multiplier", "value": "big", "rationale": ""},
        ]}
        assert contextual._validate(payload, _signals(), _plays(), convictions) == []

    def test_rejects_excluded_ticker(self, convictions):
        # TSLA is excluded in the fixture — never let a proposal target it, even if well-formed.
        signals = _signals() + [{"ticker": "TSLA", "action": "watch", "theme": None}]
        payload = {"proposals": [
            {"ticker": "TSLA", "play": "macro_dip", "kind": "size_multiplier", "value": 1.4, "rationale": ""},
        ]}
        assert contextual._validate(payload, signals, _plays(), convictions) == []

    def test_empty_payload_is_empty(self, convictions):
        assert contextual._validate({}, _signals(), _plays(), convictions) == []
        assert contextual._validate({"proposals": []}, _signals(), _plays(), convictions) == []


class TestReason:
    def test_no_api_key_skips_llm(self, monkeypatch, convictions):
        monkeypatch.setattr(config.services, "anthropic_api_key", "")
        out = contextual.reason(_signals(), {}, {"selloff": {}}, convictions, _plays(), [])
        assert out == []

    def test_parses_and_validates_a_mocked_emit(self, monkeypatch, convictions):
        monkeypatch.setattr(config.services, "anthropic_api_key", "test-key")
        payload = {"proposals": [
            {"ticker": "MSFT", "play": "macro_dip", "kind": "conviction_nudge", "value": 0.05, "rationale": "x"},
            {"ticker": "ZZZZ", "play": "macro_dip", "kind": "size_multiplier", "value": 2.0, "rationale": "y"},
        ]}
        monkeypatch.setattr(mcp_loop, "run_structured_sync", lambda *a, **k: (payload, None, []))
        market_data = {"NVDA": {"current_price": 100.0}, "MSFT": {"current_price": 100.0}}
        out = contextual.reason(_signals(), market_data, {"selloff": {}, "spy_returns": None},
                                convictions, _plays(), [])
        assert len(out) == 1                       # the unknown-ticker proposal is dropped
        assert out[0]["ticker"] == "MSFT" and out[0]["source"] == "llm"
