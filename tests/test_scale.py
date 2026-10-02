"""The marketplace keeps its whole history in memory; the hot paths must only pay for what is still running."""
import time

import pytest

from kenyabidder.errors import AppError


def _history(env, seller, n):
    """n finished auctions, injected the way a database load would."""
    base = env.english(seller["agent_id"])
    raw = env.engine.get_auction(base["auction_id"])
    del env.store.auctions[base["auction_id"]]  # only its shape is wanted
    for i in range(n):
        a = dict(raw, auction_id=f"old-{i}", status="SETTLED", bids=[], created_at=1, closed_at=2,
                 result={"outcome": "NO_SALE", "winner_agent_id": None, "price": None})
        env.store.auctions[a["auction_id"]] = a


def test_live_index_follows_creation_close_and_withdrawal(env):
    _, s = env.seller()
    a = env.english(s["agent_id"])
    b = env.english(s["agent_id"], duration_ms=600_000)
    ids = lambda: {x["auction_id"] for x in env.engine.live_auctions()}
    assert ids() == {a["auction_id"], b["auction_id"]}
    env.clock.advance(61_000)
    env.engine.tick()
    assert ids() == {b["auction_id"]}
    env.engine.withdraw_listing(listing_id=b["auction_id"], seller_agent_id=s["agent_id"])
    assert ids() == set()


def test_live_index_survives_out_of_band_changes(env):
    """Loading a snapshot / deleting demo data changes the store behind the engine's back — the index must notice."""
    _, s = env.seller()
    env.english(s["agent_id"])
    _history(env, s, 5)
    assert len(env.engine.live_auctions()) == 1
    for i in range(5):
        del env.store.auctions[f"old-{i}"]
    assert len(env.engine.live_auctions()) == 1
    env.store.auctions.clear()
    assert env.engine.live_auctions() == []
    assert env.english(s["agent_id"])["status"] == "ACTIVE"
    assert len(env.engine.live_auctions()) == 1


def test_hot_paths_do_not_walk_the_history(env):
    _, s = env.seller()
    _, b = env.bidder()
    live = env.english(s["agent_id"], duration_ms=3600_000)
    _history(env, s, 20_000)
    env.engine.live_auctions()  # first call builds the index; steady state is what matters
    t0 = time.perf_counter()
    for _ in range(200):
        env.engine.tick()
        env.engine.list_active_auctions()
    assert time.perf_counter() - t0 < 1.0, "tick/list must be O(live), not O(history)"
    t0 = time.perf_counter()
    for _ in range(50):
        env.engine._check_listing_limits(s["agent_id"], env.clock.now(), relist=True)
    assert time.perf_counter() - t0 < 0.5
    assert env.engine.submit_bid(auction_id=live["auction_id"], agent_id=b["agent_id"], amount=1000)["ok"]


def test_listing_rate_limit_counts_only_the_last_minute(make_env):
    env = make_env()
    _, s = env.seller()
    for _ in range(10):
        env.english(s["agent_id"], duration_ms=10 * 3600_000)
    assert len(env.engine._recent[s["agent_id"]]) == 10
    env.clock.advance(61_000)
    env.english(s["agent_id"], duration_ms=10 * 3600_000)
    assert len(env.engine._recent[s["agent_id"]]) == 1  # the old stamps were pruned, not accumulated


def test_trigger_index_tracks_plans_and_ignores_finished_ones(env):
    _, s = env.seller()
    _, b = env.bidder()
    a = env.english(s["agent_id"])
    ex = env.execution
    t1 = ex.register_trigger(agent_id=b["agent_id"], auction_id=a["auction_id"], kind="ENGLISH_INCREMENTAL", params={"max_bid": 3000})
    t2 = ex.register_trigger(agent_id=b["agent_id"], auction_id=a["auction_id"], kind="ENGLISH_INCREMENTAL", params={"max_bid": 4000})
    assert t1["status"] == "CANCELLED"  # superseded through the index
    assert [t["id"] for t in ex._live_for(a["auction_id"])] == [t2["id"]]
    env.clock.advance(61_000)
    env.engine.tick()
    ex.evaluate_all()
    assert ex._live_for(a["auction_id"]) == []
    assert a["auction_id"] not in ex._by_auction  # empty entries are dropped, not kept forever


