"""
Client-side Grok tool loop.

We hold the MCP connections, expose their tools to Grok as ordinary function schemas,
and execute every ``tool_use`` in-process with:

  - a per-tool timeout (`tool_timeout`): a hung tool dies fast; the model gets an error
    result and still finishes the note;
  - a circuit breaker (`failure_limit`): a repeatedly-failing server is disabled for the run;
  - full logging + a returned `tool_log` for visibility.

The wire format lives in src/grok.py. This loop still speaks content blocks and
``stop_reason`` so a provider swap stays in that one module.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

from mcp import ClientSession
from src import config
from src.grok import GrokClient
from src.mcp import ServerSpec, extract_text

logger = logging.getLogger(__name__)

# Cap a single tool result so a chatty server (e.g. AV NEWS_SENTIMENT) can't blow the context.
_MAX_TOOL_RESULT_CHARS = 8000

AgenticResult = tuple[str, Any, list[dict[str, Any]]]


@dataclass
class _Usage:
    """Minimal usage accumulator (obs.record_advisor reads input_tokens/output_tokens)."""
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, usage: Any) -> None:
        if usage is None:
            return
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0


def run_agentic_sync(prompt: str, specs: list[ServerSpec], **kwargs: Any) -> AgenticResult:
    """Synchronous entry point — runs the async loop in a fresh event loop.

    Wrap this in src.timeout.run_with_timeout for a wall-clock backstop.
    """
    return asyncio.run(run_agentic(prompt, specs, **kwargs))


async def run_agentic(
    prompt: str,
    specs: list[ServerSpec],
    *,
    model: str,
    max_tokens: int,
    effort: str = "high",
    tool_timeout: int = 45,
    max_rounds: int = 8,
    failure_limit: int = 2,
) -> AgenticResult:
    """Run the manual tool loop and return (final_text, usage, tool_log)."""
    usage = _Usage()
    tool_log: list[dict[str, Any]] = []

    async with GrokClient(api_key=config.services.grok_api_key) as client, AsyncExitStack() as stack:
        tool_to_session: dict[str, ClientSession] = {}
        tool_to_server: dict[str, str] = {}
        tools: list[Any] = []

        # -- connect servers + collect tool schemas -------------------------
        for spec in specs:
            session = await _connect(stack, spec, tool_timeout)
            if session is None:
                continue
            try:
                listed = await asyncio.wait_for(session.list_tools(), timeout=tool_timeout)
            except Exception:
                logger.warning("MCP %s: list_tools failed — skipping server", spec.name, exc_info=True)
                continue
            allowed = set(spec.allowed_tools)
            added = 0
            for t in listed.tools:
                if allowed and t.name not in allowed:
                    continue
                tools.append({
                    "name": t.name,
                    "description": t.description or "",
                    "input_schema": t.inputSchema,
                })
                tool_to_session[t.name] = session
                tool_to_server[t.name] = spec.name
                added += 1
            logger.info("MCP %s: %d tool(s) available", spec.name, added)

        # -- manual agentic loop --------------------------------------------
        failures: dict[str, int] = {}
        messages: list[Any] = [{"role": "user", "content": prompt}]
        final: Any = None
        rounds = 0

        for round_no in range(1, max_rounds + 1):
            rounds = round_no
            final = await _create(client, model, max_tokens, messages, tools, effort)
            usage.add(getattr(final, "usage", None))
            if getattr(final, "stop_reason", None) != "tool_use":
                break

            # Echo the full assistant turn back (thinking + tool_use blocks preserved).
            messages.append({"role": "assistant", "content": final.content})
            results: list[dict[str, Any]] = []
            for block in list(final.content):
                if getattr(block, "type", None) != "tool_use":
                    continue
                results.append(
                    await _dispatch_tool(
                        block, tool_to_session, tool_to_server, failures,
                        failure_limit, tool_timeout, round_no, tool_log,
                    )
                )
            messages.append({"role": "user", "content": results})
        else:
            # Ran out of rounds while still calling tools — force a final tool-free note.
            if final is not None and getattr(final, "stop_reason", None) == "tool_use":
                logger.warning("Advisor loop hit max_rounds=%d — forcing a final tool-free note", max_rounds)
                final = await _create(client, model, max_tokens, messages, [], effort)
                usage.add(getattr(final, "usage", None))

        text = extract_text(final) if final is not None else ""
        logger.info(
            "Advisor loop: %d round(s), %d tool call(s), tokens in=%d out=%d",
            rounds, len(tool_log), usage.input_tokens, usage.output_tokens,
        )
        return (text, usage, tool_log)


async def _connect(stack: AsyncExitStack, spec: ServerSpec, timeout: int) -> ClientSession | None:
    """Open an MCP session for `spec`, trying Streamable HTTP then SSE.

    Each attempt uses its own stack; only a successful attempt is handed to the caller's
    stack for cleanup, so a failed transport never leaves a half-open context behind.
    """
    for opener, transport_name in ((streamablehttp_client, "streamable-http"), (sse_client, "sse")):
        attempt = AsyncExitStack()
        try:
            transport = await attempt.enter_async_context(opener(spec.url))
            read, write = transport[0], transport[1]
            session = await attempt.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), timeout=timeout)
        except Exception:
            await attempt.aclose()
            logger.warning("MCP %s: %s connect failed", spec.name, transport_name, exc_info=True)
            continue
        await stack.enter_async_context(attempt.pop_all())
        logger.info("MCP %s: connected via %s", spec.name, transport_name)
        return session
    logger.error("MCP %s: all transports failed — server unavailable this run", spec.name)
    return None


async def _dispatch_tool(
    block: Any,
    tool_to_session: dict[str, ClientSession],
    tool_to_server: dict[str, str],
    failures: dict[str, int],
    failure_limit: int,
    tool_timeout: int,
    round_no: int,
    tool_log: list[dict[str, Any]],
) -> dict[str, Any]:
    """Execute one tool_use block; return the tool_result to feed back to the model."""
    name = getattr(block, "name", "")
    block_id = getattr(block, "id", "")
    server = tool_to_server.get(name, "?")
    entry: dict[str, Any] = {"server": server, "tool": name, "round": round_no}
    session = tool_to_session.get(name)

    if session is None:
        entry["status"] = "unknown_tool"
        tool_log.append(entry)
        logger.warning("Advisor: model called unknown tool %r — returning error", name)
        return _err_result(block_id, f"Unknown tool '{name}'")

    if failures.get(server, 0) >= failure_limit:
        entry.update(status="circuit_open", latency_ms=0)
        tool_log.append(entry)
        logger.warning("MCP %s: circuit open — %s not called", server, name)
        return _err_result(block_id, f"{server} temporarily disabled after repeated failures")

    args = dict(getattr(block, "input", {}) or {})
    t0 = time.monotonic()
    try:
        call_res = await asyncio.wait_for(
            session.call_tool(name, args, read_timeout_seconds=timedelta(seconds=tool_timeout)),
            timeout=tool_timeout,
        )
        text, is_err = _result_text(call_res)
        latency = int((time.monotonic() - t0) * 1000)
        entry.update(status=("tool_error" if is_err else "ok"), latency_ms=latency, chars=len(text))
        result = {"type": "tool_result", "tool_use_id": block_id, "content": text[:_MAX_TOOL_RESULT_CHARS], "is_error": is_err}
    except asyncio.TimeoutError:
        latency = int((time.monotonic() - t0) * 1000)
        failures[server] = failures.get(server, 0) + 1
        entry.update(status="timeout", latency_ms=latency)
        logger.warning("MCP %s: tool %s TIMED OUT after %ds (%dms)", server, name, tool_timeout, latency)
        result = _err_result(block_id, f"Tool '{name}' timed out after {tool_timeout}s")
    except Exception as exc:
        latency = int((time.monotonic() - t0) * 1000)
        failures[server] = failures.get(server, 0) + 1
        entry.update(status="error", latency_ms=latency, error=str(exc)[:200])
        logger.warning("MCP %s: tool %s failed: %s", server, name, exc)
        result = _err_result(block_id, f"Tool '{name}' error: {exc}")

    tool_log.append(entry)
    logger.info("MCP tool  %-14s %-22s %-11s %6dms", server, name, entry["status"], entry.get("latency_ms", 0))
    return result


async def _create(client: Any, model: str, max_tokens: int, messages: list[Any], tools: list[Any],
                  effort: str, tool_choice: dict[str, Any] | None = None, thinking: bool = True) -> Any:
    """One model turn. Effort rides in extra_body; src/grok.py maps it onto reasoning.effort.

    `tool_choice` forces a specific tool (used for the structured emit). Grok reasoning
    stays on for that call too: it cannot be disabled, and a forced function still reasons."""
    extra: dict[str, Any] = {"output_config": {"effort": effort}}
    if thinking and tool_choice is None:
        extra["thinking"] = {"type": "adaptive"}
    kwargs: dict[str, Any] = dict(model=model, max_tokens=max_tokens, messages=messages, extra_body=extra)
    if tools:
        kwargs["tools"] = tools
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice
    return await client.messages.create(**kwargs)


StructuredResult = tuple[dict[str, Any], Any, list[dict[str, Any]]]


def run_structured_sync(prompt: str, specs: list[ServerSpec], emit_tool: dict[str, Any],
                        **kwargs: Any) -> StructuredResult:
    """Synchronous entry point for the structured tool loop (wrap in run_with_timeout)."""
    return asyncio.run(run_structured(prompt, specs, emit_tool, **kwargs))


async def run_structured(
    prompt: str,
    specs: list[ServerSpec],
    emit_tool: dict[str, Any],
    *,
    model: str,
    max_tokens: int,
    effort: str = "high",
    tool_timeout: int = 45,
    max_rounds: int = 6,
    failure_limit: int = 2,
) -> StructuredResult:
    """Run the agentic gather/reason loop, then FORCE a final structured emit via `emit_tool`
    (`tool_choice` → that tool), returning (emitted_input_dict, usage, tool_log).

    `emit_tool` is a tool schema {name, description, input_schema}; its input_schema IS the output
    contract, so the boundary object is schema-valid by construction. Returns {} if nothing was
    emitted (the caller treats that as "no proposals")."""
    usage = _Usage()
    tool_log: list[dict[str, Any]] = []

    async with GrokClient(api_key=config.services.grok_api_key) as client, AsyncExitStack() as stack:
        tool_to_session: dict[str, ClientSession] = {}
        tool_to_server: dict[str, str] = {}
        tools: list[Any] = []

        for spec in specs:
            session = await _connect(stack, spec, tool_timeout)
            if session is None:
                continue
            try:
                listed = await asyncio.wait_for(session.list_tools(), timeout=tool_timeout)
            except Exception:
                logger.warning("MCP %s: list_tools failed — skipping server", spec.name, exc_info=True)
                continue
            allowed = set(spec.allowed_tools)
            for t in listed.tools:
                if allowed and t.name not in allowed:
                    continue
                tools.append({"name": t.name, "description": t.description or "", "input_schema": t.inputSchema})
                tool_to_session[t.name] = session
                tool_to_server[t.name] = spec.name

        # Expose the emit tool DURING the gather loop (alongside the MCP tools) so the model can
        # emit the moment it's done reasoning, rather than being told "unknown tool" if it reaches
        # for the emit tool mid-loop. A forced emit is only the fallback for when the model stops
        # (or runs out of rounds) without emitting on its own.
        gather_tools = tools + [emit_tool]
        failures: dict[str, int] = {}
        messages: list[Any] = [{"role": "user", "content": prompt}]
        final: Any = None
        payload: dict[str, Any] = {}
        emitted_early = False

        def _emit_block(msg: Any) -> Any:
            return next((b for b in list(getattr(msg, "content", []) or [])
                         if getattr(b, "type", None) == "tool_use"
                         and getattr(b, "name", None) == emit_tool["name"]), None)

        for round_no in range(1, max_rounds + 1):
            final = await _create(client, model, max_tokens, messages, gather_tools, effort)
            usage.add(getattr(final, "usage", None))
            emit = _emit_block(final)
            if emit is not None:                                   # model emitted on its own — done
                payload = dict(getattr(emit, "input", {}) or {})
                emitted_early = True
                break
            if getattr(final, "stop_reason", None) != "tool_use":  # stopped with text, no emit
                messages.append({"role": "assistant", "content": final.content})
                break
            messages.append({"role": "assistant", "content": final.content})
            results: list[dict[str, Any]] = []
            for block in list(final.content):
                if getattr(block, "type", None) != "tool_use":
                    continue
                results.append(await _dispatch_tool(
                    block, tool_to_session, tool_to_server, failures,
                    failure_limit, tool_timeout, round_no, tool_log,
                ))
            messages.append({"role": "user", "content": results})

        # -- forced structured emit (fallback) -------------------------------
        if not emitted_early:
            if messages[-1]["role"] == "assistant":  # keep roles alternating before the forced turn
                name = emit_tool["name"]
                messages.append({"role": "user", "content":
                                 f"Now call {name} with your final result. An empty list is valid."})
            emitted = await _create(
                client, model, max_tokens, messages, [emit_tool], effort,
                tool_choice={"type": "tool", "name": emit_tool["name"]},
            )
            usage.add(getattr(emitted, "usage", None))
            emit = _emit_block(emitted)
            if emit is not None:
                payload = dict(getattr(emit, "input", {}) or {})

        logger.info("Structured loop: emitted_early=%s, %d tool call(s), tokens in=%d out=%d",
                    emitted_early, len(tool_log), usage.input_tokens, usage.output_tokens)
        return (payload, usage, tool_log)


def _first_attr(obj: Any, *names: str) -> Any:
    """First present attribute among `names`, else None.

    Unlike the broker's `_field`, this tolerates an unknown shape rather than raising: the
    advisor path only narrates, so a mangled tool result should cost us a note, not the run.
    Logs loudly so an SDK rename is still visible."""
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    logger.warning("Unrecognized MCP result shape: none of %s on %s", names, type(obj).__name__)
    return None


def _result_text(call_res: Any) -> tuple[str, bool]:
    """Flatten an MCP CallToolResult to (text, is_error).

    Both field spellings are accepted (mcp v1 camelCase, v2 snake_case) so an SDK bump can't
    quietly turn every failed tool call into a successful-looking empty result."""
    parts = [getattr(item, "text", "") for item in (getattr(call_res, "content", None) or [])]
    out = "\n".join(p for p in parts if p).strip()
    if not out:
        sc = _first_attr(call_res, "structured_content", "structuredContent")
        out = str(sc) if sc else "(no content)"
    return out, bool(_first_attr(call_res, "is_error", "isError"))


def _err_result(block_id: str, message: str) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": block_id, "content": message, "is_error": True}
