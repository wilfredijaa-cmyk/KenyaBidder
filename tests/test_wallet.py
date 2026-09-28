import asyncio
import sqlite3

import pytest

from kenyabidder.errors import AppError
from kenyabidder.llm.providers import Completion, LlmError, ScriptedProvider, ToolCall, ToolSpec
from kenyabidder.wallet import AgentTokenCap, InsufficientTokens, WalletDB, estimate_tokens


def add_llm(env, name="Claude", **kw):
    return env.llms.add(name=name, provider="anthropic", model="claude-test", api_key="sk-x", **kw)


def test_credit_debit_and_balance_invariants(env):
    w = env.wallet
    u, l = "user-1", "llm-1"
    assert w.balance(u, l) == 0
    e = w.credit(u, l, 10_000, "TOPUP", ref="order-1")
    assert e["balance_after"] == 10_000 and not e.get("duplicate")
    dup = w.credit(u, l, 10_000, "TOPUP", ref="order-1")  # replayed webhook / double click
    assert dup["duplicate"] and dup["entry_id"] == e["entry_id"] and w.balance(u, l) == 10_000
    w.record_usage(u, l, 3_000, agent_id="a1", metered=True)
    assert w.balance(u, l) == 7_000
    over = w.record_usage(u, l, 9_000, agent_id="a1", metered=True)  # never overdraws
    assert w.balance(u, l) == 0 and over["tokens"] == -7_000 and over["used"] == 9_000 and over["meta"]["shortfall"] == 2_000
    assert w.verify_integrity() == []
    for bad in (0, -5, 1.5, True):
        with pytest.raises(AppError):
            w.credit(u, l, bad, "TOPUP")
    with pytest.raises(AppError):
        w.credit(u, l, 5, "USAGE")


def test_database_refuses_a_negative_balance_even_if_code_is_wrong(env):
    w = env.wallet
    w.credit("u", "l", 100, "GRANT")
    with pytest.raises(sqlite3.IntegrityError):
        w.conn.execute("UPDATE balances SET balance = -1 WHERE user_id='u'")
    with pytest.raises(AppError):
        w.adjust("u", "l", -101)
    assert w.adjust("u", "l", -100)["balance_after"] == 0


def test_free_usage_is_recorded_without_touching_the_balance(env):
    w = env.wallet
    w.credit("u", "l", 500, "GRANT")
    e = w.record_usage("u", "l", 1_234, agent_id="a", metered=False)
    assert e["tokens"] == 0 and e["used"] == 1_234 and w.balance("u", "l") == 500
    assert w.used_since("a", 0) == 1_234


def test_ledger_queries_and_reports(env):
    w = env.wallet
    w.credit("u1", "L", 1000, "TOPUP", ref="o1")
    w.credit("u1", "L", 200, "GRANT", ref="g1")
    w.record_usage("u1", "L", 300, agent_id="a1", metered=True)
    w.record_usage("u1", "L", 100, agent_id="a2", metered=True)
    assert [e["kind"] for e in w.ledger(user_id="u1")] == ["USAGE", "USAGE", "GRANT", "TOPUP"]
    assert len(w.ledger(agent_id="a1")) == 1 and len(w.ledger(kind="TOPUP")) == 1
    assert w.usage_by_agent("u1")[0] == {"agent_id": "a1", "llm_id": "L", "calls": 1, "used": 300}
    assert w.token_totals()["L"] == {"sold": 1000, "granted": 200, "used": 400, "adjusted": 0, "refunded": 0}
    assert w.outstanding_liability() == {"L": 800}
    assert w.recent_usage_avg("a1") == 300


def test_ledger_survives_reopen_and_matches_balances(tmp_path, env):
    path = tmp_path / "wallet.db"
    w = WalletDB(path, env.clock)
    w.credit("u", "L", 5_000, "TOPUP", ref="o1")
    w.record_usage("u", "L", 1_200, agent_id="a", metered=True)
    w.close()
    again = WalletDB(path, env.clock)  # simulates a crash/restart
    assert again.balance("u", "L") == 3_800 and len(again.ledger(user_id="u")) == 2
    assert again.credit("u", "L", 5_000, "TOPUP", ref="o1")["duplicate"]  # idempotency survives restarts
    assert again.verify_integrity() == []
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_integrity_check_detects_tampering(env):
    w = env.wallet
    w.credit("u", "L", 100, "TOPUP", ref="x")
    w.conn.execute("UPDATE balances SET balance = 999 WHERE user_id='u'")
    assert w.verify_integrity()


