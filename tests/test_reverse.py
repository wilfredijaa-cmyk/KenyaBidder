"""Reverse auctions / RFQs: a buyer posts what it needs and its maximum price; supplier agents bid the price DOWN."""
import pytest

from kenyabidder.errors import AppError

SPEC = {"category": "electronics", "title": "50 phone chargers", "quantity": 50}


def rfq(env, buyer_agent, **over):
    base = dict(buyer_agent_id=buyer_agent["agent_id"], product_spec=SPEC, auction_type="REVERSE_ENGLISH", duration_ms=60_000,
                max_price=10_000, min_decrement=100)
    base.update(over)
    return env.engine.create_rfq(**base)


def supplier(env, floor=0, **kw):
    return env.seller(reserve_floor=floor, **kw)


def quote(env, auction_id, agent, amount, key=None):
    return env.engine.submit_bid(auction_id=auction_id, agent_id=agent["agent_id"], amount=amount, idempotency_key=key)


def test_rfq_creation_rules(env):
    _, buyer = env.bidder(ceiling=20_000)
    _, seller = supplier(env)
    with pytest.raises(AppError) as e:
        rfq(env, buyer, max_price=20_001)
    assert e.value.code == "CEILING_EXCEEDED"
    with pytest.raises(AppError):
        env.engine.create_rfq(buyer_agent_id=seller["agent_id"], product_spec=SPEC, auction_type="REVERSE_ENGLISH", duration_ms=1000, max_price=5)  # suppliers cannot post RFQs
    with pytest.raises(AppError):
        rfq(env, buyer, auction_type="ENGLISH")
    with pytest.raises(AppError):  # and the forward call refuses reverse types
        env.engine.create_listing(seller_agent_id=seller["agent_id"], product_spec=SPEC, auction_type="REVERSE_ENGLISH", duration_ms=1000)
    v = rfq(env, buyer)
    assert v["direction"] == "REVERSE" and v["max_next_bid"] == 10_000 and v["min_next_bid"] is None and v["poster_agent_id"] == buyer["agent_id"]


def test_rfqs_and_listings_are_browsed_separately(env):
    _, buyer = env.bidder()
    _, seller = supplier(env)
    v = rfq(env, buyer)
    env.english(seller["agent_id"])
    assert [a["auction_id"] for a in env.engine.list_active_auctions(direction="REVERSE")] == [v["auction_id"]]
    assert all(a["direction"] == "FORWARD" for a in env.engine.list_active_auctions())


def test_open_reverse_auction_lowest_quote_wins_and_roles_flip_in_the_match(env):
    ub, buyer = env.bidder()
    _, s1 = supplier(env)
    _, s2 = supplier(env)
    aid = rfq(env, buyer)["auction_id"]
    assert quote(env, aid, s1, 10_001)["code"] == "BID_TOO_HIGH"
    assert quote(env, aid, s1, 9_000)["ok"]
    assert quote(env, aid, s1, 8_000)["code"] == "ALREADY_LOWEST"
    assert quote(env, aid, s2, 8_950)["code"] == "BID_TOO_HIGH"  # must undercut by the 100 decrement
    assert env.engine.view(env.engine.get_auction(aid), s2["agent_id"])["max_next_bid"] == 8_900
    assert quote(env, aid, s2, 8_900)["ok"]
    env.clock.advance(61_000)
    env.engine.tick()
    a = env.engine.get_auction(aid)
    assert a["result"] == {"outcome": "SOLD", "winner_agent_id": s2["agent_id"], "price": 8_900}
    m = next(iter(env.store.matches.values()))
    assert m["seller_agent_id"] == s2["agent_id"] and m["buyer_agent_id"] == buyer["agent_id"] and m["direction"] == "REVERSE"
    assert m["agreed_terms"]["price"] == 8_900
    assert not [r for r in env.store.market_history if not r.get("reverse")]  # RFQ prices never pollute the forward price history


def test_only_suppliers_who_are_not_the_buyer_may_quote(env):
    ub, buyer = env.bidder()
    _, other_buyer = env.bidder()
    aid = rfq(env, buyer)["auction_id"]
    assert quote(env, aid, other_buyer, 5_000)["code"] == "INVALID_BIDDER"
    # the same person running a supplier agent cannot quote on their own RFQ
    own = env.app.agents.create_agent(user_id=ub["id"], type="SELLER", constraints={"reserve_floor": 0})
    assert quote(env, aid, own, 5_000)["code"] == "SELF_BID"
    _, seller = supplier(env)
    with pytest.raises(AppError):  # a forward bidder cannot be registered against an RFQ either
        env.execution.register_trigger(agent_id=other_buyer["agent_id"], auction_id=aid, kind="REVERSE_UNDERCUT", params={"min_price": 100})


