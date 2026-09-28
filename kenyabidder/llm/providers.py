"""Provider-neutral tool-calling LLM interface.

Neutral message shapes (what the agent loop produces):
  {"role": "user", "content": str}
  {"role": "assistant", "text": str, "tool_calls": [ToolCall, ...]}
  {"role": "tool", "tool_call_id": str, "name": str, "content": str}
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Completion:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    usage: dict = field(default_factory=dict)


class LlmError(Exception):
    pass


class Provider(Protocol):
    async def complete(self, system: str, messages: list[dict], tools: list[ToolSpec], *,
                       max_tokens: int = 1024, temperature: float | None = None,
                       force_tool: str | None = None) -> Completion: ...


def _args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


class AnthropicProvider:
    """Anthropic Messages API (native tool use, prompt caching on the system prompt)."""

    def __init__(self, api_key: str, model: str, base_url: str | None = None, timeout: float = 30.0,
                 http_client: httpx.AsyncClient | None = None):
        import anthropic
        kw: dict[str, Any] = {"api_key": api_key, "timeout": timeout, "max_retries": 0}
        if base_url:
            kw["base_url"] = base_url
        if http_client is not None:
            kw["http_client"] = http_client
        self.client = anthropic.AsyncAnthropic(**kw)
        self.model = model

    @staticmethod
    def _messages(messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                blocks: list[dict] = []
                if m.get("text"):
                    blocks.append({"type": "text", "text": m["text"]})
                for c in m.get("tool_calls", []):
                    blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.args})
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "…"}]})
            else:  # tool result — consecutive results share one user turn
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                        and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
        return out

    async def complete(self, system, messages, tools, *, max_tokens=1024, temperature=None, force_tool=None):
        kw: dict[str, Any] = {
            "model": self.model, "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": self._messages(messages),
        }
        if temperature is not None:
            kw["temperature"] = temperature
        if tools:
            kw["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools]
            kw["tool_choice"] = {"type": "tool", "name": force_tool} if force_tool else {"type": "auto"}
        try:
            r = await self.client.messages.create(**kw)
        except Exception as e:  # noqa: BLE001
            raise LlmError(f"{type(e).__name__}: {e}") from e
        text = "".join(b.text for b in r.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input or {})) for b in r.content if b.type == "tool_use"]
        usage = {"input": getattr(r.usage, "input_tokens", 0), "output": getattr(r.usage, "output_tokens", 0)}
        return Completion(text, calls, r.stop_reason or "", usage)


class OpenAICompatProvider:
    """Any OpenAI-compatible /chat/completions endpoint (OpenAI, Ollama, vLLM, OpenRouter, Together, …)."""

    def __init__(self, api_key: str | None, model: str, base_url: str = "https://api.openai.com/v1",
                 timeout: float = 30.0, http_client: httpx.AsyncClient | None = None):
        self.api_key, self.model = api_key, model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.http = http_client

    @staticmethod
    def _messages(system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.get("text") or None}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [{"id": c.id, "type": "function",
                                          "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                                         for c in m["tool_calls"]]
                out.append(msg)
            else:
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        return out

    async def complete(self, system, messages, tools, *, max_tokens=1024, temperature=None, force_tool=None):
        body: dict[str, Any] = {"model": self.model, "messages": self._messages(system, messages), "max_tokens": max_tokens}
        if temperature is not None:
            body["temperature"] = temperature
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                                 "parameters": t.input_schema}} for t in tools]
            body["tool_choice"] = {"type": "function", "function": {"name": force_tool}} if force_tool else "auto"
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        client = self.http or httpx.AsyncClient(timeout=self.timeout)
        try:
            r = await client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
            if r.status_code >= 400:
                raise LlmError(f"HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
        except LlmError:
            raise
        except Exception as e:  # noqa: BLE001
            raise LlmError(f"{type(e).__name__}: {e}") from e
        finally:
            if self.http is None:
                await client.aclose()
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise LlmError("malformed response") from e
        calls = [ToolCall(c.get("id") or f"call_{i}", c["function"]["name"], _args(c["function"].get("arguments")))
                 for i, c in enumerate(msg.get("tool_calls") or []) if c.get("function")]
        u = data.get("usage") or {}
        return Completion(msg.get("content") or "", calls, data["choices"][0].get("finish_reason") or "",
                          {"input": u.get("prompt_tokens", 0), "output": u.get("completion_tokens", 0)})


class ScriptedProvider:
    """Deterministic provider for tests and offline demos: returns queued completions, records requests."""

    def __init__(self, script: list[Completion | Exception] | None = None):
        self.script = list(script or [])
        self.requests: list[dict] = []

    async def complete(self, system, messages, tools, *, max_tokens=1024, temperature=None, force_tool=None):
        self.requests.append({"system": system, "messages": list(messages), "tools": [t.name for t in tools], "force_tool": force_tool})
        if not self.script:
            raise LlmError("script exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
