"""Regressions from the seventh review round: what the marketplace costs once it has a history, and three smaller defects.

The engine already indexed live auctions and triggers; matches were still walked in full on every 50 ms tick, on every
reputation recompute and — worst — on every one-second state flush, for forty days after each deal closed.
"""
import asyncio
import time

import pytest

from kenyabidder.errors import AppError
from kenyabidder.persist import StatePersistence


def _history(env, n, *, finished=True):
    """n deals between other people, the way a year of trading leaves them behind."""
    _, s = env.seller()
    _, b = env.bidder()
    a = env.english(s["agent_id"])
    env.engine.submit_bid(auction_id=a["auction_id"], agent_id=b["agent_id"], amount=1000)
    env.clock.advance(61_000)
    env.engine.tick()
    m = list(env.store.matches.values())[0]
    for i in range(n):
        env.store.matches[f"m-{i}"] = dict(m, match_id=f"m-{i}", status="COMPLETED" if finished else "PROPOSED",
                                           updated_at=env.clock.now(), seller_agent_id=f"s-{i}", buyer_agent_id=f"b-{i}")
    return s, b, m


def test_the_tick_loop_costs_what_is_open_not_what_is_history(env):
    _history(env, 20_000)
    env.app.tick()  # first pass builds the index
    t0 = time.perf_counter()
    for _ in range(20):
        env.app.tick()
    per_tick = (time.perf_counter() - t0) / 20
    assert per_tick < 0.01, f"{per_tick*1000:.0f} ms per tick, and the loop runs it every 50 ms"


def test_reputation_reads_only_that_agents_own_deals(env):
    _, _, m = _history(env, 20_000)
    seller = m["seller_agent_id"]
    env.app.recompute_reputation(seller)  # builds the index
    t0 = time.perf_counter()
    for _ in range(50):
        env.app.recompute_reputation(seller)
    per_call = (time.perf_counter() - t0) / 50
    assert per_call < 0.002, f"{per_call*1000:.1f} ms to score one agent with one deal among 20,000"
    assert env.store.agents[seller]["reputation"]["completed_matches"] == 0  # …and none of the 20,000 other people's deals leaked in


def test_a_finished_deal_is_not_re_serialised_on_every_flush(env, make_env):
    from conftest import fresh_database
    _history(env, 5_000)
    p = StatePersistence(fresh_database(), env.clock)
    p.flush_all(env.store)
    env.clock.advance(120_000)
    p.flush_all(env.store)  # this pass freezes the settled deals
    t0 = time.perf_counter()
    ops = [p.collect(env.store, n) for n in p.names()]
    diff = time.perf_counter() - t0
    assert not any(op["upserts"] for op in ops), "a settled marketplace should have nothing left to write"
    assert diff < 0.05, f"{diff*1000:.0f} ms of diffing per second with 5,000 finished deals"


def test_a_dispute_on_a_frozen_deal_still_reaches_the_database(env):
    from conftest import fresh_database
    us, seller = env.seller(name="Amina")
    ub, buyer = env.bidder(name="David", phone="+254700000009")
    a = env.english(seller["agent_id"])
    env.engine.submit_bid(auction_id=a["auction_id"], agent_id=buyer["agent_id"], amount=1000)
    env.clock.advance(61_000)
    env.engine.tick()
    m = list(env.store.matches.values())[0]
    env.matches.confirm_match(m["match_id"], seller["agent_id"])
    env.matches.confirm_match(m["match_id"], buyer["agent_id"])
    env.matches.report_outcome(m["match_id"], seller["agent_id"], "COMPLETED")
    env.matches.report_outcome(m["match_id"], buyer["agent_id"], "COMPLETED")
    assert m["status"] == "COMPLETED"
    p = StatePersistence(fresh_database(), env.clock)
    p.flush_all(env.store)
    env.clock.advance(120_000)
    p.flush_all(env.store)
    assert m["match_id"] in p._frozen["matches"]  # cheap to keep…
    env.trust.open_dispute(buyer["agent_id"], m["match_id"], "NOT_DELIVERED", "the maize never arrived")
    d = env.trust.dispute_for_match(m["match_id"])
    env.clock.advance(8 * 24 * 3600_000)
    env.trust.rule({"id": None, "name": "root"}, d["id"], "SELLER_AT_FAULT", "no delivery proof")
    p.flush_all(env.store)
    loaded = p.load().matches[m["match_id"]]  # …and still rewritten when a ruling lands
    assert loaded["status"] == "FELL_THROUGH" and loaded["fault_agent_ids"] == [seller["agent_id"]]
    assert loaded["dispute_open"] is False


