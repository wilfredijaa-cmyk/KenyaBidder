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
        env.store.market_history.append({"category": "electronics", "auction_type": "ENGLISH", "price": p, "quantity": 10, "at": 1})   # same lot size as the listing
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
    assert bad["status"] == "CONTACT_REVEALED" and bad["fault_agent_ids"] == []      # one side's word alone changes nothing yet
    env.clock.advance(73 * 3600_000)                                                   # …until the buyer stayed silent for the grace period
    env.app.tick()
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
    env.link_whatsapp(b, "+254711111111")
    ou, other = env.bidder()
    env.store.users[ou["id"]].update(phone="+254711111111", phone_verified=True)  # (forced: two accounts cannot normally share a number)
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
    env.link_whatsapp(b, "+254722222222")
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
    env.link_whatsapp(b, "254711000111")
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


def _revealed(env, s, b):
    m = settle_match(env, s, b)
    env.matches.confirm_match(m["match_id"], s["agent_id"])
    env.matches.confirm_match(m["match_id"], b["agent_id"])
    return m


def test_a_single_report_never_hurts_anyone_and_the_other_side_can_answer(env):
    _, s = env.seller()
    _, b = env.bidder()
    m = _revealed(env, s, b)
    env.matches.report_outcome(m["match_id"], b["agent_id"], "FELL_THROUGH")
    assert m["status"] == "CONTACT_REVEALED" and s["reputation"]["fell_through_count"] == 0
    assert any("Please report your side" in n["message"] for n in env.router.notifications_for(s["agent_id"]))
    env.matches.report_outcome(m["match_id"], s["agent_id"], "COMPLETED")            # right of reply: they contradict each other
    assert m["status"] == "DISPUTED" and m["fault_agent_ids"] == []
    assert s["reputation"]["completed_matches"] == 0 and s["reputation"]["fell_through_count"] == 0 and b["reputation"]["fell_through_count"] == 0


def test_mutual_blame_faults_both_and_lone_completed_report_stands_after_grace(env):
    _, s = env.seller()
    _, b = env.bidder()
    m = _revealed(env, s, b)
    env.matches.report_outcome(m["match_id"], s["agent_id"], "FELL_THROUGH")
    env.matches.report_outcome(m["match_id"], b["agent_id"], "FELL_THROUGH")
    assert m["status"] == "FELL_THROUGH" and sorted(m["fault_agent_ids"]) == sorted([s["agent_id"], b["agent_id"]])
    m2 = _revealed(env, s, b)
    env.matches.report_outcome(m2["match_id"], s["agent_id"], "COMPLETED")
    env.clock.advance(71 * 3600_000)
    env.app.tick()
    assert m2["status"] == "CONTACT_REVEALED"
    env.clock.advance(2 * 3600_000)
    env.app.tick()
    assert m2["status"] == "COMPLETED" and s["reputation"]["completed_matches"] == 1


async def test_suspended_users_cannot_resume_via_whatsapp_or_service(env):
    admin = env.agents.create_user(name="Root Admin", password="password123")
    u, b = env.bidder(name="Banned One")
    env.link_whatsapp(b, "254799000111")
    env.agents.set_suspended(u["id"], True, by=admin["id"])
    r = await env.router.handle_inbound("WHATSAPP", "254799000111", "resume")
    assert "suspended" in r["reply"] and b["status"] == "SUSPENDED"
    with pytest.raises(AppError) as e:
        env.agents.set_status(b["agent_id"], "ACTIVE")
    assert e.value.code == "ACCOUNT_SUSPENDED"
    env.agents.set_suspended(u["id"], False, by=admin["id"])
    assert b["status"] == "PAUSED"                                                    # reinstated accounts restart deliberately
    env.agents.set_status(b["agent_id"], "ACTIVE")
    assert b["status"] == "ACTIVE"


async def test_whatsapp_batched_deliveries_run_every_message_and_odd_headers_are_403(env):
    import hashlib, hmac, json
    from kenyabidder.channels import handle_whatsapp_webhook
    _, b = env.bidder(ceiling=1000)
    env.link_whatsapp(b, "254711000222")
    msgs = lambda *texts: [{"from": "254711000222", "text": {"body": t}} for t in texts]
    body = json.dumps({"entry": [{"changes": [{"value": {"messages": msgs("status", "ceiling 4000")}}]}, {"changes": [{"value": {"messages": msgs("pause")}}]}]}).encode()
    out = await handle_whatsapp_webhook(env.router, body, None, secret="", allow_unsigned=True)
    assert out["handled"] == 3 and b["constraints"]["budget_ceiling"] == 4000 and b["status"] == "PAUSED"   # not just the first message
    with pytest.raises(AppError) as e:
        await handle_whatsapp_webhook(env.router, body, "sha256=\u00e9\u2603", secret="appsecret")
    assert e.value.code == "BAD_SIGNATURE"


