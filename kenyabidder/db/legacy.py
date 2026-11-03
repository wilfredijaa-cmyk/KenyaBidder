"""One-time import of the pre-DuckDB SQLite wallet (``wallet.db``) so an upgrade never loses a purchase."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .base import Database

log = logging.getLogger("kenyabidder.db")


def import_sqlite_wallet(db: Database, path: str) -> bool:
    p = Path(path)
    if not p.exists() or db.scalar("SELECT count(*) FROM ledger") or db.scalar("SELECT count(*) FROM orders"):
        return False
    src = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        with db.tx() as c:
            for r in src.execute("SELECT user_id, llm_id, balance FROM balances"):
                c.execute("INSERT INTO balances(user_id, llm_id, balance) VALUES (?,?,?)", tuple(r))
            # sequence numbers are re-issued in the original order (portable: no setval); entry_id remains the stable identity
            for r in src.execute("SELECT entry_id, user_id, llm_id, agent_id, kind, tokens, used, balance_after, ref, at, meta FROM ledger ORDER BY seq"):
                c.execute("INSERT INTO ledger(entry_id, user_id, llm_id, agent_id, kind, tokens, used, balance_after, ref, ts, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?)", tuple(r))
            cols = "id, user_id, pack_id, pack_name, llm_id, tokens, amount_kes, provider, status, phone, external_ref, receipt, note, created_at, updated_at, data"
            for r in src.execute(f"SELECT {cols} FROM orders"):  # nosec B608
                c.execute(f"INSERT INTO orders({cols}) VALUES ({','.join('?' * 16)})", tuple(r))  # nosec B608
                if r["receipt"] and r["status"] in ("AWAITING_REVIEW", "PAID"):
                    c.execute("INSERT INTO receipt_claims(receipt, order_id) VALUES (?,?)", (r["receipt"], r["id"]))
    finally:
        src.close()
    p.rename(p.with_name(p.name + ".imported"))
    log.warning("imported legacy SQLite wallet %s (kept as %s.imported)", p, p.name)
    return True
