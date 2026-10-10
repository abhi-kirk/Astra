"""
Grok (xAI) Responses client.

Callers still speak the message shape the tool loop already uses: content blocks,
``stop_reason``, ``tool_use`` / ``tool_result``. This module is the only place that
speaks ``POST /v1/responses``. The translation follows the shop's Worker client
(``siren-dashboard/src/llm.js``), which already runs this API key in production.

Streaming is required. A long reasoning generation on a non-streaming request sits
on one connection until the whole answer exists, and that is how the shop's nightly
call used to die. We fold the SSE body back into one message before returning, so
the tool loop never sees tokens arrive.

``store`` is false: xAI keeps Responses for 30 days by default, and these prompts
carry the portfolio.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from types import SimpleNamespace
from typing import Any

import requests

from src import config

logger = logging.getLogger(__name__)

# Gateway / overload statuses worth a second try. A 400 is a bad request retried.
_RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504, 520, 522, 524, 529})
_EFFORTS = {"low": "low", "medium": "medium", "high": "high", "max": "xhigh", "xhigh": "xhigh"}


class GrokError(RuntimeError):
    """A failed Grok call. ``retryable`` is set only for blips a second try might clear."""

    def __init__(self, message: str, *, retryable: bool = False, refusal: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.refusal = refusal


def message_from_response(res: dict[str, Any]) -> SimpleNamespace:
    """Map a completed Responses object onto the message shape the tool loop reads."""
    content: list[Any] = []
    refused = False
    for item in res.get("output") or []:
        if not item:
            continue
        kind = item.get("type")
        if kind == "refusal":
            refused = True
            continue
        if kind == "reasoning":
            # grok-4.7 returns encrypted reasoning on every turn. With store=false the
            # server will not rehydrate it, so the next request has to send the item back.
            content.append(SimpleNamespace(type="reasoning", raw=item))
            continue
        if kind == "function_call":
            try:
                parsed = json.loads(item["arguments"]) if item.get("arguments") else {}
            except (json.JSONDecodeError, TypeError):
                parsed = {}
            if not isinstance(parsed, dict):
                parsed = {}
            content.append(SimpleNamespace(
                type="tool_use",
                id=item.get("call_id") or item.get("id") or "",
                name=item.get("name") or "",
                input=parsed,
            ))
            continue
        if kind == "message":
            for part in item.get("content") or []:
                if not part:
                    continue
                if part.get("type") == "refusal":
                    refused = True
                    continue
                if part.get("type") in ("output_text", "text"):
                    citations = []
                    for ann in part.get("annotations") or []:
                        url = ann.get("url") if isinstance(ann, dict) else None
                        if isinstance(url, str):
                            title = ann.get("title") or url
                            citations.append({"url": url, "title": str(title)})
                    content.append(SimpleNamespace(
                        type="text",
                        text=part.get("text") or "",
                        citations=citations or None,
                    ))

    stop_reason = "end_turn"
    err_text = json.dumps(res.get("error") or "")
    if refused or "refus" in err_text.lower():
        stop_reason = "refusal"
    elif any(getattr(b, "type", None) == "tool_use" for b in content):
        stop_reason = "tool_use"
    elif res.get("status") == "incomplete" or "max_output_tokens" in (
        (res.get("incomplete_details") or {}).get("reason") or ""
    ):
        stop_reason = "max_tokens"
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=_usage(res.get("usage")))


def accumulate_stream(text: str) -> SimpleNamespace:
    """Fold a Responses SSE body into one message. Prefers the completed event."""
    completed: dict[str, Any] | None = None
    for line in text.split("\n"):
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            ev = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "error":
            err = ev.get("error") or {}
            kind = err.get("type") or err.get("code") or "unknown"
            exc = GrokError(f"Grok stream error ({kind}): {err.get('message') or 'unknown'}")
            exc.retryable = kind in ("overloaded_error", "api_error", "server_error")
            raise exc
        if ev.get("type") == "response.failed":
            res = ev.get("response") or {}
            err = res.get("error") or {}
            code = err.get("code") or err.get("type") or "failed"
            message = err.get("message") or "unknown"
            if code == "refusal" or "refus" in message.lower():
                completed = {"status": "completed", "output": [{"type": "refusal"}], "error": err}
                continue
            exc = GrokError(f"Grok stream error ({code}): {message}", retryable=True)
            raise exc
        if ev.get("type") in ("response.completed", "response.incomplete"):
            completed = ev.get("response") or ev
    if not completed:
        return SimpleNamespace(content=[], stop_reason=None, usage=_usage(None))
    return message_from_response(completed)


def to_grok_body(
    *,
    model: str,
    messages: list[Any],
    max_tokens: int,
    tools: list[Any] | None = None,
    effort: str | None = None,
    tool_choice: dict[str, Any] | None = None,
    prompt_cache_key: str | None = None,
) -> dict[str, Any]:
    """Translate one caller turn into a Responses request body."""
    items: list[Any] = []
    for message in messages:
        items.extend(_to_items(message))
    body: dict[str, Any] = {
        "model": model,
        "input": items,
        "stream": True,
        "store": False,
        "max_output_tokens": max_tokens,
    }
    if tools:
        body["tools"] = [_to_tool(t) for t in tools]
    mapped = _EFFORTS.get(effort or "", "")
    if mapped:
        body["reasoning"] = {"effort": mapped}
    choice = _to_tool_choice(tool_choice)
    if choice is not None:
        body["tool_choice"] = choice
    if prompt_cache_key:
        body["prompt_cache_key"] = prompt_cache_key
    return body


class GrokClient:
    """Async-context stand-in for the old Anthropic client. ``messages.create`` is the one call."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.messages = _Messages(api_key, prompt_cache_key=uuid.uuid4().hex)

    async def __aenter__(self) -> GrokClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _Messages:
    def __init__(self, api_key: str, *, prompt_cache_key: str) -> None:
        self._api_key = api_key
        self._prompt_cache_key = prompt_cache_key

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        if not self._api_key:
            raise GrokError("GROK_API_KEY is not set")
        extra = kwargs.get("extra_body") or {}
        effort = ((extra.get("output_config") or {}).get("effort")) or kwargs.get("effort")
        body = to_grok_body(
            model=kwargs["model"],
            messages=kwargs.get("messages") or [],
            max_tokens=kwargs["max_tokens"],
            tools=kwargs.get("tools"),
            effort=effort,
            tool_choice=kwargs.get("tool_choice"),
            prompt_cache_key=self._prompt_cache_key,
        )
        return _call(body, self._api_key)


