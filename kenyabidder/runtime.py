"""Process runtime: the shared app, background tick loop, snapshot persistence and the FastMCP HTTP endpoint."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from pathlib import Path

from .app import create_app
from .clock import SystemClock
from .mcpx.server import build_server
from .store import Store

log = logging.getLogger("kenyabidder.runtime")


class Runtime:
    def __init__(self, data_file: str | None = None, mcp_port: int | None = None, mcp_host: str = "127.0.0.1", tick_ms: int = 50):
        self.data_file = data_file
        self.store = Store.load(data_file) if data_file else Store()
        self.app = create_app(store=self.store, clock=SystemClock())
        self.mcp_host, self.mcp_port, self.tick_ms = mcp_host, mcp_port, tick_ms
        self.tasks: list[asyncio.Task] = []
        s = self.store.settings
        s.setdefault("storage_secret", secrets.token_hex(32))
        s.setdefault("mcp_key", secrets.token_urlsafe(24))

    @property
    def mcp_key(self) -> str:
        return os.environ.get("KENYABIDDER_MCP_KEY") or self.store.settings["mcp_key"]

    @property
    def storage_secret(self) -> str:
        return os.environ.get("KENYABIDDER_SECRET") or self.store.settings["storage_secret"]

    @property
    def mcp_url(self) -> str | None:
        return f"http://{self.mcp_host}:{self.mcp_port}/mcp" if self.mcp_port else None

    async def _tick_loop(self) -> None:
        while True:
            try:
                self.app.tick()
            except Exception:  # noqa: BLE001  the loop must survive any single failure
                log.exception("tick failed")
            await asyncio.sleep(self.tick_ms / 1000)

    async def _save_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            self.save()

    def save(self) -> None:
        if self.data_file:
            try:
                self.store.save(self.data_file)
            except Exception:  # noqa: BLE001
                log.exception("snapshot failed")

    async def _serve_mcp(self) -> None:
        from fastmcp.server.auth import StaticTokenVerifier
        auth = StaticTokenVerifier(tokens={self.mcp_key: {"client_id": "kenyabidder-admin", "scopes": []}})
        server = build_server(self.app, internal=False, auth=auth)
        try:
            await server.run_async(transport="http", host=self.mcp_host, port=self.mcp_port, path="/mcp", show_banner=False)
        except Exception:  # noqa: BLE001
            log.exception("MCP HTTP server stopped")

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.tasks.append(loop.create_task(self._tick_loop()))
        if self.data_file:
            self.tasks.append(loop.create_task(self._save_loop()))
        if self.mcp_port:
            self.tasks.append(loop.create_task(self._serve_mcp()))

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.save()