def test_transaction_rolls_back_atomically(env):
    w = env.wallet
    with pytest.raises(RuntimeError):
        with w.tx() as c:
            w.credit("u", "L", 100, "TOPUP", ref="r", c=c)
            raise RuntimeError("boom")
    assert w.balance("u", "L") == 0 and not w.ledger(user_id="u")


# ------------------------------------------------------------------ metering

def test_reservations_prevent_concurrent_overspend(env):
    llm = add_llm(env)
    u, _ = env.bidder()
    env.wallet.credit(u["id"], llm["id"], 10_000, "GRANT")
    r1 = env.meter.reserve(u["id"], llm, 6_000)
    with pytest.raises(InsufficientTokens) as e:  # second in-flight call cannot spend the same tokens
        env.meter.reserve(u["id"], llm, 6_000)
    assert e.value.code == "NO_TOKENS" and e.value.status == 402 and e.value.available == 4_000
    r1.release()
    r2 = env.meter.reserve(u["id"], llm, 6_000)
    r2.settle(2_500)
    assert env.wallet.balance(u["id"], llm["id"]) == 7_500 and env.meter.available(u["id"], llm["id"]) == 7_500
    r2.settle(999)  # settling twice is a no-op
    assert env.wallet.balance(u["id"], llm["id"]) == 7_500


def test_free_llm_needs_no_tokens_but_usage_is_tracked(env):
    llm = add_llm(env, billing_mode="free")
    u, b = env.bidder()
    res = env.meter.reserve(u["id"], llm, 10**9, b["agent_id"])
    res.settle(700)
    assert env.wallet.used_since(b["agent_id"], 0) == 700 and env.wallet.balance(u["id"], llm["id"]) == 0


def test_min_balance_uses_output_multiplier(env):
    cheap, dear = add_llm(env, name="C"), add_llm(env, name="D", output_multiplier=5)
    assert env.meter.min_balance(dear) > env.meter.min_balance(cheap)
    assert env.meter.min_balance(dear) == 2_000 + dear["max_tokens"] * 5


async def test_metered_provider_charges_reported_usage_with_output_multiplier(env):
    llm = add_llm(env, output_multiplier=5)
    u, b = env.bidder()
    env.wallet.credit(u["id"], llm["id"], 50_000, "GRANT")
    inner = ScriptedProvider([Completion(text="hi", usage={"input": 1_000, "output": 200})])
    mp = env.meter.wrap(inner, llm, b, "test")
    await mp.complete("sys", [{"role": "user", "content": "x"}], [])
    assert env.wallet.balance(u["id"], llm["id"]) == 50_000 - (1_000 + 200 * 5)
    row = env.wallet.ledger(agent_id=b["agent_id"])[0]
    assert row["used"] == 2_000 and row["meta"]["in"] == 1_000 and row["meta"]["out"] == 200 and row["meta"]["purpose"] == "test"


async def test_missing_usage_is_charged_as_a_pessimistic_estimate_never_zero(env):
    llm = add_llm(env)
    u, b = env.bidder()
    env.wallet.credit(u["id"], llm["id"], 50_000, "GRANT")
    inner = ScriptedProvider([Completion(text="a" * 300, usage={})])
    await env.meter.wrap(inner, llm, b, "t").complete("s" * 300, [{"role": "user", "content": "u" * 300}], [])
    assert env.wallet.used_since(b["agent_id"], 0) > 0 and env.wallet.balance(u["id"], llm["id"]) < 50_000


async def test_failed_or_cancelled_calls_are_not_charged_and_holds_released(env):
    llm = add_llm(env)
    u, b = env.bidder()
    env.wallet.credit(u["id"], llm["id"], 20_000, "GRANT")
    mp = env.meter.wrap(ScriptedProvider([LlmError("HTTP 500")]), llm, b, "t")
    with pytest.raises(LlmError):
        await mp.complete("s", [{"role": "user", "content": "x"}], [])
    assert env.wallet.balance(u["id"], llm["id"]) == 20_000 and env.meter.available(u["id"], llm["id"]) == 20_000

    class Slow:
        async def complete(self, *a, **k):
            await asyncio.sleep(5)
    mp = env.meter.wrap(Slow(), llm, b, "t")
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await mp.complete("s", [{"role": "user", "content": "x"}], [])
    assert env.wallet.balance(u["id"], llm["id"]) == 20_000 and env.meter.available(u["id"], llm["id"]) == 20_000


