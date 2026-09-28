import pytest

from kenyabidder.errors import AppError

WATCH = {"category": "electronics", "keywords": [], "min_quantity": 1}


def algos(strategy, llm_id=None):
    return {t: {"strategy": strategy, "llm_id": llm_id} for t in ["ENGLISH", "DUTCH", "FIRST_PRICE_SEALED", "SECOND_PRICE_SEALED"]}


async def test_new_listing_triggers_only_matching_agents(env):
    _, s = env.seller()
    _, fit = env.bidder(memory={"watch": WATCH}, config={"algorithms": algos("baseline")})
    _, wrong = env.bidder(memory={"watch": {"category": "furniture"}})
    _, poor = env.bidder(ceiling=500, memory={"watch": WATCH})
    _, late = env.bidder(memory={"watch": {**WATCH, "deadline_at": env.clock.now() + 1000}})
    a = env.english(s["agent_id"])
    await env.orchestrator.idle()
    bidders = {b["agent_id"] for b in env.engine.get_auction(a["auction_id"])["bids"]}
    assert bidders == {fit["agent_id"]}
    for x in (wrong, poor, late):
        assert not env.execution.triggers_for(x["agent_id"])


async def test_unauthorized_type_requests_participation_then_starts(env):
    _, s = env.seller()
    _, b = env.bidder(constraints={"authorized_auction_types": ["ENGLISH"]}, memory={"watch": WATCH}, config={"algorithms": algos("baseline")})
    a = env.english(s["agent_id"], auction_type="SECOND_PRICE_SEALED", reserve_price=100, duration_ms=60_000, start_price=None)
    await env.orchestrator.idle()
    ap = next(iter(env.store.approvals.values()))
    assert ap["kind"] == "PARTICIPATE" and not env.engine.get_auction(a["auction_id"])["bids"]
    await env.orchestrator.resolve_approval(ap["id"], True)
    assert env.engine.get_auction(a["auction_id"])["bids"][0]["agent_id"] == b["agent_id"]


async def test_heuristic_uses_market_history(env):
    for p in (8000, 9000, 10_000):
        env.store.market_history.append({"category": "electronics", "auction_type": "ENGLISH", "price": p, "quantity": 1, "at": 1})
    _, s = env.seller()
    _, b = env.bidder(ceiling=50_000)
    cheap = env.english(s["agent_id"], start_price=1000, reserve_price=1000)
    r = await env.orchestrator.consider(b["agent_id"], cheap["auction_id"], manual=True)
    assert r["status"] == "PLANNED" and r["trigger"]["params"]["max_bid"] == 9450 and r["trigger"]["params"]["snipe_window_ms"] > 0
    dear = env.english(s["agent_id"], start_price=40_000, reserve_price=40_000)
    assert (await env.orchestrator.consider(b["agent_id"], dear["auction_id"], manual=True))["status"] == "SKIPPED"


async def test_per_algorithm_strategy_mapping(env):
    _, s = env.seller()
    _, b = env.bidder(ceiling=50_000, config={"algorithms": {**algos("heuristic"), "ENGLISH": {"strategy": "baseline", "llm_id": None}}})
    a = env.english(s["agent_id"])
    r = await env.orchestrator.consider(b["agent_id"], a["auction_id"], manual=True)
    assert r["strategy"] == "baseline"
    sealed = env.english(s["agent_id"], auction_type="SECOND_PRICE_SEALED", reserve_price=100, duration_ms=60_000, start_price=None)
    assert (await env.orchestrator.consider(b["agent_id"], sealed["auction_id"], manual=True))["strategy"] == "heuristic"


def settle_match(env, s, b):
    a = env.english(s["agent_id"], duration_ms=1000)
    env.engine.submit_bid(auction_id=a["auction_id"], agent_id=b["agent_id"], amount=1000)
    env.clock.advance(1000)
    env.engine.tick()
    return next(m for m in env.store.matches.values() if m["auction_id"] == a["auction_id"])


