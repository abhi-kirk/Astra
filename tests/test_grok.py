"""Unit tests for src/grok.py. No network: the HTTP call is patched."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.grok import (
    GrokError,
    accumulate_stream,
    message_from_response,
    to_grok_body,
)


def _sse(response: dict, event: str = "response.completed") -> str:
    import json
    return f"data: {json.dumps({'type': event, 'response': response})}\n"


def test_text_response_maps_to_a_text_block():
    msg = message_from_response({
        "status": "completed",
        "output": [{
            "type": "message",
            "content": [{"type": "output_text", "text": "note", "annotations": [
                {"type": "url_citation", "url": "https://example.com", "title": "Ex"},
            ]}],
        }],
        "usage": {"input_tokens": 3, "output_tokens": 4},
    })
    assert msg.stop_reason == "end_turn"
    assert msg.content[0].text == "note"
    assert msg.content[0].citations == [{"url": "https://example.com", "title": "Ex"}]
    assert msg.usage.input_tokens == 3
    assert msg.usage.output_tokens == 4


def test_function_call_becomes_tool_use():
    msg = message_from_response({
        "status": "completed",
        "output": [{
            "type": "function_call",
            "call_id": "call_1",
            "name": "emit_proposals",
            "arguments": '{"proposals": []}',
        }],
    })
    assert msg.stop_reason == "tool_use"
    block = msg.content[0]
    assert block.type == "tool_use"
    assert block.id == "call_1"
    assert block.name == "emit_proposals"
    assert block.input == {"proposals": []}


def test_reasoning_item_is_kept_so_the_next_turn_can_echo_it():
    raw = {"type": "reasoning", "encrypted_content": "abc"}
    msg = message_from_response({"status": "completed", "output": [raw, {
        "type": "message", "content": [{"type": "output_text", "text": "ok"}],
    }]})
    assert msg.content[0].type == "reasoning"
    assert msg.content[0].raw == raw
    assert msg.stop_reason == "end_turn"


def test_incomplete_response_is_max_tokens():
    msg = message_from_response({
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "cut"}]}],
    })
    assert msg.stop_reason == "max_tokens"


def test_stream_error_is_retryable_only_for_overload():
    text = 'data: {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}\n'
    with pytest.raises(GrokError) as ei:
        accumulate_stream(text)
    assert ei.value.retryable

    text = 'data: {"type": "error", "error": {"type": "invalid_request_error", "message": "no"}}\n'
    with pytest.raises(GrokError) as ei:
        accumulate_stream(text)
    assert not ei.value.retryable


def test_stream_without_a_completed_event_has_no_stop_reason():
    msg = accumulate_stream("data: [DONE]\n")
    assert msg.stop_reason is None


def test_body_translates_tools_effort_and_forced_choice():
    body = to_grok_body(
        model="grok-4.7",
        messages=[
            {"role": "user", "content": "look"},
            {"role": "assistant", "content": [
                SimpleNamespace(type="reasoning", raw={"type": "reasoning", "encrypted_content": "x"}),
                SimpleNamespace(type="tool_use", id="c1", name="tavily_search", input={"q": "RKLB"}),
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": "news"},
            ]},
        ],
        max_tokens=100,
        tools=[{"name": "tavily_search", "description": "search", "input_schema": {"type": "object"}}],
        effort="max",
        tool_choice={"type": "tool", "name": "emit_proposals"},
        prompt_cache_key="run1",
    )
    assert body["model"] == "grok-4.7"
    assert body["stream"] is True
    assert body["store"] is False
    assert body["reasoning"] == {"effort": "xhigh"}
    assert body["tool_choice"] == {"type": "function", "name": "emit_proposals"}
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["parameters"] == {"type": "object"}
    assert body["prompt_cache_key"] == "run1"
    kinds = [item.get("type") or item.get("role") for item in body["input"]]
    assert kinds == ["user", "reasoning", "function_call", "function_call_output"]
    assert body["input"][2]["call_id"] == "c1"
    assert body["input"][3]["output"] == "news"


def test_create_returns_the_folded_message_and_does_not_retry_a_400():
    from src.grok import _call

    resp = MagicMock()
    resp.ok = False
    resp.status_code = 400
    resp.text = "bad"
    with patch("src.grok.requests.post", return_value=resp) as post:
        with pytest.raises(GrokError) as ei:
            _call({"model": "grok-4.7"}, "k")
    assert not ei.value.retryable
    assert post.call_count == 1


def test_create_retries_a_429_then_returns_text(monkeypatch):
    from src.grok import _call

    monkeypatch.setattr("src.grok.time.sleep", lambda _s: None)
    bad = MagicMock()
    bad.ok = False
    bad.status_code = 429
    bad.text = "slow down"
    good = MagicMock()
    good.ok = True
    good.content = _sse({
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "pong"}]}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }).encode()
    with patch("src.grok.requests.post", side_effect=[bad, good]) as post:
        msg = _call({"model": "grok-4.7"}, "k")
    assert post.call_count == 2
    assert msg.content[0].text == "pong"
    assert msg.stop_reason == "end_turn"