async def test_insufficient_tokens_blocks_before_the_provider_is_called(env):
    llm = add_llm(env)
    u, b = env.bidder()
    inner = ScriptedProvider([Completion(text="should never run")])
    with pytest.raises(InsufficientTokens):
        await env.meter.wrap(inner, llm, b, "t").complete("s", [{"role": "user", "content": "x"}], [])
    assert inner.requests == [] and env.wallet.ledger(user_id=u["id"]) == []


async def test_daily_agent_cap(env):
    llm = add_llm(env)
    u, b = env.bidder(config={"llm_id": llm["id"], "algorithms": {"ENGLISH": {"strategy": "llm", "llm_id": None}}, "max_tokens_per_day": 5_000})
    env.wallet.credit(u["id"], llm["id"], 1_000_000, "GRANT")
    ok = lambda: Completion(text="x", usage={"input": 3_000, "output": 0})
    mp = env.meter.wrap(ScriptedProvider([ok(), ok(), ok()]), llm, b, "t")
    await mp.complete("s", [{"role": "user", "content": "x"}], [])      # 3,000 used
    await mp.complete("s", [{"role": "user", "content": "x"}], [])      # 6,000 used: crossed the cap on this call
    with pytest.raises(AgentTokenCap) as e:
        await mp.complete("s", [{"role": "user", "content": "x"}], [])
    assert e.value.code == "TOKEN_CAP" and e.value.used == 6_000 and e.value.cap == 5_000
    env.clock.advance(24 * 3600_000 + 1)                                # rolling 24h window frees the agent
    await mp.complete("s", [{"role": "user", "content": "x"}], [])
    with pytest.raises(AppError):
        env.agents.update(b["agent_id"], config={"max_tokens_per_day": 10})  # nonsense caps are refused


def test_agent_token_status(env):
    llm = add_llm(env)
    u, b = env.bidder(config={"llm_id": llm["id"], "algorithms": {"ENGLISH": {"strategy": "llm", "llm_id": None}}})
    st = env.meter.status_for_agent(b)
    assert not st["ok"] and st["llms"][0]["min_needed"] == env.meter.min_balance(llm) and st["llms"][0]["balance"] == 0
    env.wallet.credit(u["id"], llm["id"], env.meter.min_balance(llm), "GRANT")
    assert env.meter.status_for_agent(b)["ok"]
    _, plain = env.bidder()  # a deterministic-only agent never needs tokens
    assert env.meter.status_for_agent(plain) == {"ok": True, "llms": [], "cap": None, "used_24h": 0, "cap_reached": False, "avg_decision_tokens": None}


async def test_low_balance_alert_fires_once(env):
    llm = add_llm(env)
    u, b = env.bidder()
    need = env.meter.min_balance(llm)
    env.wallet.credit(u["id"], llm["id"], need * 4, "GRANT")
    mp = env.meter.wrap(ScriptedProvider([Completion(text="x", usage={"input": need, "output": 0})] * 3), llm, b, "t")
    await mp.complete("s", [{"role": "user", "content": "x"}], [])   # balance 3*need — not low yet
    assert not any(n["kind"] == "low_tokens" for n in env.router.notifications_for(b["agent_id"]))
    await mp.complete("s", [{"role": "user", "content": "x"}], [])   # 2*need < 3*need -> alert
    await mp.complete("s", [{"role": "user", "content": "x"}], [])
    assert sum(n["kind"] == "low_tokens" for n in env.router.notifications_for(b["agent_id"])) == 1


def test_llm_billing_field_validation(env):
    for kw in ({"billing_mode": "gift"}, {"output_multiplier": 0}, {"output_multiplier": 1.5}, {"output_multiplier": 99}, {"cost_per_1k_kes": -1}):
        with pytest.raises(AppError):
            add_llm(env, name="Bad", **kw)
    e = add_llm(env, name="Priced", output_multiplier=5, cost_per_1k_kes=0.8)
    assert e["billing_mode"] == "metered" and e["output_multiplier"] == 5


def test_estimate_tokens_is_pessimistic():
    assert estimate_tokens("a" * 300) == 101