def test_match_flow_and_reputation_tiers(env):
    _, s = env.seller()
    _, b = env.bidder(name="David")
    m = settle_match(env, s, b)
    assert m["status"] == "PROPOSED" and m["contact_reveal"] is None
    with pytest.raises(AppError) as e:
        env.matches.report_outcome(m["match_id"], b["agent_id"], "COMPLETED")
    assert e.value.code == "INVALID_STATE"
    env.matches.confirm_match(m["match_id"], s["agent_id"])
    assert m["status"] == "SELLER_CONFIRMED" and m["contact_reveal"] is None
    env.matches.confirm_match(m["match_id"], b["agent_id"])
    assert m["status"] == "CONTACT_REVEALED" and m["contact_reveal"]["buyer_contact"]["name"] == "David"
    assert len(env.store.reveal_log) == 1
    env.matches.report_outcome(m["match_id"], s["agent_id"], "COMPLETED")
    assert m["status"] == "CONTACT_REVEALED"
    env.matches.report_outcome(m["match_id"], b["agent_id"], "COMPLETED")
    assert m["status"] == "COMPLETED" and s["reputation"]["completed_matches"] == 1 and s["reputation"]["tier"] == "NEW"
    for _ in range(2):
        x = settle_match(env, s, b)
        env.matches.confirm_match(x["match_id"], s["agent_id"])
        env.matches.confirm_match(x["match_id"], b["agent_id"])
        env.matches.report_outcome(x["match_id"], s["agent_id"], "COMPLETED")
        env.matches.report_outcome(x["match_id"], b["agent_id"], "COMPLETED")
    assert s["reputation"]["tier"] == "ESTABLISHED"
    bad = settle_match(env, s, b)
    env.matches.confirm_match(bad["match_id"], s["agent_id"])
    env.matches.confirm_match(bad["match_id"], b["agent_id"])
    env.matches.report_outcome(bad["match_id"], s["agent_id"], "FELL_THROUGH")
    assert bad["status"] == "FELL_THROUGH" and bad["fault_agent_ids"] == [b["agent_id"]]
    assert s["reputation"]["fell_through_count"] == 0 and b["reputation"]["fell_through_count"] == 1


def test_non_party_blocked_and_stale_matches_lapse(make_env):
    env = make_env(match_ttl_ms=1000)
    _, s = env.seller()
    _, b = env.bidder()
    _, out = env.bidder()
    a = env.english(s["agent_id"], duration_ms=500)
    env.engine.submit_bid(auction_id=a["auction_id"], agent_id=b["agent_id"], amount=1000)
    env.clock.advance(500)
    env.engine.tick()
    m = next(iter(env.store.matches.values()))
    with pytest.raises(AppError) as e:
        env.matches.confirm_match(m["match_id"], out["agent_id"])
    assert e.value.code == "NOT_A_PARTY"
    env.matches.confirm_match(m["match_id"], s["agent_id"])
    env.clock.advance(2000)
    env.app.tick()
    assert m["status"] == "NO_RESPONSE" and m["fault_agent_ids"] == [b["agent_id"]]


def test_seller_auto_relist_respects_floor_and_max(env):
    _, s = env.seller(reserve_floor=800, memory={"auto_relist": {"max_relists": 2, "discount_pct": 10}})
    first = env.english(s["agent_id"], reserve_price=1000, start_price=1000, duration_ms=1000)
    lst = lambda: list(env.store.auctions.values())
    env.clock.advance(1000); env.engine.tick()
    assert len(lst()) == 2 and lst()[1]["reserve_price"] == 900 and lst()[1]["relist_of"] == first["auction_id"]
    env.clock.advance(1000); env.engine.tick()
    assert lst()[2]["reserve_price"] == 810
    env.clock.advance(1000); env.engine.tick()
    assert len(lst()) == 3


