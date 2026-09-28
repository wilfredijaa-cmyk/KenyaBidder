import pytest

from kenyabidder.errors import AppError
from kenyabidder.execution import DirectTransport, TransientError
from tests.test_engine import dutch_listing


def trig(env, b, auction_id, kind, params, **kw):
    return env.execution.register_trigger(agent_id=b["agent_id"], auction_id=auction_id, kind=kind, params=params, **kw)


def test_english_trigger_bids_and_rebids_up_to_max(env):
    _, s = env.seller()
    _, b1 = env.bidder()
    _, b2 = env.bidder()
    a = env.english(s["agent_id"])
    trig(env, b1, a["auction_id"], "ENGLISH_INCREMENTAL", {"max_bid": 2000, "increment_pct": 0, "snipe_window_ms": 0})
    assert env.engine.get_auction(a["auction_id"])["bids"][-1]["amount"] == 1000
    trig(env, b2, a["auction_id"], "ENGLISH_INCREMENTAL", {"max_bid": 1500, "increment_pct": 0, "snipe_window_ms": 0})
    top = env.engine.get_auction(a["auction_id"])["bids"][-1]
    assert top["agent_id"] == b1["agent_id"] and top["amount"] == 1600
    env.clock.advance(60_000)
    assert env.engine.get_auction_detail(a["auction_id"])["result"]["winner_agent_id"] == b1["agent_id"]
    assert env.execution.triggers_for(b2["agent_id"])[0]["status"] == "EXHAUSTED"


def test_snipe_window_waits_then_fires(env):
    _, s = env.seller()
    _, b = env.bidder()
    a = env.english(s["agent_id"])
    trig(env, b, a["auction_id"], "ENGLISH_INCREMENTAL", {"max_bid": 5000, "snipe_window_ms": 5000})
    assert not env.engine.get_auction(a["auction_id"])["bids"]
    env.clock.advance(54_000)
    env.app.tick()
    assert not env.engine.get_auction(a["auction_id"])["bids"]
    env.clock.advance(1_500)
    env.app.tick()
    assert len(env.engine.get_auction(a["auction_id"])["bids"]) == 1


def test_dutch_trigger_fires_when_price_crosses(env):
    _, s = env.seller()
    _, b = env.bidder()
    a = dutch_listing(env, s, decrement=1000)
    trig(env, b, a["auction_id"], "DUTCH_ACCEPT", {"threshold": 7000})
    env.clock.advance(20_000)
    env.app.tick()
    assert env.engine.get_auction(a["auction_id"])["status"] == "ACTIVE"
    env.clock.advance(10_000)
    env.app.tick()
    d = env.engine.get_auction_detail(a["auction_id"])
    assert d["status"] == "SETTLED" and d["result"]["price"] == 7000 and len(env.store.matches) == 1


def test_guardrail_blocks_over_ceiling_and_audits(env):
    _, s = env.seller()
    _, b = env.bidder(ceiling=1500)
    _, rival = env.bidder()
    a = env.english(s["agent_id"])
    t = trig(env, b, a["auction_id"], "ENGLISH_INCREMENTAL", {"max_bid": 9000})
    env.engine.submit_bid(auction_id=a["auction_id"], agent_id=rival["agent_id"], amount=1600)
    env.execution.evaluate_auction(a["auction_id"])
    assert t["status"] == "BLOCKED" and t["last_error"] == "CEILING_EXCEEDED"
    assert any(e["guardrail_decision"] == "REJECTED" and e["rejection_reason"].startswith("CEILING_EXCEEDED") for e in env.audit.for_agent(b["agent_id"]))
    assert env.engine.get_auction(a["auction_id"])["bids"][-1]["amount"] == 1600


def test_escalation_needs_approval_and_approval_unblocks(env):
    _, s = env.seller()
    _, b = env.bidder(ceiling=10_000, constraints={"escalation_threshold_pct": 50})
    a = env.english(s["agent_id"], start_price=6000, reserve_price=6000)
    t = trig(env, b, a["auction_id"], "ENGLISH_INCREMENTAL", {"max_bid": 9000})
    assert t["status"] == "AWAITING_APPROVAL" and not env.engine.get_auction(a["auction_id"])["bids"]
    ap = next(iter(env.store.approvals.values()))
    assert ap["code"] == "ESCALATION_THRESHOLD"
    env.execution.resolve_approval(ap["id"], True)
    assert env.engine.get_auction(a["auction_id"])["bids"][-1]["amount"] == 6000


