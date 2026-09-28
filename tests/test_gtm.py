"""Go-to-market features found in the product review: phone/consent, user admin, evening summaries, demo data, terms."""
import pytest

from kenyabidder.demo import clear_demo, seed_demo
from kenyabidder.errors import AppError
from kenyabidder.terms import DEFAULT_TERMS, get_terms, set_terms
from kenyabidder.timeutil import EAT, eat


# ------------------------------------------------------------------ signup: phone, email, consent

def test_signup_normalizes_phone_validates_email_and_records_consent(env):
    u = env.agents.create_user(name="Wanjiku K", password="password123", phone="0712 345 678", email=" w@example.co.ke ")
    assert u["phone"] == "+254712345678" and u["email"] == "w@example.co.ke" and u["terms_accepted_at"] == env.clock.now() and u["terms_version"]
    with pytest.raises(AppError) as e:
        env.agents.create_user(name="Refuser", password="password123", accepted_terms=False)
    assert e.value.code == "TERMS_REQUIRED"
    for kw, code in (({"phone": "12345"}, "INVALID_PHONE"), ({"email": "not-an-email"}, "INVALID_EMAIL")):
        with pytest.raises(AppError) as e:
            env.agents.create_user(name="Someone", password="password123", **kw)
        assert e.value.code == code
    assert "Someone" not in [x["name"] for x in env.store.users.values()]


def test_set_phone_rejects_duplicates_and_claims_the_signup_grant(env):
    admin = env.agents.create_user(name="Boss", password="password123")
    llm = env.llms.add(name="C", provider="anthropic", model="m", api_key="k")
    env.billing.set_signup_grant(admin, llm["id"], 15_000)
    late = env.agents.create_user(name="No Phone Yet", password="password123")
    assert env.wallet.balance(late["id"], llm["id"]) == 0
    env.agents.set_phone(late["id"], "0733 111 222")            # adding a phone later claims the free trial tokens
    assert env.wallet.balance(late["id"], llm["id"]) == 15_000
    env.agents.set_phone(late["id"], "0733 111 222")            # idempotent
    assert env.wallet.balance(late["id"], llm["id"]) == 15_000
    other = env.agents.create_user(name="Other One", password="password123")
    with pytest.raises(AppError) as e:
        env.agents.set_phone(other["id"], "+254733111222")
    assert e.value.code == "PHONE_TAKEN"


# ------------------------------------------------------------------ user admin

def test_suspend_pauses_agents_blocks_login_and_protects_the_last_admin(env):
    admin = env.agents.create_user(name="Root Admin", password="password123")
    user, agent = env.bidder(name="Trouble")
    _, s = env.seller()
    a = env.english(s["agent_id"])
    t = env.execution.register_trigger(agent_id=agent["agent_id"], auction_id=a["auction_id"], kind="ENGLISH_INCREMENTAL", params={"max_bid": 5000, "snipe_window_ms": 5000})
    env.agents.set_suspended(user["id"], True, by=admin["id"])
    assert agent["status"] == "SUSPENDED" and t["status"] == "CANCELLED"
    with pytest.raises(AppError) as e:
        env.agents.authenticate("Trouble", "password123")
    assert e.value.code == "ACCOUNT_SUSPENDED"
    assert env.agents.authenticate("Trouble", "wrong-password") is None    # a wrong guess learns nothing about the status
    env.agents.set_suspended(user["id"], False, by=admin["id"])
    assert env.agents.authenticate("Trouble", "password123")["id"] == user["id"]
    with pytest.raises(AppError) as e:
        env.agents.set_suspended(admin["id"], True, by=admin["id"])
    assert e.value.code == "SELF_SUSPEND"
    other_admin = env.agents.create_user(name="Second Admin", password="password123", role="admin")
    env.agents.set_suspended(other_admin["id"], True, by=admin["id"])
    with pytest.raises(AppError) as e:
        env.agents.set_suspended(admin["id"], True, by="someone-else")
    assert e.value.code == "LAST_ADMIN"


def test_password_reset_and_change(env):
    env.agents.create_user(name="Root Admin", password="password123")
    u = env.agents.create_user(name="Forgetful", password="oldpassword1")
    for _ in range(5):
        env.agents.authenticate("Forgetful", "guess")
    with pytest.raises(AppError):
        env.agents.authenticate("Forgetful", "oldpassword1")     # locked out
    env.agents.admin_reset_password(u["id"], "temporary-pass1")  # also clears the lockout
    assert env.agents.authenticate("Forgetful", "temporary-pass1")
    with pytest.raises(AppError):
        env.agents.change_password(u["id"], "wrong", "brand-new-pass1")
    env.agents.change_password(u["id"], "temporary-pass1", "brand-new-pass1")
    assert env.agents.authenticate("Forgetful", "brand-new-pass1") and env.agents.authenticate("Forgetful", "temporary-pass1") is None
    with pytest.raises(AppError):
        env.agents.admin_reset_password(u["id"], "short")