async def test_omnichannel_one_agent_many_channels(env):
    _, b = env.bidder(ceiling=50_000)
    env.agents.link_channel(b["agent_id"], "WHATSAPP", "+254711111111")
    _, other = env.bidder()
    with pytest.raises(AppError) as e:
        env.agents.link_channel(other["agent_id"], "WHATSAPP", "+254711111111")
    assert e.value.code == "CHANNEL_TAKEN"
    r = await env.router.handle_inbound("WHATSAPP", "+254711111111", "ceiling 75,000")
    assert "75000" in r["reply"] and env.agents.get_agent(b["agent_id"])["constraints"]["budget_ceiling"] == 75_000
    await env.router.handle_inbound("WHATSAPP", "+254711111111", "pause")
    assert b["status"] == "PAUSED"
    env.agents.set_status(b["agent_id"], "ACTIVE")  # resumed from the web
    assert "ACTIVE" in (await env.router.handle_inbound("WHATSAPP", "+254711111111", "status"))["reply"]
    assert len(b["durable_memory"]["conversation"]) == 6
    assert (await env.router.handle_inbound("WHATSAPP", "+254799999999", "status"))["agent_id"] is None
    assert "failed" in (await env.router.handle_inbound("WHATSAPP", "+254711111111", "ceiling banana"))["reply"]


async def test_approve_escalated_bid_from_whatsapp(env):
    _, s = env.seller()
    _, b = env.bidder(ceiling=10_000, constraints={"escalation_threshold_pct": 50}, memory={"watch": WATCH, "preferred_channel": "WHATSAPP"},
                      config={"algorithms": algos("baseline")})
    env.agents.link_channel(b["agent_id"], "WHATSAPP", "+254722222222")
    a = env.english(s["agent_id"], start_price=6000, reserve_price=6000)
    await env.orchestrator.idle()
    msg = next(m for m in env.store.outbox if "Approval needed" in m["text"])
    short = msg["text"].split("approve ")[1][:8]
    await env.router.handle_inbound("WHATSAPP", "+254722222222", f"approve {short}")
    assert env.engine.get_auction(a["auction_id"])["bids"][-1]["agent_id"] == b["agent_id"]


def test_market_intel_and_recommendation(env):
    _, s = env.seller()
    for p in (1000, 2000, 3000, 4000):
        env.store.market_history.append({"category": "electronics", "auction_type": "ENGLISH", "price": p, "quantity": 1, "at": 1})
    st = env.intel.historical_clearing_prices("Electronics")
    assert st["count"] == 4 and st["median"] == 2500
    assert env.intel.recommend_listing("electronics", quantity=50)["auction_type"] == "DUTCH"
    assert env.intel.recommend_listing("electronics", scarce=True)["auction_type"] == "ENGLISH"
    assert env.intel.recommend_listing("electronics", quantity=50, allowed_types=["ENGLISH"])["auction_type"] == "ENGLISH"
    env.english(s["agent_id"])
    assert env.intel.demand_signal("electronics")["active_auctions"] == 1
    assert env.intel.comparable_active_auctions("electronics", "Samsung phones")[0]["overlap"] == 2


async def test_kpis_from_audit(env):
    _, s = env.seller()
    env.bidder(ceiling=50_000, memory={"watch": WATCH}, config={"algorithms": algos("baseline")})
    env.english(s["agent_id"], duration_ms=1000)
    await env.orchestrator.idle()
    env.clock.advance(1000)
    env.app.tick()
    k = env.app.kpis()
    assert k["agent_performance"]["auctions_settled"] == 1 and k["agent_performance"]["sell_through_rate"] == 1
    assert k["agent_performance"]["bid_latency_ms"]["samples"] >= 1 and k["agent_performance"]["bid_latency_ms"]["p95"] < 200


def test_users_auth_and_first_user_is_admin(env):
    u1 = env.agents.create_user(name="Admin One", password="correct-horse")
    u2 = env.agents.create_user(name="Someone", password="another-pass1")
    assert u1["role"] == "admin" and u2["role"] == "user"
    assert env.agents.authenticate("admin one", "correct-horse")["id"] == u1["id"]
    assert env.agents.authenticate("Admin One", "wrong-pass") is None
    assert env.agents.authenticate("nobody", "whatever12") is None
    assert "correct-horse" not in u1["password_hash"]
    for bad_kw in ({"name": "x", "password": "longenough1"}, {"name": "Valid Name", "password": "short"}, {"name": "someone", "password": "longenough1"}):
        with pytest.raises(AppError):
            env.agents.create_user(**bad_kw)


