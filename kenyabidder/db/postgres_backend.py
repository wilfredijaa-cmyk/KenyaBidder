"""PostgreSQL backend (``pip install "psycopg[binary]"``).

NOTE: written against psycopg 3's documented API and the portable SQL subset, but **not exercised in this repository's
test-suite** (no Postgres server here). Run the whole test-suite with ``KENYABIDDER_TEST_DATABASE_URL`` pointing at a
scratch database before relying on it: ``tests/test_db.py::test_backend_contract`` runs against whatever that URL is.
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

    def __init__(self, url: str):
        super().__init__()
        if psycopg is None:
            raise DatabaseError('PostgreSQL support needs `pip install "psycopg[binary]"`')
        self.url = url
        try:
            self._conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        except psycopg.Error as e:
            raise DatabaseError(f"cannot connect to PostgreSQL: {e}") from e

    @staticmethod
    def _wrap(e: Exception) -> Exception:
        if isinstance(e, (psycopg.errors.UniqueViolation, psycopg.errors.CheckViolation, psycopg.errors.NotNullViolation,
                          psycopg.errors.ForeignKeyViolation)):
            return IntegrityError(str(e))
        return DatabaseError(str(e))

    def _query(self, sql: str, params: Sequence) -> list[dict]:
        try:
            with self._conn.cursor() as cur:
                cur.execute(translate(sql), list(params))
                return list(cur.fetchall()) if cur.description else []
        except psycopg.Error as e:
            raise self._wrap(e) from e

    def _execute(self, sql: str, params: Sequence) -> int:
        try:
            with self._conn.cursor() as cur:
                cur.execute(translate(sql), list(params))
                return max(cur.rowcount, 0)
        except psycopg.Error as e:
            raise self._wrap(e) from e

    def _begin(self) -> None:
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
