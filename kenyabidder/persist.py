"""Durable application state in the SQL database (DuckDB today, PostgreSQL later).

The engine keeps its working set in memory (bid latency); this module makes that set durable without a rewrite:

* every entity is one JSON document in ``docs`` and every append-only history is rows in ``logs`` — diffed against what was
  last written, so a flush only touches what changed, and finished auctions / idempotency keys are frozen (never re-checked);
* ``collect()`` runs on the event-loop thread (consistent, and cheap because it is sliced per collection); ``apply()`` does the
  database I/O and can run in a worker thread;
* a **single-writer lease** in ``kv`` makes a second process refuse to start instead of silently forking the state. Horizontal
  scale-out of the *engine* needs the sharded state described in the README; the database itself (money, orders) is multi-process safe.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from pathlib import Path

from .db import Database, DatabaseError, IntegrityError
from .store import LIST_KEYS, MAP_KEYS, Log, Store

log = logging.getLogger("kenyabidder.persist")

TERMINAL_AUCTION = {"SETTLED", "CANCELLED"}
FINAL_MATCH = {"COMPLETED", "FELL_THROUGH", "NO_RESPONSE", "DISPUTED"}
DEAD_TRIGGER = {"DONE", "CANCELLED", "EXHAUSTED"}
MATCH_FREEZE_MS = 40 * 24 * 3600_000  # past the 30-day dispute window nothing can touch a finished match any more
SLOW = {"kbs": 30_000, "llms": 10_000, "mcps": 10_000, "packs": 10_000}  # big or rarely-edited documents: look at them every N ms, not every flush
TAIL_RECHECK = 100  # newest log rows are re-compared on every flush (a few histories flip a flag after being appended)
LEASE_MS = 30_000


class StateLocked(DatabaseError):
    """Another process holds the writer lease for this database."""


def _dump(obj) -> str:
    return json.dumps(obj, default=str, sort_keys=True, separators=(",", ":"))


def _digest(body: str) -> bytes:
    return hashlib.blake2b(body.encode(), digest_size=12).digest()


class StatePersistence:
    def __init__(self, db: Database, clock=None):
        self.db = db
        self.clock = clock
        self.owner = uuid.uuid4().hex
        self._seen: dict[str, dict[str, bytes]] = {k: {} for k in MAP_KEYS}
        self._frozen: dict[str, set[str]] = {k: set() for k in MAP_KEYS}
        self._log_next: dict[str, int] = {k: 0 for k in LIST_KEYS}   # first absolute seq NOT yet written
        self._log_floor: dict[str, int] = {k: 0 for k in LIST_KEYS}  # rows below this were already deleted
        self._log_tail: dict[str, dict[int, bytes]] = {k: {} for k in LIST_KEYS}
        self._log_version: dict[str, int] = {k: 0 for k in LIST_KEYS}  # Log.version last made durable
        self._kv_seen: dict[str, bytes] = {}
        self._checked_at: dict[str, int] = {}

    def now(self) -> int:
        import time
        return self.clock.now() if self.clock else int(time.time() * 1000)

    # ------------------------------------------------------------------ writer lease
    def acquire_lease(self, force: bool = False) -> None:
        now = self.now()
        new = _dump({"owner": self.owner, "expires": now + LEASE_MS})
        row = self.db.query_one("SELECT body FROM kv WHERE key = 'writer_lease'")
        if row is None:
            try:
                self.db.execute("INSERT INTO kv(key, body) VALUES ('writer_lease', ?)", (new,))
                return
            except IntegrityError:
                raise StateLocked("another KenyaBidder process is starting on this database") from None
        cur = json.loads(row["body"])
        if not force and cur["owner"] != self.owner and cur["expires"] > now:
            raise StateLocked(f"another KenyaBidder process owns this database (lease valid for {(cur['expires'] - now) // 1000}s more). "
                              "Stop it, or set KENYABIDDER_FORCE_LEASE=1 if it is known to be dead.")
        if self.db.execute("UPDATE kv SET body = ? WHERE key = 'writer_lease' AND body = ?", (new, row["body"])) != 1:
            raise StateLocked("lost a race for the database writer lease")

    def renew_lease(self) -> None:
        self.acquire_lease()

    def release_lease(self) -> None:
        row = self.db.query_one("SELECT body FROM kv WHERE key = 'writer_lease'")
        if row and json.loads(row["body"])["owner"] == self.owner:
            self.db.execute("DELETE FROM kv WHERE key = 'writer_lease' AND body = ?", (row["body"],))

    # ------------------------------------------------------------------ load
    def is_empty(self) -> bool:
        return not self.db.scalar("SELECT count(*) FROM docs") and not self.db.scalar("SELECT count(*) FROM kv WHERE key <> 'writer_lease'")

    def load(self) -> Store:
        s = Store()
        for name in MAP_KEYS:
            target = getattr(s, name)
            for r in self.db.query("SELECT id, body FROM docs WHERE collection = ?", (name,)):
                ent = json.loads(r["body"])
                target[r["id"]] = ent
                self._seen[name][r["id"]] = _digest(_dump(ent))
                self._maybe_freeze(name, r["id"], ent)
        for name in LIST_KEYS:
            lst: Log = getattr(s, name)
            rows = self.db.query("SELECT seq, body FROM logs WHERE collection = ? ORDER BY seq", (name,))
            for r in rows:
                lst.append(json.loads(r["body"]))
            self._log_version[name] = lst.version
            if rows:
                lst.dropped = rows[0]["seq"]
                self._log_next[name] = rows[-1]["seq"] + 1
                self._log_floor[name] = rows[0]["seq"]
                for i, ent in enumerate(lst):
                    if i >= len(lst) - TAIL_RECHECK:
                        self._log_tail[name][lst.dropped + i] = _digest(_dump(ent))
            else:
                # empty table: continue after wherever the counter stood so absolute positions never collide
                base = self._kv_get(f"log_base:{name}")
                lst.dropped = self._log_next[name] = self._log_floor[name] = base or 0
        for key, attr in (("considered", "considered"), ("settings", "settings")):
            body = self._kv_get(key)
            if body is not None:
                setattr(s, attr, set(body) if key == "considered" else body)
                self._kv_seen[key] = _digest(_dump(sorted(body) if key == "considered" else body))
        return s.upgrade()

    def _kv_get(self, key: str):
        row = self.db.query_one("SELECT body FROM kv WHERE key = ?", (key,))
        return json.loads(row["body"]) if row else None

    def _freezable(self, name: str, ent, now: int) -> bool:
        """Records that can no longer change are never serialised again — this is what keeps a year-old marketplace cheap to persist."""
        if not isinstance(ent, dict):
            return name == "idempotency"
        if name == "idempotency":
            return True
        if name == "auctions":
            return ent.get("status") in TERMINAL_AUCTION
        if name == "matches":
            return ent.get("status") in FINAL_MATCH and not ent.get("dispute_open") and now - ent.get("updated_at", now) > MATCH_FREEZE_MS
        if name == "triggers":
            return ent.get("status") in DEAD_TRIGGER
        if name == "approvals":
            return ent.get("status") not in ("PENDING",)
        return False

    def _maybe_freeze(self, name: str, eid: str, ent) -> None:
        if self._freezable(name, ent, self.now()):
            self._frozen[name].add(eid)

    # ------------------------------------------------------------------ flush
    def names(self) -> list[str]:
        return MAP_KEYS + LIST_KEYS + ["kv"]

    def collect(self, store: Store, name: str, *, force: bool = False) -> dict:
        """Diff one collection against what was last written. Call on the event-loop thread; pure CPU, no I/O."""
        op: dict = {"name": name, "upserts": [], "deletes": [], "seen": {}, "freeze": []}
        now = self.now()
        if name in SLOW:
            if not force and now - self._checked_at.get(name, -10**18) < SLOW[name]:
                return op  # unchanged since we last looked, as far as this slow collection is concerned
            self._checked_at[name] = now
        if name in MAP_KEYS:
            cur = getattr(store, name)
            seen, frozen = self._seen[name], self._frozen[name]
            for coll, eid in [t for t in store.thawed if t[0] == name]:
                frozen.discard(eid)  # edited on purpose: look at it again (its digest will differ, so it is rewritten)
                store.thawed.discard((coll, eid))
            for eid, ent in cur.items():
                if eid in frozen:
                    continue
                body = _dump(ent)
                d = _digest(body)
                if seen.get(eid) != d:
                    op["upserts"].append((eid, body))
                    op["seen"][eid] = d
                if self._freezable(name, ent, now):
                    op["freeze"].append(eid)
            op["deletes"] = [eid for eid in seen if eid not in cur]
        elif name in LIST_KEYS:
            lst = getattr(store, name)
            base = getattr(lst, "dropped", 0)
            version = getattr(lst, "version", 0)
            if version != self._log_version[name] or base + len(lst) < self._log_next[name]:
                op["rewrite"] = True  # something other than append / front-trim happened: replace the whole durable log
                op["rows"] = [(base + i, _dump(lst[i])) for i in range(len(lst))]
                op.update(next=base + len(lst), floor=base, version=version, upserts=[],
                          tail={base + i: _digest(body) for i, (_, body) in enumerate(op["rows"]) if i >= len(lst) - TAIL_RECHECK})
                return op
            op["version"] = version
            nxt = self._log_next[name]
            start = max(nxt - base, 0)
            op["rows"] = [(base + i, _dump(lst[i])) for i in range(start, len(lst))]
            op["next"] = base + len(lst)
            tail = {}
            for i in range(max(len(lst) - TAIL_RECHECK, 0), min(start, len(lst))):
                body = _dump(lst[i])
                d = _digest(body)
                if self._log_tail[name].get(base + i) != d:
                    op["upserts"].append((base + i, body))
                tail[base + i] = d
            for seq, body in op["rows"]:
                if seq >= op["next"] - TAIL_RECHECK:
                    tail[seq] = _digest(body)
            op["tail"] = tail
            op["floor"] = base
        else:  # kv
            op["kv"] = {}
            for key, val in (("considered", sorted(store.considered)), ("settings", store.settings)):
                body = _dump(val)
                d = _digest(body)
                if self._kv_seen.get(key) != d:
                    op["kv"][key] = (body, d)
        return op

    def apply(self, ops: list[dict]) -> None:
        """Write collected diffs in one transaction, then advance the caches (only after the commit succeeded)."""
        now = self.now()
        for op in ops:
            self._apply_one(op, now)  # one short transaction per collection: the shared database lock is never held for the whole flush

    def _apply_one(self, op: dict, now: int) -> None:
        ops = [op]
        with self.db.tx() as c:
            for op in ops:
                name = op["name"]
                if name in MAP_KEYS:
                    for eid, body in op["upserts"]:
                        c.execute("INSERT INTO docs(collection, id, body, updated_at) VALUES (?,?,?,?) "
                                  "ON CONFLICT (collection, id) DO UPDATE SET body = excluded.body, updated_at = excluded.updated_at", (name, eid, body, now))
                    for eid in op["deletes"]:
                        c.execute("DELETE FROM docs WHERE collection = ? AND id = ?", (name, eid))
                elif name in LIST_KEYS:
                    if op.get("rewrite"):
                        c.execute("DELETE FROM logs WHERE collection = ?", (name,))
                    for seq, body in op["rows"]:
                        c.execute("INSERT INTO logs(collection, seq, body) VALUES (?,?,?) ON CONFLICT (collection, seq) DO UPDATE SET body = excluded.body", (name, seq, body))
                    for seq, body in op["upserts"]:
                        c.execute("UPDATE logs SET body = ? WHERE collection = ? AND seq = ?", (body, name, seq))
                    if op["floor"] > self._log_floor[name]:
                        c.execute("DELETE FROM logs WHERE collection = ? AND seq < ?", (name, op["floor"]))
                        c.execute("INSERT INTO kv(key, body) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET body = excluded.body", (f"log_base:{name}", str(op["floor"])))
                else:
                    for key, (body, _d) in op["kv"].items():
                        c.execute("INSERT INTO kv(key, body) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET body = excluded.body", (key, body))
        for op in ops:
            name = op["name"]
            if name in MAP_KEYS:
                self._seen[name].update(op["seen"])
                for eid in op["deletes"]:
                    self._seen[name].pop(eid, None)
                    self._frozen[name].discard(eid)
                self._frozen[name].update(op["freeze"])
            elif name in LIST_KEYS:
                if op.get("rewrite"):
                    self._log_next[name], self._log_floor[name] = op["next"], op["floor"]
                    self._log_tail[name] = dict(op["tail"])
                else:
                    self._log_next[name] = max(self._log_next[name], op["next"])
                    self._log_floor[name] = max(self._log_floor[name], op["floor"])
                    self._log_tail[name] = {k: v for k, v in {**self._log_tail[name], **op["tail"]}.items() if k >= op["next"] - TAIL_RECHECK}
                self._log_version[name] = op["version"]
            else:
                for key, (_b, d) in op["kv"].items():
                    self._kv_seen[key] = d

    def flush_all(self, store: Store) -> None:
        """Synchronous full flush (startup import, shutdown, tests)."""
        self.apply([self.collect(store, n, force=True) for n in self.names()])

    # ------------------------------------------------------------------ legacy import
    def import_legacy_json(self, path: str | os.PathLike) -> Store | None:
        """One-time migration of the old ``state.json`` snapshot into the database (file renamed afterwards, never deleted)."""
        p = Path(path)
        if not p.exists() or not self.is_empty():
            return None
        store = Store.load(p)
        self.flush_all(store)
        p.rename(p.with_name(p.name + ".imported"))
        log.warning("imported legacy snapshot %s into the database (kept as %s.imported)", p, p.name)
        return store