def test_trigger_index_notices_pruned_and_deleted_plans(env):
    _, s = env.seller()
    _, b = env.bidder()
    a = env.english(s["agent_id"])
    ex = env.execution
    t = ex.register_trigger(agent_id=b["agent_id"], auction_id=a["auction_id"], kind="ENGLISH_INCREMENTAL", params={"max_bid": 3000})
    assert ex._live_for(a["auction_id"])
    del env.store.triggers[t["id"]]
    assert ex._live_for(a["auction_id"]) == []
    ex.evaluate_all()  # must not trip over the vanished plan


def test_browse_shows_what_is_running_plus_recent_history_only(env):
    _, s = env.seller()
    _, b = env.bidder()
    live = env.english(s["agent_id"], duration_ms=3600_000)
    _history(env, s, 400)
    for i in range(400):
        env.store.auctions[f"old-{i}"]["created_at"] = 10 + i
        env.store.auctions[f"old-{i}"]["closed_at"] = 20 + i
    rows = env.engine.browse(b["agent_id"])
    assert len(rows) == 1 + 60  # the live one and the 60 most recently finished, not 400
    assert rows[0]["auction_id"] == live["auction_id"]  # newest first
    assert {r["auction_id"] for r in rows[1:]} == {f"old-{i}" for i in range(340, 400)}


def test_a_listing_that_finishes_moves_from_the_live_set_to_the_recent_list(env):
    _, s = env.seller()
    _, b = env.bidder()
    a = env.english(s["agent_id"])
    assert [r["auction_id"] for r in env.engine.browse(b["agent_id"])] == [a["auction_id"]]
    env.clock.advance(61_000)
    assert [r["auction_id"] for r in env.engine.browse(b["agent_id"])] == [a["auction_id"]]  # now finished, still listed as recent
    assert env.engine.live_auctions() == []


def test_hidden_and_removed_listings_are_only_for_poster_and_admins(env):
    _, s = env.seller()
    _, b = env.bidder()
    a = env.english(s["agent_id"], duration_ms=3600_000)
    raw = env.engine.get_auction(a["auction_id"])
    raw["hidden"] = True  # held for review
    assert env.engine.browse(b["agent_id"]) == []
    assert [r["auction_id"] for r in env.engine.browse(s["agent_id"])] == [a["auction_id"]]
    assert [r["auction_id"] for r in env.engine.browse(b["agent_id"], admin=True)] == [a["auction_id"]]
    with pytest.raises(AppError) as e:
        env.engine.get_auction_detail(a["auction_id"], b["agent_id"])
    assert e.value.code == "AUCTION_NOT_FOUND"
    assert env.engine.get_auction_detail(a["auction_id"], s["agent_id"])["hidden"] is True
    # taken down by the moderators: gone for everyone but the poster and the admins, also after it is no longer "hidden"
    raw.update(hidden=False, status="CANCELLED", result={"outcome": "CANCELLED", "reason": "removed by moderators: counterfeit"})
    assert env.engine.browse(b["agent_id"]) == []
    assert [r["auction_id"] for r in env.engine.browse(s["agent_id"])] == [a["auction_id"]]
    with pytest.raises(AppError):
        env.engine.get_auction_detail(a["auction_id"], b["agent_id"])
    assert env.engine.get_auction_detail(a["auction_id"], admin=True)["status"] == "CANCELLED"


def test_has_posted_is_an_index_not_a_scan(env):
    _, s = env.seller()
    _, s2 = env.seller()
    assert not env.engine.has_posted(s["agent_id"])
    env.english(s["agent_id"])
    assert env.engine.has_posted(s["agent_id"]) and not env.engine.has_posted(s2["agent_id"])