def _call(body: dict[str, Any], api_key: str) -> SimpleNamespace:
    deadline = time.monotonic() + config.grok.total_s
    last: GrokError | None = None
    attempts = max(1, config.grok.retry_attempts)
    for attempt in range(1, attempts + 1):
        try:
            return _attempt(body, api_key)
        except GrokError as exc:
            last = exc
            if not exc.retryable or attempt == attempts:
                raise
            wait = config.grok.retry_base_delay_s * (2 ** (attempt - 1))
            if time.monotonic() + wait >= deadline:
                raise GrokError(f"{exc} (no retry: {config.grok.total_s}s call budget spent)") from exc
            logger.info("Grok attempt %d/%d failed (%s), retrying in %.0fs", attempt, attempts, exc, wait)
            time.sleep(wait)
    assert last is not None
    raise last


def _attempt(body: dict[str, Any], api_key: str) -> SimpleNamespace:
    try:
        resp = requests.post(
            config.grok.api_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=(10, config.grok.timeout_s),
            stream=True,
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise GrokError(f"Grok call failed before a response: {exc}", retryable=True) from exc
    if not resp.ok:
        detail = (resp.text or "")[:300]
        raise GrokError(
            f"Grok HTTP {resp.status_code}: {detail}",
            retryable=resp.status_code in _RETRY_STATUSES,
        )
    try:
        raw = resp.content.decode("utf-8", errors="replace")
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise GrokError(f"Grok stream broke while reading: {exc}", retryable=True) from exc
    msg = _parse_body(raw)
    if not msg.stop_reason:
        raise GrokError("Grok stream ended without a completed response", retryable=True)
    if msg.stop_reason == "refusal":
        raise GrokError("Grok declined the request (refusal)", refusal=True)
    if msg.stop_reason == "max_tokens":
        raise GrokError("Grok output truncated (max_tokens): raise the budget in config")
    return msg


def _parse_body(raw: str) -> SimpleNamespace:
    if "data:" in raw:
        return accumulate_stream(raw)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GrokError("Grok stream ended without a completed response", retryable=True) from exc
    if isinstance(obj, dict) and obj.get("type") in ("response.completed", "response.incomplete"):
        obj = obj.get("response") or obj
    if isinstance(obj, dict) and "output" in obj:
        return message_from_response(obj)
    raise GrokError("Grok stream ended without a completed response", retryable=True)


def _usage(raw: Any) -> SimpleNamespace:
    if not isinstance(raw, dict):
        return SimpleNamespace(input_tokens=0, output_tokens=0)
    tin = raw.get("input_tokens", raw.get("prompt_tokens", 0)) or 0
    tout = raw.get("output_tokens", raw.get("completion_tokens", 0)) or 0
    return SimpleNamespace(input_tokens=int(tin), output_tokens=int(tout))


def _to_tool(tool: Any) -> dict[str, Any]:
    if isinstance(tool, dict) and tool.get("type") == "function" and tool.get("parameters"):
        return tool
    name = tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", "")
    description = tool.get("description") if isinstance(tool, dict) else getattr(tool, "description", "")
    schema = tool.get("input_schema") if isinstance(tool, dict) else getattr(tool, "input_schema", None)
    return {
        "type": "function",
        "name": name or "",
        "description": description or "",
        "parameters": schema or {"type": "object", "properties": {}},
    }


def _to_tool_choice(choice: dict[str, Any] | None) -> Any:
    """Anthropic ``{"type": "tool", "name": ...}`` becomes a Responses forced function."""
    if not choice:
        return None
    if choice.get("type") == "tool" and choice.get("name"):
        return {"type": "function", "name": choice["name"]}
    if choice.get("type") == "function" and choice.get("name"):
        return {"type": "function", "name": choice["name"]}
    return choice


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _to_items(message: Any) -> list[Any]:
    if not message:
        return []
    kind = _get(message, "type")
    if kind in ("function_call", "function_call_output"):
        return [message]
    content = _get(message, "content")
    if isinstance(content, list) and any(_get(b, "type") == "tool_result" for b in content):
        return [
            {
                "type": "function_call_output",
                "call_id": _get(b, "tool_use_id") or "",
                "output": _result_output(_get(b, "content")),
            }
            for b in content
            if _get(b, "type") == "tool_result"
        ]
    if _get(message, "role") == "assistant" and isinstance(content, list):
        items: list[Any] = []
        for block in content:
            btype = _get(block, "type")
            if btype == "reasoning":
                raw = _get(block, "raw")
                if isinstance(raw, dict):
                    items.append(raw)
            elif btype == "tool_use":
                tool_input = _get(block, "input") or {}
                items.append({
                    "type": "function_call",
                    "call_id": _get(block, "id") or "",
                    "name": _get(block, "name") or "",
                    "arguments": tool_input if isinstance(tool_input, str) else json.dumps(tool_input),
                })
            elif btype == "text" and _get(block, "text"):
                items.append({"role": "assistant", "content": _get(block, "text")})
        return items
    if isinstance(content, str):
        return [{"role": _get(message, "role") or "user", "content": content}]
    return [{"role": _get(message, "role") or "user", "content": str(content if content is not None else "")}]


def _result_output(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content if content is not None else "")
