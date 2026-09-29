"""Restart survival, single-writer protection, legacy imports and backups, through the real Runtime."""
import asyncio

import pytest

from kenyabidder.backups import BackupService, restore_backup
from kenyabidder.clock import FakeClock
from kenyabidder.db import open_database
from kenyabidder.persist import StateLocked
from kenyabidder.runtime import Runtime


async def test_state_and_money_survive_a_restart(tmp_path):
    f = str(tmp_path / "state.json")
    rt = Runtime(data_file=f)
    rt.start()
    a = rt.app
    uid = a.agents.create_user(name="Wanjiru", password="password123", phone="+254700000002")["id"]
    a.wallet.credit(uid, "L1", 500, "TOPUP", ref="o1")
    await rt.flush()
    await rt.stop()

    rt2 = Runtime(data_file=f)
    assert [u["name"] for u in rt2.store.users.values()] == ["Wanjiru"]
    assert rt2.app.wallet.balance(uid, "L1") == 500
    await rt2.stop()


async def test_second_process_is_refused(tmp_path):
    f = str(tmp_path / "state.json")
    rt = Runtime(data_file=f)
    with pytest.raises(Exception):  # DuckDB itself refuses a second opener; PostgreSQL relies on the lease
        Runtime(data_file=f)
    await rt.stop()


def test_lease_blocks_a_second_writer_on_a_shared_database(tmp_path):
    from kenyabidder.persist import StatePersistence
    db = open_database(f"duckdb://{tmp_path}/x.duckdb")
    a, b = StatePersistence(db), StatePersistence(db)
    a.acquire_lease()
    with pytest.raises(StateLocked):
        b.acquire_lease()
    b.acquire_lease(force=True)


def test_backup_is_verified_pruned_and_restorable(tmp_path):
    clock = FakeClock()
    db = open_database(f"duckdb://{tmp_path}/live.duckdb")
    db.execute("INSERT INTO balances(user_id, llm_id, balance) VALUES ('u','l',42)")
    svc = BackupService(db, tmp_path / "bk", clock, keep=2, every_hours=1)
    assert svc.due()
    for _ in range(4):
        clock.advance(3_600_000)
        svc.run()
    files = svc.list()
    assert len(files) == 2 and not svc.due()
    db.close()
    restore_backup(tmp_path / "bk" / files[0]["name"], tmp_path / "live.duckdb")
    back = open_database(f"duckdb://{tmp_path}/live.duckdb")
    assert back.scalar("SELECT balance FROM balances") == 42
    assert (tmp_path / "live.duckdb.before-restore").exists()