# ------------------------------------------------------------------ evening summary

def test_daily_summary_goes_out_once_per_eat_evening_and_only_with_news(env):
    _, s = env.seller()
    _, quiet = env.seller()
    env.english(s["agent_id"], duration_ms=10 * 3600_000)
    day_start = env.clock.now() - env.clock.now() % (24 * 3600_000) - 3 * 3600_000   # 00:00 EAT of the current EAT day
    env.clock.t = day_start + 17 * 3600_000 + 59 * 60_000                             # 17:59 EAT
    assert env.sellers.send_daily_summaries() == 0
    env.clock.advance(2 * 60_000)                                                     # 18:01 EAT
    assert eat(env.clock.now()).hour == 18
    assert env.sellers.send_daily_summaries() == 1                                    # the quiet seller is skipped
    assert env.sellers.send_daily_summaries() == 0                                    # once per day
    msg = next(n for n in env.router.notifications_for(s["agent_id"]) if n["kind"] == "summary")
    assert "1 live listings" in msg["message"]
    assert not any(n["kind"] == "summary" for n in env.router.notifications_for(quiet["agent_id"]))
    env.clock.advance(24 * 3600_000)
    assert env.sellers.send_daily_summaries() == 1                                    # next evening
    env.agents.update(s["agent_id"], memory={"daily_summary": False})
    env.clock.advance(24 * 3600_000)
    assert env.sellers.send_daily_summaries() == 0                                    # opted out


def test_time_helpers_use_east_africa_time(env):
    assert eat(0).strftime("%H:%M") == "03:00" and EAT.utcoffset(None).total_seconds() == 3 * 3600


# ------------------------------------------------------------------ demo marketplace

async def test_demo_seed_gives_a_lively_market_that_agents_can_use_and_is_removable(env):
    _, real_buyer = env.bidder(ceiling=200_000, memory={"watch": {"category": "electronics"}})
    info = seed_demo(env.app, seed=1)
    assert info["sellers"] == 4 and info["listings"] == 8
    with pytest.raises(AppError) as e:
        seed_demo(env.app)
    assert e.value.code == "DEMO_EXISTS"
    live = env.engine.list_active_auctions()
    assert len(live) == 8 and {a["auction_type"] for a in live} == {"ENGLISH", "DUTCH", "SECOND_PRICE_SEALED", "FIRST_PRICE_SEALED"}
    assert all(a["demo"] for a in live)
    assert env.intel.historical_clearing_prices("electronics")["count"] >= 8       # the heuristic has history to reason from
    demo_phone_auction = next(a for a in live if a["product_spec"]["category"] == "electronics" and a["auction_type"] == "ENGLISH")
    r = await env.orchestrator.consider(real_buyer["agent_id"], demo_phone_auction["auction_id"], manual=True)
    assert r["status"] == "PLANNED"
    env.clock.advance(7 * 3600_000)
    env.app.tick()
    assert env.store.matches == {}                                                   # demo sales never create real matches
    assert len(env.store.auctions) > 8 and all(a["demo"] for a in env.store.auctions.values())   # unsold lots were auto-relisted — still demo
    cleared = clear_demo(env.app)
    assert cleared["listings"] > 8 and cleared["users"] == 4
    assert env.engine.list_active_auctions() == [] and env.intel.historical_clearing_prices("electronics")["count"] == 0
    assert real_buyer["agent_id"] in env.store.agents                                # real users survive
    assert env.execution.triggers_for(real_buyer["agent_id"]) == []


# ------------------------------------------------------------------ terms

def test_terms_are_editable_with_a_sensible_default(env):
    assert get_terms(env.store) == DEFAULT_TERMS
    assert "do NOT hold" in DEFAULT_TERMS and "BOTH sides confirm" in DEFAULT_TERMS and "LLM tokens" in DEFAULT_TERMS
    set_terms(env.store, "  Custom terms.  ")
    assert get_terms(env.store) == "Custom terms."
    set_terms(env.store, "")
    assert get_terms(env.store) == DEFAULT_TERMS
    with pytest.raises(AppError):
        set_terms(env.store, "x" * 20_001)


# ------------------------------------------------------------------ review round 3 regressions

def test_store_prune_bounds_everything_that_only_ever_grew(env):
    from kenyabidder.store import Store
    st = env.store
    for i in range(20_050):
        st.idempotency[f"k{i}"] = {"ok": True}
    st.audit.extend({"n": i} for i in range(50_100))
    st.settings["login_failures"] = {f"ghost{i}": [1] for i in range(100)}                     # long-expired lockout entries
    st.triggers["old"] = {"id": "old", "status": "DONE", "created_at": 0}
    st.triggers["live"] = {"id": "live", "status": "ACTIVE", "created_at": 0}
    st.approvals["old"] = {"id": "old", "status": "REJECTED", "created_at": 0, "resolved_at": 1}
    st.prune(env.clock.now())
    assert len(st.idempotency) == 20_000 and "k0" not in st.idempotency and "k20049" in st.idempotency
    assert len(st.audit) == 50_000 and st.audit[-1] == {"n": 50_099}
    assert st.settings["login_failures"] == {} and "old" not in st.triggers and "live" in st.triggers and "old" not in st.approvals
    assert isinstance(st, Store)