def test_rejecting_approval_cancels_and_cannot_resolve_twice(env):
    _, s = env.seller()
    _, b = env.bidder(ceiling=10_000, constraints={"escalation_threshold_pct": 50})
    a = env.english(s["agent_id"], start_price=6000, reserve_price=6000)
    t = trig(env, b, a["auction_id"], "ENGLISH_INCREMENTAL", {"max_bid": 9000})
    ap = next(iter(env.store.approvals.values()))
    env.execution.resolve_approval(ap["id"], False)
    assert t["status"] == "CANCELLED"
    with pytest.raises(AppError) as e:
        env.execution.resolve_approval(ap["id"], True)
    assert e.value.code == "APPROVAL_RESOLVED"


def test_revoking_authority_cancels_triggers(env):
    _, s = env.seller()
    _, b = env.bidder()
    a = env.english(s["agent_id"])
    t = trig(env, b, a["auction_id"], "ENGLISH_INCREMENTAL", {"max_bid": 5000, "snipe_window_ms": 5000})
    env.agents.set_status(b["agent_id"], "PAUSED")
    assert t["status"] == "CANCELLED"
    env.clock.advance(56_000)
    env.app.tick()
    assert not env.engine.get_auction(a["auction_id"])["bids"]


def test_network_drop_after_bid_landed_reconciles(env):
    _, s = env.seller()
    _, b = env.bidder()
    a = env.english(s["agent_id"])
    direct, calls = DirectTransport(env.engine), []

    class Flaky:
        def submit_bid(self, **kw):
            calls.append(1)
            direct.submit_bid(**kw)
            raise TransientError()
    env.execution.transport = Flaky()
    trig(env, b, a["auction_id"], "ENGLISH_INCREMENTAL", {"max_bid": 5000})
    assert len(env.engine.get_auction(a["auction_id"])["bids"]) == 1 and len(calls) == 1
    assert env.audit.for_agent(b["agent_id"])[0]["execution_result"]["reconciled"] is True


def test_network_drop_before_bid_retries_same_key(env):
    _, s = env.seller()
    _, b = env.bidder()
    a = env.english(s["agent_id"])
    direct, keys = DirectTransport(env.engine), []

    class Flaky:
        def submit_bid(self, **kw):
            keys.append(kw["idempotency_key"])
            if len(keys) <= 2:
                raise TransientError()
            return direct.submit_bid(**kw)
    env.execution.transport = Flaky()
    trig(env, b, a["auction_id"], "ENGLISH_INCREMENTAL", {"max_bid": 5000})
    assert len(env.engine.get_auction(a["auction_id"])["bids"]) == 1
    assert len(set(keys)) == 1 and len(keys) == 3


def test_persistent_transport_failure_is_surfaced(env):
    _, s = env.seller()
    _, b = env.bidder()
    a = env.english(s["agent_id"])

    class Dead:
        def submit_bid(self, **kw):
            raise TransientError()
    env.execution.transport = Dead()
    t = trig(env, b, a["auction_id"], "ENGLISH_INCREMENTAL", {"max_bid": 5000})
    assert t["status"] == "BLOCKED" and t["last_error"] == "TRANSPORT_FAILURE"


def test_rate_limit_stops_runaway_and_recovers(make_env):
    env = make_env(guardrail_config={"max_proposals_per_minute": 5})
    _, s = env.seller()
    _, b1 = env.bidder(ceiling=1_000_000)
    _, b2 = env.bidder(ceiling=1_000_000)
    a = env.english(s["agent_id"], duration_ms=600_000)
    trig(env, b1, a["auction_id"], "ENGLISH_INCREMENTAL", {"max_bid": 1_000_000})
    trig(env, b2, a["auction_id"], "ENGLISH_INCREMENTAL", {"max_bid": 1_000_000})
    n = len(env.engine.get_auction(a["auction_id"])["bids"])
    assert n <= 10
    env.clock.advance(61_000)
    env.app.tick()
    assert len(env.engine.get_auction(a["auction_id"])["bids"]) > n