def test_matches_for_an_agent_survive_a_demo_purge(env):
    _, _, m = _history(env, 100)
    seller = m["seller_agent_id"]
    assert len(env.matches.matches_for(seller)) == 1
    del env.store.matches["m-0"]
    assert len(env.matches.matches_for(seller)) == 1  # the index notices records that vanished
    env.store.matches.clear()
    assert env.matches.matches_for(seller) == []


def test_unconfirmed_matches_still_lapse(env):
    """The index must not hide the work expire_stale exists to do."""
    us, seller = env.seller()
    ub, buyer = env.bidder()
    a = env.english(seller["agent_id"])
    env.engine.submit_bid(auction_id=a["auction_id"], agent_id=buyer["agent_id"], amount=1000)
    env.clock.advance(61_000)
    env.engine.tick()
    m = list(env.store.matches.values())[0]
    env.matches.confirm_match(m["match_id"], seller["agent_id"])
    env.clock.advance(25 * 3600_000)
    env.app.tick()
    assert m["status"] == "NO_RESPONSE" and m["fault_agent_ids"] == [buyer["agent_id"]]


def test_a_transient_failure_does_not_retire_an_auction_for_an_agent(env):
    """`considered` is set before the strategy runs so one auction is planned once; a failure must release it again."""
    _, s = env.seller()
    _, b = env.bidder(ceiling=100_000, memory={"watch": {"category": "electronics"}, "auto_bid": True})
    a = env.english(s["agent_id"], duration_ms=600_000)
    real, calls = env.execution.register_trigger, {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database hiccup")
        return real(**kw)

    env.execution.register_trigger = flaky
    with pytest.raises(RuntimeError):
        asyncio.run(env.orchestrator.consider(b["agent_id"], a["auction_id"]))
    assert f'{b["agent_id"]}:{a["auction_id"]}' not in env.store.considered
    assert asyncio.run(env.orchestrator.consider(b["agent_id"], a["auction_id"]))["status"] == "PLANNED"


def test_knowledge_search_indexes_a_document_once_not_once_per_search(env):
    kb = env.kb.create("Handbook")
    para = " ".join(["maize delivery nairobi wholesale pallet invoice mombasa tonne"] * 15)
    for d in range(8):
        env.kb.add_text(kb["id"], f"doc {d}", "\n\n".join([para] * 50))
    env.kb.search("maize delivery", [kb["id"]])  # first search indexes the corpus
    t0 = time.perf_counter()
    for _ in range(10):
        hits = env.kb.search("maize delivery nairobi", [kb["id"]])
    per_search = (time.perf_counter() - t0) / 10
    assert hits and per_search < 0.05, f"{per_search*1000:.0f} ms per search over a 0.5 MB knowledge base"
    doc_id = next(iter(kb["docs"]))
    env.kb.remove_doc(kb["id"], doc_id)
    assert doc_id not in env.kb._tok_cache  # a removed document leaves nothing behind
    assert all(h["doc_id"] != doc_id for h in env.kb.search("maize delivery", [kb["id"]]))  # and drops out of the results


def test_platform_kpis_are_computed_once_per_window_for_every_viewer(env):
    _, s = env.seller()
    _, b = env.bidder()
    a = env.english(s["agent_id"])
    env.app.manual_bid(b["agent_id"], a["auction_id"], 1000)
    first = env.app.kpis()
    env.app.manual_bid(b["agent_id"], a["auction_id"], 1200)
    assert env.app.kpis() is first                       # a second watcher re-uses the figures
    assert env.app.kpis(max_age_ms=0) is not first       # and the window can always be bypassed
    assert env.app.kpis(max_age_ms=0)["system_reliability"]["audit_entries"] == len(env.store.audit)


def test_a_password_reset_clears_the_re_authentication_lockout(env):
    u = env.agents.create_user(name="Wanjiru", password="password123")
    for _ in range(5):
        with pytest.raises(AppError):
            env.agents.reauth(u["id"], "wrong-one")
    with pytest.raises(AppError) as e:
        env.agents.reauth(u["id"], "password123")
    assert e.value.code == "TOO_MANY_ATTEMPTS"
    env.agents.set_password(u["id"], "brand-new-password")
    assert env.agents.reauth(u["id"], "brand-new-password")  # the counter was about the old password