def test_sealed_rfq_hides_quotes_and_lowest_wins_first_in_time_breaks_ties(env):
    _, buyer = env.bidder()
    _, s1 = supplier(env)
    _, s2 = supplier(env)
    _, s3 = supplier(env)
    aid = rfq(env, buyer, auction_type="REVERSE_SEALED")["auction_id"]
    assert quote(env, aid, s1, 12_000)["code"] == "BID_TOO_HIGH"
    assert quote(env, aid, s1, 7_000)["ok"]
    env.clock.advance(10)
    assert quote(env, aid, s2, 7_000)["ok"]
    assert quote(env, aid, s3, 7_500)["ok"]
    assert quote(env, aid, s3, 7_400)["ok"]  # revisable until close
    v = env.engine.view(env.engine.get_auction(aid), s3["agent_id"])
    assert len(v["bids"]) == 1 and v["current_price"] is None  # competitors' quotes stay hidden
    env.clock.advance(61_000)
    env.engine.tick()
    assert env.engine.get_auction(aid)["result"]["winner_agent_id"] == s1["agent_id"]
    assert env.engine.get_auction(aid)["result"]["price"] == 7_000


def test_anti_snipe_extends_a_reverse_auction(env):
    _, buyer = env.bidder()
    _, s1 = supplier(env)
    aid = rfq(env, buyer, anti_snipe={"window_ms": 5_000, "extend_ms": 10_000})["auction_id"]
    env.clock.advance(58_000)
    assert quote(env, aid, s1, 9_000)["ok"]
    a = env.engine.get_auction(aid)
    assert a["status"] == "EXTENDING" and a["extended_until"] == 70_000 + a["starts_at"]


def test_unanswered_rfq_notifies_the_buyer(env):
    _, buyer = env.bidder()
    aid = rfq(env, buyer, duration_ms=1_000)["auction_id"]
    env.clock.advance(2_000)
    env.engine.tick()
    assert env.engine.get_auction(aid)["result"]["outcome"] == "NO_SALE"
    assert any(n["kind"] == "rfq" for n in env.router.notifications_for(buyer["agent_id"]))


def test_poster_can_withdraw_only_before_quotes(env):
    _, buyer = env.bidder()
    _, s1 = supplier(env)
    a1, a2 = rfq(env, buyer)["auction_id"], rfq(env, buyer)["auction_id"]
    quote(env, a2, s1, 9_000)
    assert env.engine.withdraw_listing(listing_id=a1, seller_agent_id=buyer["agent_id"])["status"] == "CANCELLED"
    with pytest.raises(AppError) as e:
        env.engine.withdraw_listing(listing_id=a2, seller_agent_id=buyer["agent_id"])
    assert e.value.code == "HAS_BIDS"
    with pytest.raises(AppError):
        env.engine.withdraw_listing(listing_id=a2, seller_agent_id=s1["agent_id"])


def test_two_supplier_agents_compete_autonomously_and_respect_their_floors(env):
    _, buyer = env.bidder()
    _, s1 = supplier(env, floor=7_000)
    _, s2 = supplier(env, floor=8_000)
    aid = rfq(env, buyer, min_decrement=100)["auction_id"]
    env.execution.register_trigger(agent_id=s1["agent_id"], auction_id=aid, kind="REVERSE_UNDERCUT", params={"min_price": 7_000, "decrement_pct": 0})
    env.execution.register_trigger(agent_id=s2["agent_id"], auction_id=aid, kind="REVERSE_UNDERCUT", params={"min_price": 8_000, "decrement_pct": 0})
    a = env.engine.get_auction(aid)
    assert a["bids"][-1]["agent_id"] == s1["agent_id"] and a["current_price"] == 8_000  # s2 was pushed out at its 8,000 floor
    assert min(b["amount"] for b in a["bids"] if b["agent_id"] == s2["agent_id"]) >= 8_000  # s2 never went below its own floor
    ts2 = [t for t in env.store.triggers.values() if t["agent_id"] == s2["agent_id"]][0]
    assert ts2["status"] == "EXHAUSTED"
    env.clock.advance(61_000)
    env.engine.tick()
    r = env.engine.get_auction(aid)["result"]
    assert r["winner_agent_id"] == s1["agent_id"] and r["price"] == 8_000
    assert next(t for t in env.store.triggers.values() if t["agent_id"] == s1["agent_id"])["outcome"] == "WON"