def test_idempotency_keeps_a_small_record_not_the_whole_auction(env):
    _, s = env.seller()
    _, b = env.bidder()
    a = env.english(s["agent_id"])
    env.engine.submit_bid(auction_id=a["auction_id"], agent_id=b["agent_id"], amount=1000, idempotency_key="k")
    saved = env.store.idempotency[f"{b['agent_id']}:k"]
    assert set(saved) == {"ok", "bid"}                                                            # no embedded auction view (was O(N^2) memory)
    assert env.engine.submit_bid(auction_id=a["auction_id"], agent_id=b["agent_id"], amount=1000, idempotency_key="k")["replayed"]


def test_login_failure_book_is_pruned_when_attackers_invent_names(env, monkeypatch):
    monkeypatch.setattr("kenyabidder.agents.verify_password", lambda *a: False)                # scrypt is slow; this test is about bookkeeping
    for i in range(2_100):
        env.agents.authenticate(f"invented-{i}", "x")
    env.clock.advance(6 * 60_000)
    env.agents.authenticate("one-more", "x")
    assert len(env.store.settings["login_failures"]) <= 2


def test_update_is_all_or_nothing(env):
    _, b = env.bidder(ceiling=1_000)
    with pytest.raises(AppError):
        env.agents.update(b["agent_id"], constraints={"budget_ceiling": 999_999}, memory={"preferred_channel": "WHATSAPP"}, config={"llm_id": "no-such-llm"})
    assert b["constraints"]["budget_ceiling"] == 1_000 and b["durable_memory"]["preferred_channel"] == "WEB"   # nothing half-applied


def test_anthropic_usage_includes_cache_tokens():
    import asyncio, json
    import httpx2
    from kenyabidder.llm.providers import AnthropicProvider
    handler = lambda req: httpx2.Response(200, json={"id": "m", "type": "message", "role": "assistant", "model": "x", "stop_reason": "end_turn", "content": [{"type": "text", "text": "hi"}],
                                                     "usage": {"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 3000, "cache_read_input_tokens": 500}})
    p = AnthropicProvider("k", "m", base_url="http://a.test", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    r = asyncio.run(p.complete("s", [{"role": "user", "content": "x"}], []))
    assert r.usage == {"input": 3600, "output": 20}                                              # cached tokens are processed tokens: bill them


def test_cannot_delete_an_llm_customers_have_paid_for(env):
    admin = env.agents.create_user(name="Boss", password="password123")
    llm = env.llms.add(name="Paid", provider="anthropic", model="m", api_key="k")
    u = env.agents.create_user(name="Customer One", password="password123")
    env.wallet.credit(u["id"], llm["id"], 5_000, "TOPUP", ref="o1")
    with pytest.raises(AppError) as e:
        env.llms.remove(llm["id"])
    assert e.value.code == "IN_USE" and "5,000 tokens" in e.value.message
    env.wallet.adjust(u["id"], llm["id"], -5_000)
    pack = env.billing.add_pack(admin, name="P", llm_id=llm["id"], tokens=5_000, price_kes=100)
    with pytest.raises(AppError) as e:
        env.llms.remove(llm["id"])
    assert "packs" in e.value.message
    env.billing.remove_pack(admin, pack["id"])
    env.llms.remove(llm["id"])
    assert llm["id"] not in env.store.llms


async def test_async_login_verifies_off_the_event_loop_and_behaves_identically(env):
    import asyncio, threading
    u = env.agents.create_user(name="Async Login", password="password123")
    seen = []
    import kenyabidder.agents as ag
    real = ag.verify_password
    ag.verify_password = lambda *a: (seen.append(threading.current_thread() is threading.main_thread()), real(*a))[1]
    try:
        assert (await env.agents.authenticate_async("async login", "password123"))["id"] == u["id"]
        assert await env.agents.authenticate_async("Async Login", "nope") is None
        assert await env.agents.authenticate_async("ghost", "nope") is None
    finally:
        ag.verify_password = real
    assert seen == [False, False, False]                                                        # never on the main (event-loop) thread
    for _ in range(4):
        await env.agents.authenticate_async("Async Login", "nope")
    with pytest.raises(AppError) as e:
        await env.agents.authenticate_async("Async Login", "password123")
    assert e.value.code == "TOO_MANY_ATTEMPTS"
