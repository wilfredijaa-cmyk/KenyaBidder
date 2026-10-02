"""MCP registry + client manager.

Admins register MCP servers (the built-in KenyaBidder server, remote HTTP servers, or local stdio servers).
Tools are discovered with FastMCP's client and cached; agents are then assigned specific tools by reference
(``"<mcp_id>:<tool_name>"``). Internal/state-changing tools of the built-in server can never be assigned.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from typing import Callable

from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport

from ..errors import bad, conflict, not_found
from ..security import check_outbound_url

BUILTIN_ID = "builtin"
ROLES = ["BIDDER", "SELLER"]
MAX_RESULT_CHARS = 8000


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:24] or "mcp"


class McpManager:
    def __init__(self, store, clock, builtin_factory: Callable[[], FastMCP] | None = None, allow_stdio: bool | None = None):
        self.store, self.clock = store, clock
        self._builtin_factory = builtin_factory
        self._builtin: FastMCP | None = None
        self.allow_stdio = os.environ.get("KENYABIDDER_ALLOW_STDIO") == "1" if allow_stdio is None else allow_stdio
        self._locks: dict[str, asyncio.Lock] = {}

    # ---------- registry ----------

    def ensure_builtin(self) -> dict:
        if BUILTIN_ID not in self.store.mcps:
            self.store.mcps[BUILTIN_ID] = {
                "id": BUILTIN_ID, "name": "KenyaBidder built-in", "slug": "kb", "transport": "builtin", "url": "",
                "bearer_token": "", "bearer_env": "", "command": "", "args": [], "env": {}, "roles": list(ROLES),
                "enabled": True, "disabled_tools": [], "tools": [], "tools_refreshed_at": None, "last_error": None,
                "builtin": True, "created_at": self.clock.now()}
        return self.store.mcps[BUILTIN_ID]

    def add(self, *, name: str, transport: str, url: str = "", bearer_token: str = "", bearer_env: str = "",
            command: str = "", args: list[str] | None = None, env: dict[str, str] | None = None,
            roles: list[str] | None = None, enabled: bool = True) -> dict:
        name = (name or "").strip()
        if not name:
            raise bad("INVALID_MCP", "name is required")
        if transport not in ("http", "stdio"):
            raise bad("INVALID_MCP", "transport must be http or stdio")
        if transport == "http" and not url.startswith(("http://", "https://")):
            raise bad("INVALID_MCP", "url (http/https) is required for HTTP MCP servers")
        if transport == "http":
            check_outbound_url(url.strip(), "url")
        if transport == "stdio":
            if not self.allow_stdio:
                raise bad("STDIO_DISABLED", "stdio MCP servers run local commands and are disabled — start the app with KENYABIDDER_ALLOW_STDIO=1 to enable")
            if not command.strip():
                raise bad("INVALID_MCP", "command is required for stdio MCP servers")
        roles = roles or list(ROLES)
        if any(r not in ROLES for r in roles):
            raise bad("INVALID_MCP", "roles must be a subset of BIDDER, SELLER")
        if any(m["name"].lower() == name.lower() for m in self.store.mcps.values()):
            raise conflict("NAME_TAKEN", f"an MCP server named {name!r} already exists")
        slug, base, i = slugify(name), slugify(name), 2
        while any(m["slug"] == slug for m in self.store.mcps.values()):
            slug, i = f"{base}_{i}", i + 1
        m = {"id": str(uuid.uuid4()), "name": name, "slug": slug, "transport": transport, "url": url.strip(),
             "bearer_token": bearer_token, "bearer_env": bearer_env, "command": command.strip(), "args": args or [],
             "env": env or {}, "roles": roles, "enabled": enabled, "disabled_tools": [], "tools": [],
             "tools_refreshed_at": None, "last_error": None, "builtin": False, "created_at": self.clock.now()}
        self.store.mcps[m["id"]] = m
        return m

    def update(self, mcp_id: str, **patch) -> dict:
        m = self.get(mcp_id)
        allowed = {"enabled", "roles", "disabled_tools"} if m["builtin"] else \
            {"name", "url", "bearer_token", "bearer_env", "command", "args", "env", "roles", "enabled", "disabled_tools"}
        for k, v in patch.items():
            if k not in allowed or v is None:
                continue
            if k == "bearer_token" and v == "":
                continue  # blank = keep
            m[k] = v
        return m

    def remove(self, mcp_id: str) -> None:
        m = self.get(mcp_id)
        if m["builtin"]:
            raise bad("BUILTIN", "the built-in server cannot be removed (disable it instead)")
        users = [a["agent_id"][:6] for a in self.store.agents.values()
                 if any(r.split(":", 1)[0] == mcp_id for r in a.get("config", {}).get("tools", []))]
        if users:
            raise conflict("IN_USE", f"tools from this MCP are assigned to agent(s): {', '.join(users)} — unassign them first")
        del self.store.mcps[mcp_id]

    def get(self, mcp_id: str) -> dict:
        m = self.store.mcps.get(mcp_id)
        if not m:
            raise not_found("MCP_NOT_FOUND", "MCP server not found")
        return m

    def list(self, *, enabled_only: bool = False, role: str | None = None) -> list[dict]:
        self.ensure_builtin()
        return [m for m in sorted(self.store.mcps.values(), key=lambda x: (not x["builtin"], x["name"].lower()))
                if (not enabled_only or m["enabled"]) and (not role or role in m["roles"])]

    @staticmethod
    def masked(m: dict) -> dict:
        out = {k: v for k, v in m.items() if k not in ("bearer_token", "env")}
        out["has_bearer_token"] = bool(m.get("bearer_token"))
        out["env_keys"] = sorted(m.get("env", {}))
        return out

    # ---------- transport ----------

    def _builtin_server(self) -> FastMCP:
        if self._builtin is None:
            if not self._builtin_factory:
                raise bad("NO_BUILTIN", "built-in MCP server is not available")
            self._builtin = self._builtin_factory()
        return self._builtin

    def _client(self, m: dict) -> Client:
        t = m["transport"]
        if t == "builtin":
            return Client(self._builtin_server())
        if t == "http":
            token = m.get("bearer_token") or (os.environ.get(m["bearer_env"]) if m.get("bearer_env") else None)
            headers = {"authorization": f"Bearer {token}"} if token else None
            return Client(StreamableHttpTransport(m["url"], headers=headers))
        if not self.allow_stdio:
            raise bad("STDIO_DISABLED", "stdio MCP servers are disabled")
        return Client(StdioTransport(m["command"], m.get("args", []), env=m.get("env") or None))

    # ---------- discovery ----------

    async def refresh_tools(self, mcp_id: str, timeout: float = 20.0) -> list[dict]:
        m = self.get(mcp_id)
        try:
            async with asyncio.timeout(timeout):
                async with self._client(m) as c:
                    tools = await c.list_tools()
        except Exception as e:  # noqa: BLE001
            m["last_error"] = f"{type(e).__name__}: {str(e)[:300]}"
            raise bad("MCP_UNREACHABLE", m["last_error"]) from e
        out = []
        for t in tools:
            tags = ((t.meta or {}).get("fastmcp", {}) or {}).get("tags") or []
            if m["builtin"] and "internal" in tags:
                continue  # state-changing tools are never assignable
            out.append({"name": t.name, "description": (t.description or "")[:600],
                        "input_schema": t.input_schema or {"type": "object", "properties": {}}, "tags": list(tags)})
        m["tools"], m["tools_refreshed_at"], m["last_error"] = out, self.clock.now(), None
        return out

    # ---------- assignment ----------

    def assignable_tools(self, role: str) -> list[dict]:
        out = []
        for m in self.list(enabled_only=True, role=role):
            for t in m["tools"]:
                if t["name"] in m["disabled_tools"]:
                    continue
                out.append({"ref": f"{m['id']}:{t['name']}", "mcp_id": m["id"], "mcp": m["name"], "slug": m["slug"],
                            "tool": t["name"], "description": t["description"], "input_schema": t["input_schema"]})
        return out

    def validate_refs(self, role: str, refs: list[str]) -> list[str]:
        ok = {t["ref"] for t in self.assignable_tools(role)}
        for r in refs:
            if r not in ok:
                raise bad("INVALID_TOOL", f"tool {r!r} is not available to {role} agents (unknown, disabled, internal, or its MCP server is not enabled for this role)")
        return list(dict.fromkeys(refs))

    # ---------- execution ----------

    async def call(self, mcp_id: str, tool: str, args: dict, timeout: float = 15.0) -> dict:
        """Call one tool. Never raises: returns {ok, text} with truncated text. Output is untrusted data."""
        m = self.get(mcp_id)
        if not m["enabled"]:
            return {"ok": False, "text": f"MCP server {m['name']!r} is disabled"}
        if tool in m["disabled_tools"] or (m["tools"] and tool not in {t["name"] for t in m["tools"]}):
            return {"ok": False, "text": f"tool {tool!r} is not available"}
        try:
            async with asyncio.timeout(timeout):
                async with self._client(m) as c:
                    r = await c.call_tool(tool, args or {}, raise_on_error=False)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "text": f"{type(e).__name__}: {str(e)[:300]}"}
        text = "\n".join(getattr(b, "text", "") for b in r.content if getattr(b, "text", None))
        if not text and r.structured_content is not None:
            text = json.dumps(r.structured_content, default=str)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n…[truncated]"
        return {"ok": not r.is_error, "text": text}
