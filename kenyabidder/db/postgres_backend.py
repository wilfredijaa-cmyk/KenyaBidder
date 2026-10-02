"""PostgreSQL backend (``pip install "psycopg[binary]"``).

Run the whole test-suite against a server with ``KENYABIDDER_TEST_DATABASE_URL=postgresql://…`` (every test gets its own
throw-away schema). CI does this against a PostgreSQL service container.
"""
from __future__ import annotations

from typing import Sequence

from .base import Database, DatabaseError, IntegrityError

try:  # optional dependency
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover
    psycopg = None


def translate(sql: str) -> str:
    """``?`` placeholders → psycopg's ``%s`` (literal ``%`` must be doubled first, e.g. ``LIKE 'signup:%'``)."""
    return sql.replace("%", "%%").replace("?", "%s")


class PostgresDatabase(Database):
    dialect = "postgres"

    def __init__(self, url: str, schema: str | None = None):
        super().__init__()
        if psycopg is None:
            raise DatabaseError('PostgreSQL support needs `pip install "psycopg[binary]"`')
        self.url = url
        if schema and not schema.replace("_", "").isalnum():
            raise DatabaseError("invalid schema name")
        self.schema = schema
        self._conn = self._connect()

    def _connect(self):
        try:
            conn = psycopg.connect(self.url, autocommit=True, row_factory=dict_row, connect_timeout=10)
        except psycopg.Error as e:
            raise DatabaseError(f"cannot connect to PostgreSQL: {e}") from e
        if self.schema:  # isolate everything in one schema (tests; multi-tenant hosting)
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
            conn.execute(f'SET search_path TO "{self.schema}"')
        return conn

    def _ensure(self) -> None:
        """A server restart, failover or idle-timeout kills the connection: open a new one instead of failing forever (never mid-transaction)."""
        if self._tx_depth:
            return
        if self._conn.closed or self._conn.broken:
            self._conn = self._connect()

    def drop_schema(self) -> None:
        if self.schema:
            with self._lock:
                self._conn.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')

    @staticmethod
    def _wrap(e: Exception) -> Exception:
        if isinstance(e, (psycopg.errors.UniqueViolation, psycopg.errors.CheckViolation, psycopg.errors.NotNullViolation,
                          psycopg.errors.ForeignKeyViolation)):
            return IntegrityError(str(e))
        return DatabaseError(str(e))

    def _query(self, sql: str, params: Sequence) -> list[dict]:
        self._ensure()
        for attempt in (0, 1):
            try:
                with self._conn.cursor() as cur:
                    cur.execute(translate(sql), list(params))
                    return list(cur.fetchall()) if cur.description else []
            except psycopg.OperationalError as e:
                if attempt or self._tx_depth:
                    raise self._wrap(e) from e
                self._conn = self._connect()  # a read is safe to repeat on a fresh connection
            except psycopg.Error as e:
                raise self._wrap(e) from e
        return []

    def _execute(self, sql: str, params: Sequence) -> int:
        self._ensure()  # writes are not repeated blindly (they may have committed); the next call finds a healthy connection
        try:
            with self._conn.cursor() as cur:
                cur.execute(translate(sql), list(params))
                return max(cur.rowcount, 0)
        except psycopg.Error as e:
            raise self._wrap(e) from e

    def _begin(self) -> None:
        self._ensure()
        try:
            self._conn.execute("BEGIN")
        except psycopg.OperationalError:
            self._conn = self._connect()
            self._conn.execute("BEGIN")

    def _commit(self) -> None:
        self._conn.execute("COMMIT")

    def _rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except psycopg.Error:
            pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()
