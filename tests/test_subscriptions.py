"""Subscription plans: a pack that also buys days of perks (more agents, higher decision allowance)."""
import pytest

from kenyabidder.errors import AppError
from kenyabidder.subscriptions import DAY_MS


@pytest.fixture
def plan(env):
    admin = env.agents.create_user(name="Boss", password="password123")
    llm = env.llms.add(name="Claude", provider="anthropic", model="m", api_key="k")
    pack = env.billing.add_pack(admin, name="Pro", llm_id=llm["id"], tokens=500_000, price_kes=2000,
                                plan={"days": 30, "max_agents": 25, "decisions_per_hour": 240})
    user = env.agents.create_user(name="Customer", password="password123", phone="+254711000042")
    env.billing.dev_payments = True
    return type("P", (), {"env": env, "admin": admin, "llm": llm, "pack": pack, "user": user})


async def buy(p):
    o = await p.env.billing.checkout(p.user, p.pack["id"], "dev")
    return p.env.billing.get_order(o["id"])


def test_plan_validation(env):
    admin = env.agents.create_user(name="Boss", password="password123")
    llm = env.llms.add(name="Claude", provider="anthropic", model="m", api_key="k")
    for bad in ({"days": 0}, {"days": 30, "max_agents": 0}, {"days": "x"}, {"days": 30, "decisions_per_hour": 10**6}, "monthly"):
        with pytest.raises(AppError):
            env.billing.add_pack(admin, name="X", llm_id=llm["id"], tokens=1000, price_kes=100, plan=bad)
    assert env.billing.add_pack(admin, name="Plain", llm_id=llm["id"], tokens=1000, price_kes=100)["plan"] is None


async def test_paying_activates_the_plan_and_credits_tokens(plan):
    env = plan.env
    assert env.subscriptions.max_agents(plan.user["id"]) == 10 and not env.subscriptions.active(plan.user["id"])
    await buy(plan)
    s = env.subscriptions.active(plan.user["id"])
    assert s and s["plan_name"] == "Pro" and s["current_period_end"] == env.clock.now() + 30 * DAY_MS
    assert env.wallet.balance(plan.user["id"], plan.llm["id"]) == 500_000
    assert env.subscriptions.max_agents(plan.user["id"]) == 25 and env.subscriptions.decisions_per_hour(plan.user["id"]) == 240


async def test_the_plan_really_raises_the_agent_limit(plan):
    env = plan.env
    env.store.settings["max_agents_per_user"] = 2
    make = lambda: env.agents.create_agent(user_id=plan.user["id"], type="BIDDER", constraints={"budget_ceiling": 1000})  # noqa: E731
    make(), make()
    with pytest.raises(AppError) as e:
        make()
    assert e.value.code == "AGENT_LIMIT"
    await buy(plan)
    make()  # the plan lifts it to 25
    env.clock.advance(31 * DAY_MS)
    env.subscriptions.tick()
    with pytest.raises(AppError):
        make()  # …until it lapses (existing agents keep working; only new ones are limited)


async def test_buying_early_extends_and_reminders_and_expiry_are_sent_once(plan):
    env = plan.env
    _, agent = plan.user, env.agents.create_agent(user_id=plan.user["id"], type="BIDDER", constraints={"budget_ceiling": 1000})
    await buy(plan)
    first_end = env.subscriptions.active(plan.user["id"])["current_period_end"]
    env.clock.advance(28 * DAY_MS)
    assert env.subscriptions.tick() == 1 and env.subscriptions.tick() == 0  # one reminder, not one per tick
    await buy(plan)  # renewing before expiry stacks the days
    s = env.subscriptions.active(plan.user["id"])
    assert s["current_period_end"] == first_end + 30 * DAY_MS and s["renewals"] == 1
    env.clock.advance(60 * DAY_MS)
    env.subscriptions.tick()
    assert env.store.subscriptions[plan.user["id"]]["status"] == "EXPIRED" and not env.subscriptions.active(plan.user["id"])
    msgs = [n["message"] for n in env.router.notifications_for(agent["agent_id"])]
    assert any("ends on" in m for m in msgs) and any("has ended" in m for m in msgs)
    await buy(plan)  # re-subscribing after a lapse starts fresh
    assert env.subscriptions.active(plan.user["id"])["renewals"] == 0


