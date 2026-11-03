import os

import pytest

from kenyabidder.db import open_database

URL = os.environ.get("KENYABIDDER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="needs PostgreSQL")


def test_database_reconnects_after_the_connection_is_killed():
    db = open_database(URL)
    try:
        assert db.scalar("SELECT 1") == 1
        db._conn.close()  # what a server restart / failover looks like from here
        assert db.healthy()
        assert db.scalar("SELECT 2") == 2
        with db.tx() as t:
            assert t.scalar("SELECT 3") == 3
    finally:
        db.close()