def test_evening_summary_waits_for_news_instead_of_giving_up_for_the_day(env):
    from kenyabidder.timeutil import eat
    _, s = env.seller()
    day_start = env.clock.now() - env.clock.now() % (24 * 3600_000) - 3 * 3600_000
    env.clock.t = day_start + 18 * 3600_000 + 20_000                                  # 18:00:20 EAT, nothing happened today
    assert env.sellers.send_daily_summaries() == 0
    env.english(s["agent_id"], duration_ms=10 * 3600_000)                              # a listing appears at 18:00:20…
    env.clock.advance(60_000)
    assert env.sellers.send_daily_summaries() == 1                                     # …and the same evening's summary still goes out
    assert eat(env.clock.now()).hour == 18


async def test_disabled_llm_does_not_trap_the_agent_configuration(env):
    llm = env.llms.add(name="Later Disabled", provider="anthropic", model="m", api_key="k", billing_mode="free")
    cfg = {"llm_id": llm["id"], "algorithms": {"ENGLISH": {"strategy": "llm", "llm_id": None}}}
    _, b = env.bidder(config=cfg)
    env.llms.update(llm["id"], enabled=False)
    env.agents.update(b["agent_id"], config={"max_tokens_per_day": 5_000})           # unrelated edit still works
    assert b["config"]["max_tokens_per_day"] == 5_000 and b["config"]["llm_id"] == llm["id"]
    other = env.llms.add(name="Fresh", provider="anthropic", model="m", api_key="k")
    env.llms.update(other["id"], enabled=False)
    with pytest.raises(AppError):                                                     # but you cannot newly *assign* a disabled one
        env.agents.update(b["agent_id"], config={"llm_id": other["id"]})


async def test_blocked_retries_do_not_burn_the_hourly_decision_budget(env):
    """Regression: begin_decision counted before tokens were reserved, so a no-token agent retrying every 30s exhausted its budget."""
    llm = env.llms.add(name="Claude", provider="anthropic", model="m", api_key="k")
    _, s = env.seller()
    from tests.test_metering import llm_bidder
    u, b = llm_bidder(env, llm)
    a = env.english(s["agent_id"], duration_ms=10 * 3600_000)
    env.store.settings["max_llm_decisions_per_hour"] = 2
    for _ in range(10):
        assert (await env.orchestrator.consider(b["agent_id"], a["auction_id"], manual=True))["code"] == "NO_TOKENS"
    assert env.meter.decisions_last_hour(b["agent_id"]) == 0 and env.meter._inflight.get(b["agent_id"], 0) == 0
    from kenyabidder.llm.providers import ScriptedProvider
    from tests.test_metering import bid
    env.llms.set_provider_override(llm["id"], ScriptedProvider([bid()]))
    env.wallet.credit(u["id"], llm["id"], 10**6, "GRANT")
    assert (await env.orchestrator.consider(b["agent_id"], a["auction_id"], manual=True))["status"] == "PLANNED"   # not RATE_LIMITED


async def test_heuristic_skips_a_dutch_lot_whose_floor_exceeds_its_valuation(env):
    from kenyabidder.strategy import HeuristicStrategy
    _, s = env.seller()
    _, b = env.bidder(ceiling=100_000)
    a = env.engine.create_listing(seller_agent_id=s["agent_id"], product_spec={"category": "electronics", "title": "T", "quantity": 1}, auction_type="DUTCH",
                                  reserve_price=24_000, duration_ms=100_000, dutch={"start_price": 40_000, "floor_price": 24_000, "decrement": 1000, "interval_ms": 1000})
    p = await HeuristicStrategy().propose({"agent": b, "auction": env.engine.get_auction(a["auction_id"]), "intel": {"stats": {"median": 9_524, "count": 5}}})   # value ≈ 10,000
    assert p["action"] == "SKIP"                                                       # never raises its offer up to the floor


async def test_valuation_scales_per_unit_history_to_the_size_of_the_lot(env):
    """Regression: the heuristic and anomaly breaker compared lot totals of different sizes."""
    for p in (1000, 1100, 900):
        env.store.market_history.append({"category": "electronics", "auction_type": "ENGLISH", "price": p, "quantity": 1, "at": 1})   # ~KES 1,000 per unit
    _, s = env.seller()
    _, b = env.bidder(ceiling=1_000_000)
    lot = env.english(s["agent_id"], product_spec={"category": "electronics", "title": "Bulk phones", "quantity": 100}, start_price=1000, reserve_price=1000)
    r = await env.orchestrator.consider(b["agent_id"], lot["auction_id"], manual=True)
    assert r["trigger"]["params"]["max_bid"] == 105_000                            # 1,000/unit x 100 units x 1.05, not 1,050