def test_snapshot_roundtrip(env, tmp_path):
    from kenyabidder.store import Store
    _, s = env.seller()
    env.english(s["agent_id"])
    env.llms.add(name="M", provider="anthropic", model="x", api_key="sk-secret")
    p = tmp_path / "s.json"
    env.store.save(p)
    assert oct(p.stat().st_mode)[-3:] == "600"
    again = Store.load(p)
    assert len(again.auctions) == 1 and len(again.llms) == 1 and "builtin" in again.mcps


def test_manual_bid_passes_guardrails_and_is_audited(env):
    _, s = env.seller()
    _, b = env.bidder(ceiling=2000)
    a = env.english(s["agent_id"])
    over = env.app.manual_bid(b["agent_id"], a["auction_id"], 9999)
    assert not over["ok"] and over["code"] == "CEILING_EXCEEDED"
    ok = env.app.manual_bid(b["agent_id"], a["auction_id"], 1500)
    assert ok["ok"] and env.engine.get_auction(a["auction_id"])["bids"][-1]["amount"] == 1500
    kinds = [(e["proposed_action"]["action"], e["guardrail_decision"]) for e in env.audit.for_agent(b["agent_id"])]
    assert ("manual_bid", "REJECTED") in kinds and ("manual_bid", "APPROVED") in kinds
    low = env.app.manual_bid(b["agent_id"], a["auction_id"], 1200)
    assert not low["ok"] and low["code"] in ("BID_TOO_LOW", "ALREADY_HIGHEST")


def test_login_lockout_after_repeated_failures(env):
    env.agents.create_user(name="Target User", password="correct-horse")
    for _ in range(5):
        assert env.agents.authenticate("target user", "wrong-guess") is None
    with pytest.raises(AppError) as e:  # even the right password is refused while locked out
        env.agents.authenticate("Target User", "correct-horse")
    assert e.value.code == "TOO_MANY_ATTEMPTS" and e.value.status == 429
    env.clock.advance(5 * 60_000 + 1)
    assert env.agents.authenticate("Target User", "correct-horse")["name"] == "Target User"
    for _ in range(5):  # unknown names are throttled identically (no user enumeration)
        env.agents.authenticate("ghost", "x")
    with pytest.raises(AppError):
        env.agents.authenticate("ghost", "x")


async def test_whatsapp_webhook_requires_valid_signature(env):
    import hashlib, hmac, json
    from kenyabidder.channels import handle_whatsapp_webhook
    _, b = env.bidder(ceiling=1000)
    env.agents.link_channel(b["agent_id"], "WHATSAPP", "254711000111")
    body = json.dumps({"entry": [{"changes": [{"value": {"messages": [{"from": "254711000111", "text": {"body": "ceiling 4000"}}]}}]}]}).encode()
    sig = "sha256=" + hmac.new(b"appsecret", body, hashlib.sha256).hexdigest()

    with pytest.raises(AppError) as e:  # unconfigured -> refused (an open webhook would let anyone approve bids)
        await handle_whatsapp_webhook(env.router, body, sig, secret="")
    assert e.value.code == "WEBHOOK_NOT_CONFIGURED"
    for bad_sig in (None, "sha256=deadbeef", sig.replace("sha256=", "")):
        with pytest.raises(AppError) as e:
            await handle_whatsapp_webhook(env.router, body, bad_sig, secret="appsecret")
        assert e.value.code == "BAD_SIGNATURE"
    assert b["constraints"]["budget_ceiling"] == 1000  # nothing executed by rejected requests
    ok = await handle_whatsapp_webhook(env.router, body, sig, secret="appsecret")
    assert "4000" in ok["reply"] and b["constraints"]["budget_ceiling"] == 4000
    assert any("4000" in m["text"] for m in env.store.outbox)
    # dev opt-out accepts the simple {from,text} shape and ignores junk
    dev = await handle_whatsapp_webhook(env.router, json.dumps({"from": "254711000111", "text": "status"}).encode(), None, secret="", allow_unsigned=True)
    assert "BIDDER" in dev["reply"]
    assert (await handle_whatsapp_webhook(env.router, b'{"hello": 1}', None, secret="", allow_unsigned=True))["ignored"]
    with pytest.raises(AppError) as e:
        await handle_whatsapp_webhook(env.router, b"not json", None, secret="", allow_unsigned=True)
    assert e.value.code == "INVALID_JSON"