def test_guardrail_blocks_a_quote_below_the_suppliers_floor(env):
    _, buyer = env.bidder()
    _, s1 = supplier(env, floor=5_000)
    aid = rfq(env, buyer)["auction_id"]
    d = env.guardrail.evaluate({"agent_id": s1["agent_id"], "auction_id": aid, "action": "place_bid", "amount": 4_999, "source": "user"})
    assert d["decision"] == "REJECTED" and d["code"] == "BELOW_FLOOR"
    d = env.guardrail.evaluate({"agent_id": s1["agent_id"], "auction_id": aid, "action": "place_bid", "amount": 5_000, "source": "strategy"})
    assert d["decision"] == "APPROVED"
    _, b2 = env.bidder()
    assert env.guardrail.evaluate({"agent_id": b2["agent_id"], "auction_id": aid, "action": "place_bid", "amount": 5_000, "source": "user"})["code"] == "NOT_A_SUPPLIER"


async def test_supplier_strategies_plan_within_their_limits(env):
    _, buyer = env.bidder()
    _, s1 = supplier(env, floor=6_000, memory={"auto_bid": True, "watch": {"category": "electronics"}})
    aid = rfq(env, buyer, max_price=10_000)["auction_id"]
    for strat, kind in (("baseline", "REVERSE_UNDERCUT"), ("heuristic", "REVERSE_UNDERCUT")):
        env.agents.update(s1["agent_id"], config={"algorithms": {"REVERSE_ENGLISH": {"strategy": strat, "llm_id": None}}})
        env.store.considered.clear()
        r = await env.orchestrator.consider(s1["agent_id"], aid, manual=True)
        assert r["status"] == "PLANNED" and r["trigger"]["kind"] == kind and r["trigger"]["params"]["min_price"] >= 6_000
    # a buyer maximum below the supplier's floor is filtered before any strategy (or token) is spent
    _, poor = env.bidder()
    low = rfq(env, poor, max_price=5_000)["auction_id"]
    assert (await env.orchestrator.consider(s1["agent_id"], low))["status"] == "FILTERED"
    sealed = rfq(env, buyer, auction_type="REVERSE_SEALED", max_price=9_000)["auction_id"]
    env.store.considered.clear()
    r = await env.orchestrator.consider(s1["agent_id"], sealed, manual=True)
    assert r["trigger"]["kind"] == "REVERSE_SEALED_BID" and 6_000 <= r["trigger"]["params"]["amount"] <= 9_000


def test_llm_proposals_are_clamped_into_the_floor_and_the_buyers_maximum(env):
    from kenyabidder.strategy import proposal_from_tool_input
    _, buyer = env.bidder()
    _, s1 = supplier(env, floor=6_000)
    a = env.engine.get_auction(rfq(env, buyer, max_price=10_000)["auction_id"])
    p = proposal_from_tool_input({"action": "BID", "min_price": 1, "decrement_pct": 500, "reasoning": "x"}, a, s1)
    assert p["params"]["min_price"] == 6_000 and p["params"]["decrement_pct"] == 100
    sealed = env.engine.get_auction(rfq(env, buyer, auction_type="REVERSE_SEALED", max_price=10_000)["auction_id"])
    assert proposal_from_tool_input({"action": "BID", "amount": 10**9, "reasoning": "x"}, sealed, s1)["params"]["amount"] == 10_000
    assert proposal_from_tool_input({"action": "BID", "amount": "cheap", "reasoning": "x"}, sealed, s1) is None


def test_a_sealed_quote_above_the_buyers_maximum_is_blocked_not_refired(env):
    _, buyer = env.bidder()
    _, s1 = supplier(env)
    aid = rfq(env, buyer, auction_type="REVERSE_SEALED", max_price=5_000)["auction_id"]
    t = env.execution.register_trigger(agent_id=s1["agent_id"], auction_id=aid, kind="REVERSE_SEALED_BID", params={"amount": 9_000})
    assert t["status"] == "BLOCKED" and t["last_error"] == "BID_TOO_HIGH"
    n = len(env.store.audit)
    env.engine.tick()
    env.execution.evaluate_all()
    assert len(env.store.audit) == n  # no more attempts, no more audit noise