def test_only_the_verified_own_number_can_be_linked_to_whatsapp(env):
    ua, a = env.bidder(phone="+254711000777")
    with pytest.raises(AppError) as e:  # not verified yet
        env.agents.link_channel(a["agent_id"], "WHATSAPP", "+254711000777")
    assert e.value.code == "NUMBER_NOT_VERIFIED"
    env.store.users[ua["id"]]["phone_verified"] = True
    with pytest.raises(AppError):  # someone else's number
        env.agents.link_channel(a["agent_id"], "WHATSAPP", "+254799999999")
    linked = env.agents.link_channel(a["agent_id"], "WHATSAPP", "0711 000 777")
    assert linked["channel_identity_map"] == [{"channel": "WHATSAPP", "external_id": "254711000777"}]  # stored the way WhatsApp delivers it
    assert env.agents.find_by_channel("WHATSAPP", "+254711000777")["agent_id"] == a["agent_id"]


async def test_concurrent_wrong_passwords_are_all_counted_before_the_lockout_check(env):
    import asyncio
    u = env.agents.create_user(name="Target", password="password123")
    res = await asyncio.gather(*[env.agents.authenticate_async("Target", f"guess-{i}") for i in range(30)], return_exceptions=True)
    assert sum(isinstance(r, AppError) and r.code == "TOO_MANY_ATTEMPTS" for r in res) >= 20  # only the first few got a guess
    with pytest.raises(AppError):
        await env.agents.authenticate_async("Target", "password123")  # locked, even with the right password
    env.clock.advance(6 * 60_000)
    assert (await env.agents.authenticate_async("Target", "password123"))["id"] == u["id"]
    assert not env.store.settings["login_failures"].get("target")  # success clears the counter


async def test_changing_the_phone_cuts_the_old_numbers_control_of_the_agent(env):
    ua, a = env.bidder(phone="+254711000888")
    env.link_whatsapp(a, "+254711000888")
    env.agents.update(a["agent_id"], memory={"preferred_channel": "WHATSAPP"})
    assert (await env.router.handle_inbound("WHATSAPP", "254711000888", "status"))["agent_id"] == a["agent_id"]
    env.agents.set_phone(ua["id"], "0722 111 222")
    assert (await env.router.handle_inbound("WHATSAPP", "254711000888", "pause"))["agent_id"] is None  # the old number no longer commands the agent
    assert a["status"] == "ACTIVE" and a["channel_identity_map"] == [] and a["durable_memory"]["preferred_channel"] == "WEB"


async def test_whatsapp_replays_are_ignored_by_message_id(env):
    import json
    from kenyabidder.channels import handle_whatsapp_webhook
    _, b = env.bidder(ceiling=50_000, phone="+254711000321")
    env.link_whatsapp(b, "254711000321")
    body = json.dumps({"entry": [{"changes": [{"value": {"messages": [{"id": "wamid.ABC", "from": "254711000321", "text": {"body": "ceiling 60000"}}]}}]}]}).encode()
    first = await handle_whatsapp_webhook(env.router, body, None, secret="", allow_unsigned=True)
    assert first["handled"] == 1 and b["constraints"]["budget_ceiling"] == 60_000
    b["constraints"]["budget_ceiling"] = 1_000  # someone changes it back…
    again = await handle_whatsapp_webhook(env.router, body, None, secret="", allow_unsigned=True)  # …and the captured request is replayed
    assert again.get("duplicate") and b["constraints"]["budget_ceiling"] == 1_000


async def test_whatsapp_commands_are_marked_seen_only_after_they_ran_and_stale_deliveries_are_dropped(env, monkeypatch):
    import json
    from kenyabidder.channels import handle_whatsapp_webhook
    _, b = env.bidder(ceiling=50_000, phone="+254711000654")
    env.link_whatsapp(b, "254711000654")

    def payload(mid, text, ts=None):
        m = {"id": mid, "from": "254711000654", "text": {"body": text}}
        if ts is not None:
            m["timestamp"] = str(int(ts))
        return json.dumps({"entry": [{"changes": [{"value": {"messages": [m]}}]}]}).encode()
    real = env.router.handle_inbound
    calls = {"n": 0}

    async def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient failure")
        return await real(*a, **k)
    monkeypatch.setattr(env.router, "handle_inbound", flaky)
    with pytest.raises(RuntimeError):
        await handle_whatsapp_webhook(env.router, payload("wamid.1", "ceiling 70000"), None, secret="", allow_unsigned=True)
    assert "wamid.1" not in env.store.settings.get("wa_seen", [])  # it did not run, so Meta's retry must still be able to deliver it
    ok = await handle_whatsapp_webhook(env.router, payload("wamid.1", "ceiling 70000"), None, secret="", allow_unsigned=True)
    assert ok["handled"] == 1 and b["constraints"]["budget_ceiling"] == 70_000
    old = env.clock.now() / 1000 - 7200
    stale = await handle_whatsapp_webhook(env.router, payload("wamid.2", "ceiling 1"), None, secret="", allow_unsigned=True) if False else \
        await handle_whatsapp_webhook(env.router, payload("wamid.9", "ceiling 1", ts=old), None, secret="", allow_unsigned=True)
    assert stale.get("duplicate") and b["constraints"]["budget_ceiling"] == 70_000  # a captured request replayed hours later does nothing
