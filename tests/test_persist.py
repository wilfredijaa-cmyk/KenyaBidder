"""Durable state: everything the engine keeps in memory survives a restart, and only what changed is written."""
import pytest

from kenyabidder.db import open_database
from kenyabidder.persist import StateLocked, StatePersistence
from kenyabidder.store import Store


@pytest.fixture
def db(tmp_path):
    d = open_database(f"duckdb://{tmp_path}/s.duckdb")
    yield d
    d.close()


def roundtrip(db, store, clock=None):
    p = StatePersistence(db, clock)
    p.flush_all(store)
    return StatePersistence(db, clock).load()


def test_full_roundtrip_of_maps_logs_and_kv(db):
    s = Store()
    s.users["u1"] = {"id": "u1", "name": "Wanjiru", "n": 5}
    s.auctions["a1"] = {"auction_id": "a1", "status": "ACTIVE", "bids": [{"amount": 10}]}
    s.audit.extend({"i": i} for i in range(5))
    s.considered.update({"x", "y"})
    s.settings["mcp_key"] = "k"
    s.upgrade()  # loading always brings older records up to the current shape
    back = roundtrip(db, s)
    assert back.users == s.users and back.auctions == s.auctions and list(back.audit) == list(s.audit)
    assert back.considered == {"x", "y"} and back.settings == {"mcp_key": "k"}


def test_flush_writes_only_changes_and_freezes_settled_auctions(db):
    s = Store()
    s.auctions["a1"] = {"auction_id": "a1", "status": "ACTIVE", "n": 1}
    s.auctions["a2"] = {"auction_id": "a2", "status": "SETTLED", "n": 1}
    p = StatePersistence(db)
    p.flush_all(s)
    assert p.collect(s, "auctions")["upserts"] == []  # nothing changed
    s.auctions["a1"]["n"] = 2
    assert [e for e, _ in p.collect(s, "auctions")["upserts"]] == ["a1"]
    s.auctions["a2"]["n"] = 99  # settled lots are frozen: never re-examined
    p.flush_all(s)
    assert Store().auctions == {} and StatePersistence(db).load().auctions["a2"]["n"] == 1


def test_deletions_and_log_trimming_stay_aligned_across_restarts(db):
    s = Store()
    s.users.update({"a": {"v": 1}, "b": {"v": 2}})
    s.notifications.extend({"i": i} for i in range(10))
    p = StatePersistence(db)
    p.flush_all(s)
    del s.users["a"]
    del s.notifications[:4]  # trimmed exactly like channels.py does
    s.notifications.append({"i": 10})
    p.flush_all(s)
    back = StatePersistence(db).load()
    assert list(back.users) == ["b"]
    assert [n["i"] for n in back.notifications] == list(range(4, 11))
    back.notifications.append({"i": 11})  # a loaded store keeps appending at the right absolute position
    p2 = StatePersistence(db)
    again = p2.load()
    again.notifications.append({"i": 11})
    p2.flush_all(again)
    assert [n["i"] for n in StatePersistence(db).load().notifications] == list(range(4, 12))


def test_mutated_recent_log_rows_are_rewritten(db):
    s = Store()
    s.outbox.append({"id": 1, "sent": False})
    p = StatePersistence(db)
    p.flush_all(s)
    s.outbox[0]["sent"] = True
    p.flush_all(s)
    assert StatePersistence(db).load().outbox[0]["sent"] is True


def test_emptied_log_continues_numbering(db):
    s = Store()
    s.reveal_log.extend([{"i": 1}, {"i": 2}])
    p = StatePersistence(db)
    p.flush_all(s)
    del s.reveal_log[:]
    p.flush_all(s)
    back = StatePersistence(db).load()
    assert list(back.reveal_log) == [] and back.reveal_log.dropped == 2
    back.reveal_log.append({"i": 3})
    p2 = StatePersistence(db)
    st = p2.load()
    st.reveal_log.append({"i": 3})
    p2.flush_all(st)
    assert StatePersistence(db).load().reveal_log[0] == {"i": 3}


def test_single_writer_lease(db):
    from kenyabidder.clock import FakeClock
    clock = FakeClock(1_000)
    a, b = StatePersistence(db, clock), StatePersistence(db, clock)
    a.acquire_lease()
    a.renew_lease()
    with pytest.raises(StateLocked):
        b.acquire_lease()
    clock.advance(60_000)  # a crashed: its lease expires
    b.acquire_lease()
    b.release_lease()
    a.acquire_lease()  # free again


def test_legacy_snapshot_is_imported_once(db, tmp_path):
    legacy = tmp_path / "state.json"
    s = Store()
    s.users["u"] = {"id": "u"}
    s.save(legacy)
    p = StatePersistence(db)
    got = p.import_legacy_json(legacy)
    assert got.users == {"u": {"id": "u", "session_version": 0}} and not legacy.exists() and (tmp_path / "state.json.imported").exists()
    assert StatePersistence(db).load().users == {"u": {"id": "u", "session_version": 0}}
    assert p.import_legacy_json(legacy) is None


def test_any_non_append_change_to_a_log_is_made_durable(db):
    """Mid-list removal, slice assignment, insert and sort all rewrite the durable copy — nothing stale can come back after a restart."""
    s = Store()
    s.notifications.extend({"i": i, "u": "a" if i % 2 else "b"} for i in range(300))
    p = StatePersistence(db)
    p.flush_all(s)
    s.notifications[:] = [n for n in s.notifications if n["u"] == "a"]  # what privacy deletion does
    p.flush_all(s)
    assert [n["i"] for n in StatePersistence(db).load().notifications] == [i for i in range(300) if i % 2]
    del s.notifications[10]  # removal in the middle
    s.notifications.insert(5, {"i": -1, "u": "a"})
    p.flush_all(s)
    assert list(StatePersistence(db).load().notifications) == list(s.notifications)
    s.notifications.sort(key=lambda n: -n["i"])
    p.flush_all(s)
    assert list(StatePersistence(db).load().notifications) == list(s.notifications)
    del s.notifications[-3:]  # shrinking from the END
    p.flush_all(s)
    assert list(StatePersistence(db).load().notifications) == list(s.notifications)
    s.notifications.clear()
    p.flush_all(s)
    assert list(StatePersistence(db).load().notifications) == []


def test_records_written_before_reverse_auctions_are_upgraded_on_load(db):
    s = Store()
    s.auctions["old"] = {"auction_id": "old", "status": "ACTIVE", "seller_agent_id": "s1", "auction_type": "ENGLISH", "bids": []}  # no poster/direction/verified_only
    StatePersistence(db).flush_all(s)
    a = StatePersistence(db).load().auctions["old"]
    assert a["poster_agent_id"] == "s1" and a["direction"] == "FORWARD" and a["verified_only"] is False