def test_kind_and_params_validation(env):
    _, s = env.seller()
    _, b = env.bidder()
    a = env.english(s["agent_id"])
    for kind, params, code in [("DUTCH_ACCEPT", {"threshold": 1}, "KIND_MISMATCH"), ("ENGLISH_INCREMENTAL", {"max_bid": -5}, "INVALID_PARAMS")]:
        with pytest.raises(AppError) as e:
            trig(env, b, a["auction_id"], kind, params)
        assert e.value.code == code


def test_market_anomaly_trips_circuit_breaker(env):
    _, s = env.seller()
    for _ in range(5):
        env.store.market_history.append({"category": "electronics", "auction_type": "ENGLISH", "price": 1000, "quantity": 1, "at": 1})
    _, b = env.bidder(ceiling=1_000_000)
    a = env.english(s["agent_id"], start_price=50_000, reserve_price=50_000)
    t = trig(env, b, a["auction_id"], "ENGLISH_INCREMENTAL", {"max_bid": 900_000})
    assert t["status"] == "BLOCKED" and t["last_error"] == "MARKET_ANOMALY"
    assert "electronics" in env.store.breakers
    assert any(n["kind"] == "anomaly" for n in env.router.notifications_for(b["agent_id"]))


def test_sealed_trigger_submits_once_and_wins(env):
    _, s = env.seller()
    _, b = env.bidder()
    a = env.english(s["agent_id"], auction_type="SECOND_PRICE_SEALED", reserve_price=100, duration_ms=5000, start_price=None)
    t = trig(env, b, a["auction_id"], "SEALED_BID", {"amount": 3000})
    assert t["status"] == "DONE"
    env.clock.advance(5000)
    assert env.engine.get_auction_detail(a["auction_id"])["result"]["winner_agent_id"] == b["agent_id"]


def test_unauthorized_type_escalates(env):
    _, s = env.seller()
    _, b = env.bidder(constraints={"authorized_auction_types": ["ENGLISH"]})
    a = env.english(s["agent_id"], auction_type="SECOND_PRICE_SEALED", reserve_price=100, duration_ms=5000, start_price=None)
    t = trig(env, b, a["auction_id"], "SEALED_BID", {"amount": 3000})
    assert t["status"] == "AWAITING_APPROVAL" and not env.engine.get_auction(a["auction_id"])["bids"]
    env.execution.resolve_approval(t["approval_id"], True)
    assert len(env.engine.get_auction(a["auction_id"])["bids"]) == 1


def test_standing_trigger_beats_late_manual_accept_single_settlement(env):
    _, s = env.seller()
    _, t1 = env.bidder()
    _, manual = env.bidder()
    a = dutch_listing(env, s, decrement=1000)
    trig(env, t1, a["auction_id"], "DUTCH_ACCEPT", {"threshold": 9500})
    settled = []
    env.engine.events.on("auction.settled", lambda **kw: settled.append(kw))
    env.clock.advance(15_000)
    res = env.engine.submit_bid(auction_id=a["auction_id"], agent_id=manual["agent_id"], amount=9000)
    assert not res["ok"] and res["code"] == "AUCTION_NOT_OPEN"
    assert len(settled) == 1 and len(env.store.matches) == 1
    assert env.engine.get_auction(a["auction_id"])["result"]["winner_agent_id"] == t1["agent_id"]


def test_fractional_increment_pct_still_places_an_integer_bid(env):
    """Regression: an LLM proposing increment_pct=2.5 produced a float amount and permanently blocked the trigger."""
    _, s = env.seller()
    _, b = env.bidder(ceiling=10**7)
    _, rival = env.bidder(ceiling=10**7)
    a = env.english(s["agent_id"], start_price=10_000, reserve_price=10_000)
    env.engine.submit_bid(auction_id=a["auction_id"], agent_id=rival["agent_id"], amount=10_000)
    t = trig(env, b, a["auction_id"], "ENGLISH_INCREMENTAL", {"max_bid": 900_000, "increment_pct": 2.5, "snipe_window_ms": 0})
    assert t["status"] == "ACTIVE" and t["last_error"] is None
    top = env.engine.get_auction(a["auction_id"])["bids"][-1]
    assert top["agent_id"] == b["agent_id"] and top["amount"] == 10_250 and isinstance(top["amount"], int)