async def test_decision_allowance_is_boosted_only_while_subscribed(plan):
    env = plan.env
    a = env.agents.create_agent(user_id=plan.user["id"], type="BIDDER", constraints={"budget_ceiling": 1000})
    assert env.meter.hourly_limit(a["agent_id"]) == 60
    await buy(plan)
    assert env.meter.hourly_limit(a["agent_id"]) == 240
    env.store.settings["max_llm_decisions_per_hour"] = 0  # an admin who set "unlimited" stays unlimited
    assert env.meter.hourly_limit(a["agent_id"]) == 0


async def test_refunding_a_plan_order_cancels_the_subscription(plan):
    env = plan.env
    o = await buy(plan)
    env.billing.admin_refund(plan.admin, o["id"], "customer request")
    assert env.store.subscriptions[plan.user["id"]]["status"] == "CANCELLED" and not env.subscriptions.active(plan.user["id"])


async def test_the_plan_bought_is_the_plan_delivered_even_if_the_pack_changes(plan):
    env = plan.env
    o = await env.billing.checkout(plan.user, plan.pack["id"], "dev")  # dev method pays immediately; edit the pack first for a manual-style flow
    env.billing.update_pack(plan.admin, plan.pack["id"], plan={"days": 1})
    assert env.subscriptions.active(plan.user["id"])["current_period_end"] == env.clock.now() + 30 * DAY_MS


async def test_refunding_one_order_takes_back_only_its_days(plan):
    env = plan.env
    o1 = await buy(plan)
    o2 = await buy(plan)  # stacked: 60 days
    s = env.subscriptions.active(plan.user["id"])
    assert s["current_period_end"] == env.clock.now() + 60 * DAY_MS
    env.billing.admin_refund(plan.admin, o1["id"], "duplicate purchase")
    s = env.subscriptions.active(plan.user["id"])
    assert s and s["current_period_end"] == env.clock.now() + 30 * DAY_MS and s["order_ids"] == [o2["id"]]
    env.billing.admin_refund(plan.admin, o2["id"], "customer request")
    assert env.store.subscriptions[plan.user["id"]]["status"] == "CANCELLED"


async def test_a_lost_activation_is_healed_by_recovery_and_by_repeat_payment_calls(plan):
    env = plan.env
    env.billing.on_paid = None  # simulate the process dying between the payment commit and the plan activation
    o = await buy(plan)
    assert not env.subscriptions.active(plan.user["id"]) and env.billing.get_order(o["id"])["status"] == "PAID"
    env.billing.on_paid = env.subscriptions.activate
    env.clock.advance(2 * DAY_MS)
    assert env.subscriptions.recover(env.billing.paid_plan_orders(0)) == 1
    s = env.subscriptions.active(plan.user["id"])
    assert s["current_period_end"] == env.clock.now() - 2 * DAY_MS + 30 * DAY_MS  # the days run from when it was PAID, not from now
    assert env.subscriptions.recover(env.billing.paid_plan_orders(0)) == 0  # idempotent
    env.store.subscriptions.clear()
    env.billing.mark_paid(o["id"], receipt=None, method="retry")  # a repeated payment callback heals it too
    assert env.subscriptions.active(plan.user["id"])


async def test_recovery_grants_nothing_for_an_order_whose_days_already_ran_out(plan):
    env = plan.env
    env.billing.on_paid = None
    await buy(plan)
    env.clock.advance(40 * DAY_MS)
    assert env.subscriptions.recover(env.billing.paid_plan_orders(0)) == 0 and not env.subscriptions.active(plan.user["id"])


def test_refund_reclaims_only_what_is_left_even_after_tokens_were_spent(env):
    admin = env.agents.create_user(name="Boss", password="password123")
    llm = env.llms.add(name="Claude", provider="anthropic", model="m", api_key="k")
    pack = env.billing.add_pack(admin, name="P", llm_id=llm["id"], tokens=100_000, price_kes=500)
    u = env.agents.create_user(name="Cust", password="password123")
    o = env.billing._insert_order(u, pack, "manual", None)
    env.billing.mark_paid(o["id"], receipt="ABC123", method="test")
    env.wallet.record_usage(u["id"], llm["id"], 60_000, agent_id="a", metered=True)
    r = env.billing.admin_refund(admin, o["id"], "changed my mind")
    assert r["status"] == "REFUNDED" and env.wallet.balance(u["id"], llm["id"]) == 0 and "reclaimed 40,000" in r["note"]
    assert env.wallet.verify_integrity() == []
