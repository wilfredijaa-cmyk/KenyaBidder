"""Agents mapped to metered LLMs operate only with tokens (spec: bidder/seller agents need sufficient tokens)."""
import pytest

from kenyabidder.errors import AppError
from kenyabidder.llm.providers import Completion, ScriptedProvider, ToolCall

WATCH = {"category": "electronics", "keywords": [], "min_quantity": 1}
ALL = ["ENGLISH", "DUTCH", "FIRST_PRICE_SEALED", "SECOND_PRICE_SEALED"]


def llm_bidder(env, llm, **kw):
    cfg = {"llm_id": llm["id"], "algorithms": {t: {"strategy": "llm", "llm_id": None} for t in ALL}}
    return env.bidder(ceiling=20_000, memory={"watch": WATCH}, config={**cfg, **kw.pop("config", {})}, **kw)


def bid(amount=3000):
    return Completion(tool_calls=[ToolCall("p", "propose_action", {"action": "BID", "max_bid": amount, "increment_pct": 0, "snipe_window_ms": 0, "reasoning": "go"})],
                      usage={"input": 900, "output": 100})


@pytest.fixture
def world(env):
    llm = env.llms.add(name="Claude", provider="anthropic", model="m", api_key="k", output_multiplier=5, cost_per_1k_kes=0.5)
    _, s = env.seller()
    u, b = llm_bidder(env, llm)
    env.llms.set_provider_override(llm["id"], ScriptedProvider([bid(), bid(), bid()]))
    return type("W", (), {"env": env, "llm": llm, "seller": s, "user": u, "agent": b})


async def test_agent_without_tokens_does_not_operate_and_owner_is_told_once(world):
    env, b = world.env, world.agent
    provider = env.llms._overrides[world.llm["id"]]
    a1 = env.english(world.seller["agent_id"])
    await env.orchestrator.idle()
    assert provider.requests == []                                   # the LLM was never even called
    assert env.execution.triggers_for(b["agent_id"]) == [] and not env.engine.get_auction(a1["auction_id"])["bids"]
    assert (b["agent_id"], a1["auction_id"]) in env.orchestrator.blocked
    notes = [n for n in env.router.notifications_for(b["agent_id"]) if n["kind"] == "no_tokens"]
    assert len(notes) == 1 and "Wallet" in notes[0]["message"]
    env.english(world.seller["agent_id"])                           # a second listing must not spam a second nudge
    await env.orchestrator.idle()
    assert len([n for n in env.router.notifications_for(b["agent_id"]) if n["kind"] == "no_tokens"]) == 1
    r = await env.orchestrator.consider(b["agent_id"], a1["auction_id"], manual=True)
    assert r["status"] == "BLOCKED" and r["code"] == "NO_TOKENS" and "top up" in r["reason"]


async def test_topup_wakes_the_agent_and_it_bids_and_is_charged(world):
    env, b, u = world.env, world.agent, world.user
    a = env.english(world.seller["agent_id"])
    await env.orchestrator.idle()
    assert not env.engine.get_auction(a["auction_id"])["bids"]
    admin = env.agents.create_user(name="Boss", password="password123")
    before = env.wallet.balance(u["id"], world.llm["id"])
    env.billing.admin_grant(admin, u["id"], world.llm["id"], 100_000, "top-up")   # → on_credit → retry_blocked
    await env.orchestrator.idle()
    await __import__("asyncio").sleep(0)                                        # let the scheduled retry run
    await env.orchestrator.idle()
    top = env.engine.get_auction(a["auction_id"])["bids"]
    assert top and top[-1]["agent_id"] == b["agent_id"]
    used = 900 + 100 * 5
    assert env.wallet.balance(u["id"], world.llm["id"]) == before + 100_000 - used
    assert (b["agent_id"], a["auction_id"]) not in env.orchestrator.blocked
    assert env.wallet.ledger(agent_id=b["agent_id"])[0]["meta"]["purpose"] == "bid_strategy"


async def test_tokens_are_per_llm(env):
    cheap = env.llms.add(name="Cheap", provider="anthropic", model="m", api_key="k")
    other = env.llms.add(name="Other", provider="anthropic", model="m", api_key="k")
    _, s = env.seller()
    u, b = llm_bidder(env, cheap)
    env.llms.set_provider_override(cheap["id"], ScriptedProvider([bid()]))
    env.wallet.credit(u["id"], other["id"], 10**7, "GRANT")           # plenty of tokens — but for a different model
    a = env.english(s["agent_id"])
    r = await env.orchestrator.consider(b["agent_id"], a["auction_id"], manual=True)
    assert r["status"] == "BLOCKED"


async def test_fallback_policy_uses_the_deterministic_heuristic_instead(world):
    env, b = world.env, world.agent
    env.billing.set_policy(env.agents.create_user(name="Boss", password="password123"), "fallback")
    a = env.english(world.seller["agent_id"])
    r = await env.orchestrator.consider(b["agent_id"], a["auction_id"], manual=True)
    assert r["status"] == "PLANNED" and "heuristic fallback" in r["reasoning"] and "not enough" in r["reasoning"]
    assert env.wallet.ledger(user_id=world.user["id"]) == []          # and nothing was charged
    with pytest.raises(AppError):
        env.billing.set_policy(None, "yolo")


async def test_deterministic_algorithms_never_need_tokens(env):
    llm = env.llms.add(name="Claude", provider="anthropic", model="m", api_key="k")
    _, s = env.seller()
    cfg = {"llm_id": llm["id"], "algorithms": {"ENGLISH": {"strategy": "baseline", "llm_id": None}}}   # LLM assigned but not used for ENGLISH
    u, b = env.bidder(ceiling=20_000, memory={"watch": WATCH}, config=cfg)
    a = env.english(s["agent_id"])
    await env.orchestrator.idle()
    assert env.engine.get_auction(a["auction_id"])["bids"][-1]["agent_id"] == b["agent_id"]
    assert env.meter.status_for_agent(b)["ok"]


