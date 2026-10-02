"""Portable database interface.

Everything above this layer writes ONE SQL dialect — the subset both DuckDB and PostgreSQL accept:
``?`` placeholders, ``BIGINT``/``VARCHAR``, sequences, ``INSERT … ON CONFLICT … DO UPDATE``, ``RETURNING``, plain
(non-partial) unique constraints. Backends translate placeholders and normalise errors; nothing else differs.
"""
from __future__ import annotations

import abc
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Sequence


class DatabaseError(Exception):
    """Any backend failure."""


class IntegrityError(DatabaseError):
    """A UNIQUE / CHECK / NOT NULL / PRIMARY KEY violation (the database refusing to break an invariant)."""


class Tx:
    """A handle inside one atomic transaction. Same API as the database itself."""

    def __init__(self, db: "Database"):
        self._db = db

    def query(self, sql: str, params: Sequence = ()) -> list[dict]:
        return self._db._query(sql, params)

    def query_one(self, sql: str, params: Sequence = ()) -> dict | None:
        rows = self._db._query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence = (), default: Any = None) -> Any:
        r = self._db._query(sql, params)
        return default if not r else next(iter(r[0].values()))

    def execute(self, sql: str, params: Sequence = ()) -> int:
        """Run INSERT/UPDATE/DELETE/DDL; returns the number of rows affected."""
        return self._db._execute(sql, params)


class Database(abc.ABC):
    dialect = "generic"

    def __init__(self):
        self._lock = threading.RLock()
        self._tx_depth = 0

    # -- backend hooks (called with the lock held) --
    @abc.abstractmethod
    def _query(self, sql: str, params: Sequence) -> list[dict]: ...

    @abc.abstractmethod
    def _execute(self, sql: str, params: Sequence) -> int: ...

    @abc.abstractmethod
    def _begin(self) -> None: ...

    @abc.abstractmethod
    def _commit(self) -> None: ...

    @abc.abstractmethod
    def _rollback(self) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    def checkpoint(self) -> None:
        """Flush to durable storage (no-op where the engine already guarantees it)."""

    def backup(self, dest: str) -> None:
        raise NotImplementedError(f"{self.dialect} backups are done with the engine's own tooling")

    # -- public API (thread-safe) --
    def query(self, sql: str, params: Sequence = ()) -> list[dict]:
        with self._lock:
            return self._query(sql, params)

    def query_one(self, sql: str, params: Sequence = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence = (), default: Any = None) -> Any:
        r = self.query(sql, params)
        return default if not r else next(iter(r[0].values()))

    def execute(self, sql: str, params: Sequence = ()) -> int:
        with self._lock:
            return self._execute(sql, params)

    def executemany(self, sql: str, rows: Sequence[Sequence]) -> None:
        with self.tx() as t:
            for r in rows:
                t.execute(sql, r)

    @contextmanager
    def tx(self) -> Iterator[Tx]:
        """One atomic transaction. Re-entrant: an inner ``tx()`` joins the outer one."""
        with self._lock:
            if self._tx_depth:
                self._tx_depth += 1
                try:
                    yield Tx(self)
                finally:
                    self._tx_depth -= 1
                return
            self._begin()
            self._tx_depth = 1
            try:
                yield Tx(self)
            except BaseException:
                self._tx_depth = 0
                self._rollback()
                raise
            else:
                self._tx_depth = 0
                self._commit()

    def healthy(self) -> bool:
        """Health probes must never queue behind a long flush: if the connection is busy right now, it is working, not broken."""
        if not self._lock.acquire(blocking=False):  # called on the event loop: never wait
            return True
        try:
            return next(iter(self._query("SELECT 1", ())[0].values())) == 1
        except Exception:  # noqa: BLE001
            return False
        finally:
            self._lock.release()
