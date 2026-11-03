"""Ordered, versioned migrations. Append new entries; never edit an applied one. Portable SQL only (see base.py)."""
from __future__ import annotations

import logging

from .base import Database

log = logging.getLogger("kenyabidder.db")

MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (1, "wallet: balances, ledger, orders, receipt claims", [
        """CREATE TABLE balances(
             user_id VARCHAR NOT NULL, llm_id VARCHAR NOT NULL,
             balance BIGINT NOT NULL CHECK (balance >= 0),
             PRIMARY KEY (user_id, llm_id))""",
        "CREATE SEQUENCE ledger_seq START 1",
        """CREATE TABLE ledger(
             seq BIGINT PRIMARY KEY DEFAULT nextval('ledger_seq'),
             entry_id VARCHAR NOT NULL UNIQUE,
             user_id VARCHAR NOT NULL, llm_id VARCHAR NOT NULL, agent_id VARCHAR,
             kind VARCHAR NOT NULL,
             tokens BIGINT NOT NULL,
             used BIGINT NOT NULL DEFAULT 0,
             balance_after BIGINT NOT NULL,
             ref VARCHAR, ts BIGINT NOT NULL, meta VARCHAR NOT NULL DEFAULT '{}',
             UNIQUE (kind, ref))""",
        "CREATE INDEX ix_ledger_user ON ledger(user_id, seq)",
        "CREATE INDEX ix_ledger_agent ON ledger(agent_id, ts)",
        """CREATE TABLE orders(
             id VARCHAR PRIMARY KEY, user_id VARCHAR NOT NULL, pack_id VARCHAR, pack_name VARCHAR, llm_id VARCHAR NOT NULL,
             tokens BIGINT NOT NULL, amount_kes BIGINT NOT NULL, provider VARCHAR NOT NULL, status VARCHAR NOT NULL,
             phone VARCHAR, external_ref VARCHAR, receipt VARCHAR, note VARCHAR,
             created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL, data VARCHAR NOT NULL DEFAULT '{}')""",
        "CREATE INDEX ix_orders_user ON orders(user_id, created_at)",
        "CREATE INDEX ix_orders_ext ON orders(external_ref)",
        # 'one M-Pesa code, one purchase': a receipt is CLAIMED while its order is awaiting review or paid, released otherwise.
        # (A partial unique index would say the same, but DuckDB has none — a claims table is portable to every engine.)
        "CREATE TABLE receipt_claims(receipt VARCHAR PRIMARY KEY, order_id VARCHAR NOT NULL)",
    ]),
    (2, "durable application state: documents, append-only logs, key/value", [
        # one generic pair of tables keeps the in-memory working set simple; typed tables/views can be layered on later
        """CREATE TABLE docs(
             collection VARCHAR NOT NULL, id VARCHAR NOT NULL, body VARCHAR NOT NULL, updated_at BIGINT NOT NULL,
             PRIMARY KEY (collection, id))""",
        """CREATE TABLE logs(
             collection VARCHAR NOT NULL, seq BIGINT NOT NULL, body VARCHAR NOT NULL,
             PRIMARY KEY (collection, seq))""",
        "CREATE TABLE kv(key VARCHAR PRIMARY KEY, body VARCHAR NOT NULL)",
    ]),
]


def migrate(db: Database) -> int:
    """Apply pending migrations, each in its own transaction. Returns the resulting schema version."""
    db.execute("CREATE TABLE IF NOT EXISTS schema_version(version BIGINT PRIMARY KEY, description VARCHAR NOT NULL, applied_at BIGINT NOT NULL)")
    done = {r["version"] for r in db.query("SELECT version FROM schema_version")}
    import time
    for version, desc, statements in MIGRATIONS:
        if version in done:
            continue
        with db.tx() as t:
            for s in statements:
                t.execute(s)
            t.execute("INSERT INTO schema_version(version, description, applied_at) VALUES (?,?,?)", (version, desc, int(time.time() * 1000)))
        log.info("applied migration %s: %s", version, desc)
    return db.scalar("SELECT MAX(version) FROM schema_version") or 0
