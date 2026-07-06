"""
Unit tests for src/mcp_loop.py — the client-side Claude tool loop.

Fakes the Anthropic client (scripted responses) and the MCP session (in-process tools),
so no network is touched. The headline guard is `test_tool_timeout_still_produces_note`:
a hung tool must NOT stall the run to an empty note — the regression we're fixing.
"""

import asyncio
from types import SimpleNamespace

from anthropic.types import TextBlock, ToolUseBlock

from src import mcp_loop
from src.mcp import ServerSpec

SPEC = [ServerSpec("tavily", "https://tavily", ["tavily-search"])]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _text_resp(text: str, stop: str = "end_turn"):
    return SimpleNamespace(
        content=[TextBlock(type="text", text=text, citations=None)],
        stop_reason=stop,
        usage=SimpleNamespace(input_tokens=5, output_tokens=7),
    )


def _tool_resp(tool_name: str, tool_input=None, block_id: str = "tu1"):
    return SimpleNamespace(
        content=[ToolUseBlock(type="tool_use", id=block_id, name=tool_name, input=tool_input or {})],
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=5, output_tokens=7),
    )


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._responses:
            return self._responses.pop(0)
        return _text_resp("## ASTRA Note\nFinal fallback.")  # terminal when script is exhausted


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _tool(name: str):
    return SimpleNamespace(name=name, description="d", inputSchema={"type": "object", "properties": {}})


class _FakeSession:
    def __init__(self, tools, impl):
        self._tools = tools
        self._impl = impl
        self.call_count = 0

    async def list_tools(self):
        return SimpleNamespace(tools=self._tools)

    async def call_tool(self, name, args, read_timeout_seconds=None):
        self.call_count += 1
        return await self._impl(name, args)


def _ok_impl(text: str = "results"):
    async def impl(name, args):
        return SimpleNamespace(content=[SimpleNamespace(text=text)], structuredContent=None, isError=False)
    return impl


def _patch(monkeypatch, client, session):
    monkeypatch.setattr("src.mcp_loop.anthropic.AsyncAnthropic", lambda **kw: client)

    async def fake_connect(stack, spec, timeout):
        return session

    monkeypatch.setattr("src.mcp_loop._connect", fake_connect)


def _run(**overrides):
    kwargs = dict(model="m", max_tokens=100, tool_timeout=5, max_rounds=4, failure_limit=2)
    kwargs.update(overrides)
    return mcp_loop.run_agentic_sync("prompt", SPEC, **kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_happy_path(monkeypatch):
    client = _FakeClient([_tool_resp("tavily-search", {"query": "RKLB"}), _text_resp("## ASTRA Note\nBody here.")])
    session = _FakeSession([_tool("tavily-search")], _ok_impl("news!"))
    _patch(monkeypatch, client, session)

    text, usage, tool_log = _run()

    assert "Body here." in text
    assert len(tool_log) == 1 and tool_log[0]["status"] == "ok"
    assert usage.input_tokens == 10  # 2 model turns × 5
    assert session.call_count == 1


def test_tool_timeout_still_produces_note(monkeypatch):
    async def slow_impl(name, args):
        await asyncio.sleep(3)
        return SimpleNamespace(content=[SimpleNamespace(text="late")], structuredContent=None, isError=False)

    client = _FakeClient([_tool_resp("tavily-search"), _text_resp("## ASTRA Note\nMechanical fallback.")])
    session = _FakeSession([_tool("tavily-search")], slow_impl)
    _patch(monkeypatch, client, session)

    text, _usage, tool_log = _run(tool_timeout=1)

    assert "Mechanical fallback." in text          # note produced despite the hang
    assert tool_log[0]["status"] == "timeout"


def test_circuit_breaker(monkeypatch):
    async def boom(name, args):
        raise RuntimeError("boom")

    client = _FakeClient([
        _tool_resp("tavily-search", block_id="a"),
        _tool_resp("tavily-search", block_id="b"),
        _text_resp("## ASTRA Note\nDone."),
    ])
    session = _FakeSession([_tool("tavily-search")], boom)
    _patch(monkeypatch, client, session)

    text, _usage, tool_log = _run(max_rounds=5, failure_limit=1)

    assert [e["status"] for e in tool_log] == ["error", "circuit_open"]
    assert session.call_count == 1  # second call short-circuited, no network hit
    assert "Done." in text


def test_max_rounds_forces_final_note(monkeypatch):
    client = _FakeClient([_tool_resp("tavily-search", block_id="a"), _tool_resp("tavily-search", block_id="b")])
    session = _FakeSession([_tool("tavily-search")], _ok_impl())
    _patch(monkeypatch, client, session)

    text, _usage, tool_log = _run(max_rounds=2, failure_limit=5)

    assert "Final fallback." in text
    assert len(tool_log) == 2
    assert client.messages.calls[-1].get("tools") is None  # forced final call is tool-free


def test_no_servers_still_produces_note(monkeypatch):
    client = _FakeClient([_text_resp("## ASTRA Note\nMechanical only.")])
    _patch(monkeypatch, client, None)  # _connect returns None → no tools

    text, _usage, tool_log = _run()

    assert "Mechanical only." in text
    assert tool_log == []
    assert client.messages.calls[0].get("tools") is None
