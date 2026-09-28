"""Entry point: ``python -m kenyabidder`` (NiceGUI console + FastMCP endpoint + WhatsApp webhook)."""
from __future__ import annotations

import argparse
import hmac
import logging
import os

from fastapi import HTTPException, Request
from fastapi.responses import PlainTextResponse
from nicegui import app as ng_app
from nicegui import ui

from . import runtime as runtime_mod
from .channels import handle_whatsapp_webhook
from .errors import AppError
from .ui import admin, agents_page, common, pages, wallet_page


def build(runtime: runtime_mod.Runtime) -> None:
    """Register pages, webhooks and lifecycle hooks against a runtime (also used by the UI tests)."""
    common.bind(runtime)
    pages.register()
    agents_page.register()
    wallet_page.register()
    admin.register()
    ng_app.on_startup(runtime.start)
    ng_app.on_shutdown(runtime.stop)
    core = runtime.app

    @ng_app.get("/healthz")
    def healthz():
        return {"ok": True}

    @ng_app.get("/webhooks/whatsapp")
    def wa_verify(request: Request):
        token = os.environ.get("WHATSAPP_VERIFY_TOKEN")
        q = request.query_params
        if token and hmac.compare_digest(q.get("hub.verify_token", ""), token):
            return PlainTextResponse(q.get("hub.challenge", ""))
        raise HTTPException(403, "webhook verification failed")

    @ng_app.post("/webhooks/mpesa/{secret}")
    async def mpesa_callback(secret: str, request: Request):
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        try:
            return await core.billing.handle_mpesa_callback(secret, payload)
        except AppError as e:
            raise HTTPException(e.status, e.message) from None

    @ng_app.post("/webhooks/whatsapp")
    async def wa_inbound(request: Request):
        raw = await request.body()
        try:
            return await handle_whatsapp_webhook(core.router, raw, request.headers.get("x-hub-signature-256"))
        except AppError as e:
            raise HTTPException(e.status, e.message) from None


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="kenyabidder", description="KenyaBidder — AI auction matchmaking")
    p.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    p.add_argument("--data", default=os.environ.get("KENYABIDDER_DATA", "data/state.json"), help="state snapshot file ('' = in-memory only)")
    p.add_argument("--mcp-port", type=int, default=int(os.environ.get("KENYABIDDER_MCP_PORT", 0)), help="serve KenyaBidder's FastMCP endpoint on this port (0 = off)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    rt = runtime_mod.Runtime(data_file=args.data or None, mcp_port=args.mcp_port or None)
    build(rt)
    ui.run(host=args.host, port=args.port, title="KenyaBidder", storage_secret=rt.storage_secret, reload=False, show=False, dark=None, favicon="🔨")


if __name__ in {"__main__", "__mp_main__"}:
    main()
