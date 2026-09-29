"""Database layer. ``open_database("duckdb:///path")`` today; ``open_database("postgresql://…")`` when you outgrow one process."""
from __future__ import annotations

from .base import Database, DatabaseError, IntegrityError, Tx  # noqa: F401
from .schema import migrate  # noqa: F401


def open_database(url: str | None = None) -> Database:
    """``:memory:`` | ``duckdb:///relative-or-absolute/file.duckdb`` | ``postgresql://user:pw@host/db``."""
    url = url or ":memory:"
    if url == ":memory:" or url == "duckdb://:memory:":
        from .duckdb_backend import DuckDatabase
        db: Database = DuckDatabase(":memory:")
    elif url.startswith("duckdb://"):
        from .duckdb_backend import DuckDatabase
        db = DuckDatabase(url[len("duckdb://"):])
    elif url.startswith(("postgresql://", "postgres://")):
        from .postgres_backend import PostgresDatabase
        db = PostgresDatabase(url)
    else:
        raise DatabaseError(f"unsupported database URL {url!r} (use duckdb:///path.duckdb or postgresql://…)")
    migrate(db)
    return db
