from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Sequence

import duckdb

from .base import Database, DatabaseError, IntegrityError

_DML = ("INSERT", "UPDATE", "DELETE")


class DuckDatabase(Database):
    """Embedded, single-process DuckDB (ACID, ``CHECK``/``UNIQUE`` enforced). One connection, serialised by a lock."""

    dialect = "duckdb"

    def __init__(self, path: str | os.PathLike = ":memory:"):
        super().__init__()
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = duckdb.connect(self.path)
        except duckdb.Error as e:
            raise DatabaseError(f"cannot open {self.path}: {e} (is another KenyaBidder process using it?)") from e
        if self.path != ":memory:":
            try:
                os.chmod(self.path, 0o600)  # holds balances, payment references and (encrypted-at-rest is up to the host) user data
            except OSError:
                pass

    @staticmethod
    def _wrap(e: Exception) -> Exception:
        if isinstance(e, duckdb.ConstraintException):
            return IntegrityError(str(e))
        return DatabaseError(str(e))

    def _run(self, sql: str, params: Sequence):
        try:
            return self._conn.execute(sql, list(params))
        except duckdb.Error as e:
            raise self._wrap(e) from e

    def _query(self, sql: str, params: Sequence) -> list[dict]:
        cur = self._run(sql, params)
        if cur.description is None:
            return []
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def _execute(self, sql: str, params: Sequence) -> int:
        cur = self._run(sql, params)
        head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if head in _DML and " RETURNING " not in sql.upper() and cur.description is not None:
            row = cur.fetchone()  # DuckDB reports affected rows as a one-row result
            return int(row[0]) if row else 0
        return 0

    def _begin(self) -> None:
        self._run("BEGIN TRANSACTION", ())

    def _commit(self) -> None:
        self._run("COMMIT", ())

    def _rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except duckdb.Error:
            pass

    def checkpoint(self) -> None:
        if self.path != ":memory:":
            with self._lock:
                self._run("CHECKPOINT", ())

    def backup(self, dest: str) -> None:
        """Consistent copy: checkpoint, then copy the file while holding the lock (single writer ⇒ nothing can change under us)."""
        if self.path == ":memory:":
            raise DatabaseError("an in-memory database has nothing to back up")
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._run("CHECKPOINT", ())
            tmp = f"{dest}.partial"
            try:
                shutil.copy2(self.path, tmp)
                os.chmod(tmp, 0o600)
                os.replace(tmp, dest)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)  # a full disk must not leave a full-size orphan behind
                raise

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except duckdb.Error:
                pass
