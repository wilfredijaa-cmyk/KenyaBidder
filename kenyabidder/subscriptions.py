"""Subscription plans: a token pack that also buys ``days`` of perks.

Payment is the same audited flow as any pack (M-Pesa STK / manual receipt). Renewal is a fresh purchase (Daraja has no
merchant-initiated card-style debit for most shortcodes): buying again while active *extends* the period, and the plan owner is
reminded three days before it lapses. A refunded plan order ends the subscription.
"""
from __future__ import annotations

DAY_MS = 24 * 3600_000
REMIND_BEFORE_MS = 3 * DAY_MS
DEFAULT_MAX_AGENTS = 10


class SubscriptionService:
    def __init__(self, store, clock, notify=None):
        self.store, self.clock = store, clock
        self.notify = notify or (lambda *a, **k: None)

    # ------------------------------------------------------------------ state

    def active(self, user_id: str) -> dict | None:
        s = self.store.subscriptions.get(user_id)
        return s if s and s["status"] == "ACTIVE" and s["current_period_end"] > self.clock.now() else None

    def activate(self, order: dict, *, at: int | None = None) -> dict | None:
        """Called once an order for a plan pack is PAID. Idempotent per order. ``at`` = when it was paid (recovery only)."""
        plan = order["data"]["plan"]
        now = self.clock.now()
        s = self.store.subscriptions.get(order["user_id"])
        if s and order["id"] in (s.get("seen_orders") or s["order_ids"]):
            return s  # already counted (even if its days have since lapsed): a duplicate callback must not grant them again
        if at is not None and at + plan["days"] * DAY_MS <= now:
            return None  # a recovered order whose days have already run out: nothing left to grant
        base = now if at is None else at
        if s and s["status"] == "ACTIVE" and s["current_period_end"] > now:
            s["current_period_end"] += plan["days"] * DAY_MS  # buying early extends, never wastes days
            s["renewals"] += 1
        else:
            s = {"user_id": order["user_id"], "started_at": base, "current_period_end": base + plan["days"] * DAY_MS, "renewals": 0,
                 "order_ids": [], "seen_orders": list((s or {}).get("seen_orders") or (s or {}).get("order_ids") or []), "reminded_for": None}
            self.store.subscriptions[order["user_id"]] = s
        s.update(status="ACTIVE", plan_name=order["pack_name"], plan=dict(plan), reminded_for=None)
        s["order_ids"] = [*s["order_ids"], order["id"]][-50:]
        s["seen_orders"] = [*(s.get("seen_orders") or []), order["id"]][-500:]
        self._tell(order["user_id"], f"Your {order['pack_name']} plan is active until {self._date(s['current_period_end'])}.")
        return s

    def cancel_for_order(self, order: dict) -> None:
        """A refund takes back only the days that order bought; other paid orders keep theirs."""
        s = self.store.subscriptions.get(order["user_id"])
        if not s or order["id"] not in s["order_ids"]:
            return
        s["order_ids"] = [i for i in s["order_ids"] if i != order["id"]]
        s["current_period_end"] -= order["data"]["plan"]["days"] * DAY_MS
        if not s["order_ids"] or s["current_period_end"] <= self.clock.now():
            s["status"] = "CANCELLED"
            self._tell(order["user_id"], "Your plan was cancelled after a refund.")
        else:
            self._tell(order["user_id"], f"A refund took back {order['data']['plan']['days']} days — your plan now ends on {self._date(s['current_period_end'])}.")

    def recover(self, paid_plan_orders: list[dict]) -> int:
        """Safety net: a PAID plan order whose activation was lost (crash between the payment commit and the plan) is activated now."""
        n = 0
        known = {i for s in self.store.subscriptions.values() for i in (s.get("seen_orders") or s["order_ids"])}
        for o in paid_plan_orders:
            if o["id"] not in known and o["data"].get("plan") and self.activate(o, at=o["updated_at"]):
                n += 1
        return n

    def tick(self) -> int:
        """Maintenance: expire lapsed plans, remind those about to lapse. Returns how many changed."""
        now, n = self.clock.now(), 0
        for uid, s in self.store.subscriptions.items():
            if s["status"] != "ACTIVE":
                continue
            if s["current_period_end"] <= now:
                s["status"] = "EXPIRED"
                self._tell(uid, f"Your {s['plan_name']} plan has ended. Renew in Wallet to keep your extra agents and higher decision allowance.")
                n += 1
            elif s["current_period_end"] - now <= REMIND_BEFORE_MS and s.get("reminded_for") != s["current_period_end"]:
                s["reminded_for"] = s["current_period_end"]
                self._tell(uid, f"Your {s['plan_name']} plan ends on {self._date(s['current_period_end'])}. Renew in Wallet to keep it running.")
                n += 1
        return n

    # ------------------------------------------------------------------ perks

    def max_agents(self, user_id: str) -> int:
        base = self.store.settings.get("max_agents_per_user", DEFAULT_MAX_AGENTS)  # 0 = unlimited
        s = self.active(user_id)
        perk = (s or {}).get("plan", {}).get("max_agents")
        if not base:
            return 0
        return max(base, perk) if perk else base

    def decisions_per_hour(self, user_id: str) -> int | None:
        s = self.active(user_id)
        return (s or {}).get("plan", {}).get("decisions_per_hour")

    # ------------------------------------------------------------------ helpers

    def _date(self, ms: int) -> str:
        from .timeutil import eat
        return eat(ms).strftime("%d %b %Y")

    def _tell(self, user_id: str, text: str) -> None:
        agents = sorted((a for a in self.store.agents.values() if a["principal_user_id"] == user_id), key=lambda a: a["created_at"])
        if agents:
            self.notify(agents[0]["agent_id"], "tokens", text)
