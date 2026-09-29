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

from . import metrics as metrics_mod
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
        return {"ok": True}  # liveness: the process is up

    @ng_app.get("/readyz")
    async def readyz():  # async: runs on the event loop that owns the state (a worker thread would iterate dicts being mutated)
        ok, checks = metrics_mod.readiness(runtime)  # readiness: safe to route traffic here
        if not ok:
            raise HTTPException(503, detail=checks)
        return {"ok": True, "checks": checks}

    @ng_app.get("/metrics")
    async def metrics(request: Request):
        token = os.environ.get("KENYABIDDER_METRICS_TOKEN")
        auth = request.headers.get("authorization", "")
        if token:
            if not hmac.compare_digest(auth.encode(), f"Bearer {token}".encode()):
                raise HTTPException(401, "metrics token required")
        elif (request.client.host if request.client else "") not in ("127.0.0.1", "::1"):
            raise HTTPException(403, "set KENYABIDDER_METRICS_TOKEN to scrape metrics remotely")
        return PlainTextResponse(metrics_mod.render(runtime), media_type="text/plain; version=0.0.4")

    @ng_app.get("/webhooks/whatsapp")
    def wa_verify(request: Request):
        token = os.environ.get("WHATSAPP_VERIFY_TOKEN")
        q = request.query_params
        if token and hmac.compare_digest(q.get("hub.verify_token", "").encode(), token.encode()):
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
    p.add_argument("--data", default=os.environ.get("KENYABIDDER_DATA", "data/state.json"), help="data file location; state lives in kenyabidder.duckdb beside it ('' = in-memory only; set KENYABIDDER_DATABASE_URL for PostgreSQL)")
    p.add_argument("--mcp-port", type=int, default=int(os.environ.get("KENYABIDDER_MCP_PORT", 0)), help="serve KenyaBidder's FastMCP endpoint on this port (0 = off)")
    p.add_argument("command", nargs="*", help="'restore <backup file>' — replace the database with a backup (app must be stopped)")
    args = p.parse_args(argv)
    if args.command[:1] == ["restore"]:
        from .backups import restore_backup
        if len(args.command) != 2:
            p.error("usage: python -m kenyabidder restore <backup file>")
        dest = os.path.join(os.path.dirname(args.data or "data/state.json") or ".", "kenyabidder.duckdb")
        restore_backup(args.command[1], dest)
        print(f"Restored {args.command[1]} to {dest} (previous database kept as .before-restore-<time>). Start the app again.")
        return
    metrics_mod.configure_logging()
    rt = runtime_mod.Runtime(data_file=args.data or None, mcp_port=args.mcp_port or None)
    build(rt)
    ui.run(host=args.host, port=args.port, title="KenyaBidder", storage_secret=rt.storage_secret, reload=False, show=False, dark=None, favicon="🔨", uvicorn_logging_level="warning", access_log=False)  # no access log: the M-Pesa callback secret is in the URL path


if __name__ in {"__main__", "__mp_main__"}:
    main()
