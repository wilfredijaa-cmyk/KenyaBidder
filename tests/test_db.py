"""Backend contract tests: DuckDB always; PostgreSQL too when KENYABIDDER_TEST_DATABASE_URL is set."""
import os

import pytest

from kenyabidder.db import DatabaseError, IntegrityError, open_database
from kenyabidder.db.postgres_backend import translate


@pytest.fixture(params=["duckdb"] + (["postgres"] if os.environ.get("KENYABIDDER_TEST_DATABASE_URL") else []))
def db(request, tmp_path):
    if request.param == "duckdb":
        d = open_database(f"duckdb://{tmp_path}/t.duckdb")
    else:
        from conftest import fresh_database
        d = fresh_database()
    yield d
    d.close()


def test_migrations_apply_and_are_idempotent(db):
    from kenyabidder.db.schema import MIGRATIONS, migrate
    migrate(db)
    assert db.scalar("SELECT max(version) FROM schema_version") == MIGRATIONS[-1][0]


def test_tx_rolls_back_and_constraints_normalise(db):
    db.execute("INSERT INTO balances(user_id, llm_id, balance) VALUES (?,?,?)", ("u", "l", 5))
    with pytest.raises(RuntimeError):
        with db.tx() as c:
            c.execute("UPDATE balances SET balance = 9 WHERE user_id = ?", ("u",))
            raise RuntimeError("boom")
    assert db.scalar("SELECT balance FROM balances WHERE user_id = ?", ("u",)) == 5
    with pytest.raises(IntegrityError):
        db.execute("UPDATE balances SET balance = -1 WHERE user_id = ?", ("u",))
    assert db.execute("UPDATE balances SET balance = 6 WHERE user_id = ?", ("u",)) == 1
    assert db.execute("UPDATE balances SET balance = 6 WHERE user_id = ?", ("nobody",)) == 0


def test_backup_is_a_usable_copy(tmp_path):
    d = open_database(f"duckdb://{tmp_path}/a.duckdb")
    d.execute("INSERT INTO balances(user_id, llm_id, balance) VALUES ('u','l',7)")
    dest = tmp_path / "b.duckdb"
    d.backup(str(dest))
    d.close()
    copy = open_database(f"duckdb://{dest}")
    assert copy.scalar("SELECT balance FROM balances") == 7
    copy.close()


def test_url_parsing_and_placeholder_translation():
    assert translate("SELECT '100%' WHERE a = ? AND b = ?") == "SELECT '100%%' WHERE a = %s AND b = %s"
    with pytest.raises(DatabaseError):
        open_database("mysql://x")