async def test_running_out_mid_research_blocks_the_next_step_and_charges_only_what_was_used(world):
    env, b, u = world.env, world.agent, world.user
    llm = world.llm
    await env.mcps.refresh_tools("builtin")
    env.agents.update(b["agent_id"], config={"tools": ["builtin:get_demand_signal"]})
    need = env.meter.min_balance(llm)
    env.wallet.credit(u["id"], llm["id"], need + 500, "GRANT")        # enough for one call, not for two
    research = Completion(tool_calls=[ToolCall("r", "kb__get_demand_signal", {"category": "electronics"})], usage={"input": 1_000, "output": 100})
    env.llms.set_provider_override(llm["id"], ScriptedProvider([research, bid()]))
    a = env.english(world.seller["agent_id"])
    r = await env.orchestrator.consider(b["agent_id"], a["auction_id"], manual=True)
    assert r["status"] == "BLOCKED"
    assert env.wallet.balance(u["id"], llm["id"]) == need + 500 - (1_000 + 100 * 5)   # the research call was paid for
    assert env.wallet.verify_integrity() == []


async def test_daily_cap_blocks_then_retry_resumes_when_window_frees(world):
    env, b, u = world.env, world.agent, world.user
    env.agents.update(b["agent_id"], config={"max_tokens_per_day": 1_000})
    env.wallet.credit(u["id"], world.llm["id"], 10**6, "GRANT")
    a1 = env.english(world.seller["agent_id"])
    assert (await env.orchestrator.consider(b["agent_id"], a1["auction_id"], manual=True))["status"] == "PLANNED"   # uses 1,400 > cap
    a2 = env.english(world.seller["agent_id"])
    r = await env.orchestrator.consider(b["agent_id"], a2["auction_id"], manual=True)
    assert r["status"] == "BLOCKED" and r["code"] == "TOKEN_CAP"
    assert await env.orchestrator.retry_blocked() == 0                # still inside the 24h window
    env.clock.advance(25 * 3600_000)
    a3 = env.english(world.seller["agent_id"], duration_ms=3600_000)
    env.orchestrator.blocked[(b["agent_id"], a3["auction_id"])] = "TOKEN_CAP"
    assert await env.orchestrator.retry_blocked() == 1


async def test_seller_advisor_is_gated_too(env):
    llm = env.llms.add(name="Claude", provider="anthropic", model="m", api_key="k")
    _, s = env.seller(config={"llm_id": llm["id"], "advisor": {"strategy": "llm", "llm_id": None}})
    script = ScriptedProvider([Completion(tool_calls=[ToolCall("l", "propose_listing", {"auction_type": "ENGLISH", "reserve_price": 1500, "start_price": 1200, "reasoning": "adv"})],
                                          usage={"input": 500, "output": 50})])
    env.llms.set_provider_override(llm["id"], script)
    with pytest.raises(AppError) as e:                                # policy 'block': the seller is told to top up
        await env.advisor.recommend(s, "electronics", 5)
    assert e.value.code == "NO_TOKENS" and script.requests == []
    env.wallet.credit(s["principal_user_id"], llm["id"], 100_000, "GRANT")
    r = await env.advisor.recommend(s, "electronics", 5)
    assert r["auction_type"] == "ENGLISH" and r["source"].startswith("llm:")
    assert env.wallet.ledger(agent_id=s["agent_id"])[0]["meta"]["purpose"] == "listing_advice"
    env.billing.set_policy(env.agents.create_user(name="Boss", password="password123"), "fallback")
    env.wallet.adjust(s["principal_user_id"], llm["id"], -env.wallet.balance(s["principal_user_id"], llm["id"]))
    r = await env.advisor.recommend(s, "electronics", 5)
    assert r["source"] == "rules" and "rule-based fallback" in r["reasoning"]


async def test_concurrent_decisions_cannot_overspend_one_wallet(env):
    """Ten agents of one user decide at once with tokens for ~two calls: the holds must stop the rest."""
    import asyncio
    llm = env.llms.add(name="Claude", provider="anthropic", model="m", api_key="k")
    u = env.agents.create_user(name="Whale", password="password123")
    need = env.meter.min_balance(llm)
    env.wallet.credit(u["id"], llm["id"], need * 2 + 100, "GRANT")
    _, s = env.seller()
    cfg = {"llm_id": llm["id"], "algorithms": {"ENGLISH": {"strategy": "llm", "llm_id": None}}}
    agents = [env.app.agents.create_agent(user_id=u["id"], type="BIDDER", constraints={"budget_ceiling": 20_000}, memory={"watch": WATCH}, config=cfg) for _ in range(10)]

    class Slow(ScriptedProvider):
        async def complete(self, *a, **k):
            await asyncio.sleep(0.02)
            return Completion(tool_calls=[ToolCall("p", "propose_action", {"action": "SKIP", "reasoning": "x"})], usage={"input": 100, "output": 10})
    env.llms.set_provider_override(llm["id"], Slow())
    a = env.english(s["agent_id"])
    res = await asyncio.gather(*(env.orchestrator.consider(x["agent_id"], a["auction_id"], manual=True) for x in agents))
    done = [r for r in res if r["status"] != "BLOCKED"]
    assert 1 <= len(done) <= 2 and len(res) - len(done) >= 8
    assert env.wallet.balance(u["id"], llm["id"]) >= 0 and env.wallet.verify_integrity() == []
