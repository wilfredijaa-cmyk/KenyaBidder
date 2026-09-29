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
    assert len(list(tmp_path.glob("live.duckdb.before-restore-*"))) == 1
    (tmp_path / "live.duckdb.wal").write_text("stale log")
    restore_backup(tmp_path / "bk" / files[0]["name"], tmp_path / "live.duckdb")  # a second restore keeps BOTH safety copies and drops the stale WAL
    assert not (tmp_path / "live.duckdb.wal").exists() and list(tmp_path.glob("live.duckdb.wal.before-restore-*"))


async def test_readiness_and_metrics(tmp_path):
    import json
    from kenyabidder import metrics
    rt = Runtime(data_file=str(tmp_path / "state.json"))
    ok, checks = metrics.readiness(rt)
    assert ok and checks == {"database": True}  # loops not started yet: nothing to judge
    rt.start()
    await asyncio.sleep(0.2)
    await rt.flush()
    ok, checks = metrics.readiness(rt)
    assert ok and checks["tick_loop"] and checks["state_flush"]
    rt.app.agents.create_user(name="Wanjiru", password="password123", phone="+254711000001")
    text = metrics.render(rt)
    assert "kenyabidder_up 1" in text and "kenyabidder_users 1" in text and "kenyabidder_ready 1" in text
    assert 'kenyabidder_check_ok{check="database"} 1' in text and "# TYPE kenyabidder_auctions gauge" in text
    rt.last_tick_at -= 60  # a hung tick loop must flip readiness
    assert metrics.readiness(rt)[0] is False and "kenyabidder_ready 0" in metrics.render(rt)
    rt.last_tick_at = __import__("time").time()
    rt.flush_errors_streak = 3
    assert metrics.readiness(rt)[1]["state_flush"] is False
    await rt.stop()
    line = metrics.JsonFormatter().format(__import__("logging").LogRecord("x", 20, "f", 1, "hello %s", ("w",), None))
    assert json.loads(line)["msg"] == "hello w"


async def test_losing_the_writer_lease_stops_writes_and_flips_readiness(tmp_path):
    import json, time
    from kenyabidder import metrics
    rt = Runtime(data_file=str(tmp_path / "state.json"))
    rt.LEASE_RENEW_EVERY = 1
    rt.start()
    await asyncio.sleep(0.1)
    # another process force-takes the lease (e.g. an operator ran a second instance with KENYABIDDER_FORCE_LEASE=1)
    rt.db.execute("UPDATE kv SET body = ? WHERE key = 'writer_lease'", (json.dumps({"owner": "someone-else", "expires": int(time.time() * 1000) + 60_000}),))
    for _ in range(30):
        if rt.lost_lease:
            break
        await asyncio.sleep(0.2)
    assert rt.lost_lease
    ok, checks = metrics.readiness(rt)
    assert not ok and checks["writer_lease"] is False
    before = rt.db.scalar("SELECT count(*) FROM docs")
    rt.app.agents.create_user(name="Late", password="password123")
    await asyncio.sleep(1.5)
    assert rt.db.scalar("SELECT count(*) FROM docs") == before  # nothing written after the lease was lost
    await rt.stop()
