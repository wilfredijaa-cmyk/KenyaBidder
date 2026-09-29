"""Process runtime: the shared app, background tick loop, snapshot persistence and the FastMCP HTTP endpoint."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from pathlib import Path

from .app import create_app
from .clock import SystemClock
from .mcpx.server import build_server
from .backups import BackupService
from .db import open_database
from .db.legacy import import_sqlite_wallet
from .persist import StateLocked, StatePersistence
from .store import Store

log = logging.getLogger("kenyabidder.runtime")


class Runtime:
    LEASE_RENEW_EVERY = 10  # save-loop beats (1s each)

    def __init__(self, data_file: str | None = None, mcp_port: int | None = None, mcp_host: str = "127.0.0.1", tick_ms: int = 50, database_url: str | None = None):
        self.data_file = data_file
        url = database_url or os.environ.get("KENYABIDDER_DATABASE_URL")
        if not url and data_file:
            url = "duckdb://" + str(Path(data_file).with_name("kenyabidder.duckdb"))
        self.database_url = url or ":memory:"
        self.db = open_database(self.database_url)
        self.persistence: StatePersistence | None = None
        self.backups: BackupService | None = None
        if url:  # durable mode: state lives in the database, guarded by a single-writer lease
            self.persistence = StatePersistence(self.db)
            self.persistence.acquire_lease(force=os.environ.get("KENYABIDDER_FORCE_LEASE") == "1")
            legacy_dir = Path(data_file).parent if data_file else None
            if legacy_dir:
                import_sqlite_wallet(self.db, str(legacy_dir / "wallet.db"))
            self.store = self.persistence.load()
            if data_file:
                self.store = self.persistence.import_legacy_json(data_file) or self.store
            backup_dir = os.environ.get("KENYABIDDER_BACKUP_DIR") or (str(legacy_dir / "backups") if legacy_dir else None)
            if backup_dir and self.db.dialect == "duckdb":
                self.backups = BackupService(self.db, backup_dir, SystemClock(), keep=int(os.environ.get("KENYABIDDER_BACKUP_KEEP", 14)),
                                             every_hours=float(os.environ.get("KENYABIDDER_BACKUP_EVERY_HOURS", 24)))
                self.backups.prime()
        else:
            self.store = Store()
        self._flush_lock = asyncio.Lock()
        self.started_at = 0.0
        self.last_tick_at = self.last_flush_at = 0.0
        self.flush_errors = self.flush_errors_streak = 0
        self.lost_lease = False
        self.app = create_app(store=self.store, clock=SystemClock(), database=self.db)
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
            self.last_tick_at = time.time()
            await asyncio.sleep(self.tick_ms / 1000)

    async def flush(self) -> None:
        """Persist what changed. Diffing runs on the loop thread one collection at a time (yielding between them, so bids are never
        stalled); the database writes run in a worker thread."""
        p = self.persistence
        if not p:
            return
        async with self._flush_lock:
            ops = []
            for name in p.names():
                ops.append(p.collect(self.store, name))
                await asyncio.sleep(0)
            await asyncio.to_thread(p.apply, ops)
            self.last_flush_at = time.time()

    async def _save_loop(self) -> None:
        beats = 0
        while True:
            await asyncio.sleep(1)
            beats += 1
            if beats % self.LEASE_RENEW_EVERY == 0 and not self.lost_lease:  # the lease is renewed whether or not flushing works: a failing disk must not hand the database to a second process
                try:
                    await asyncio.to_thread(self.persistence.renew_lease)
                except StateLocked:
                    self.lost_lease = True
                    log.critical("LOST THE WRITER LEASE — another process now owns this database. This instance stops writing; restart it after checking for a duplicate deployment.")
                except Exception:  # noqa: BLE001  transient database error: try again at the next beat
                    log.exception("lease renewal failed")
            if self.lost_lease:
                continue  # never write state we no longer own
            try:
                await self.flush()
                self.flush_errors_streak = 0
            except Exception:  # noqa: BLE001
                self.flush_errors += 1
                self.flush_errors_streak += 1
                log.exception("state flush failed")

    async def _maintenance_loop(self) -> None:
        """Every 30s: recover lost M-Pesa callbacks, expire stale orders, wake capped/blocked agents, send evening summaries."""
        a = self.app
        while True:
            await asyncio.sleep(30)
            for name, step in (("mpesa reconcile", a.billing.reconcile), ("blocked retry", a.orchestrator.retry_blocked)):
                try:
                    await step()
                except Exception:  # noqa: BLE001
                    log.exception("%s failed", name)
            if self.backups and self.backups.due():
                try:
                    await asyncio.to_thread(self.backups.run)
                except Exception:  # noqa: BLE001
                    log.exception("backup failed")
            for name, sync_step in (("order expiry", a.billing.expire_stale), ("subscriptions", a.subscriptions.tick),
                                    ("plan recovery", lambda: a.subscriptions.recover(a.billing.paid_plan_orders(a.clock.now() - 400 * 24 * 3600_000))), ("evening summaries", a.sellers.send_daily_summaries),
                                    ("pruning", lambda: self.store.prune(a.clock.now()))):
                try:
                    sync_step()
                except Exception:  # noqa: BLE001  one failing job must not skip the others
                    log.exception("%s failed", name)

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
        self.started_at = self.last_tick_at = time.time()
        problems = self.app.wallet.verify_integrity()
        if problems:  # never silently run with a ledger that disagrees with balances
            log.error("WALLET INTEGRITY PROBLEMS: %s", "; ".join(problems[:5]))
        self.tasks.append(loop.create_task(self._tick_loop()))
        self.tasks.append(loop.create_task(self._maintenance_loop()))
        if self.persistence:
            self.tasks.append(loop.create_task(self._save_loop()))
        if self.mcp_port:
            self.tasks.append(loop.create_task(self._serve_mcp()))

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.persistence and not self.lost_lease:
            try:
                self.persistence.flush_all(self.store)
                self.persistence.release_lease()
            except Exception:  # noqa: BLE001
                log.exception("final state flush failed")
        self.app.wallet.close()
