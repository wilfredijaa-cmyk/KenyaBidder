"""Guardrail Interceptor (spec §7.3, Security): a deterministic rules engine — never an LLM.

Every proposed action passes through here before any state-changing call.
Decisions: APPROVED | REJECTED | ESCALATED.
"""
from __future__ import annotations

from .engine import is_open

DEFAULT_CONFIG = {
    "max_proposals_per_minute": 60,
    "low_stakes_ceiling": 1_000_000,  # max single bid for NEW-tier agents (§11.1)
    "anomaly_multiple": 3,
    "anomaly_min_samples": 5,
    "breaker_cooldown_ms": 10 * 60_000,
}


class GuardrailInterceptor:
    def __init__(self, store, clock, engine, intel, config: dict | None = None):
        self.store, self.clock, self.engine, self.intel = store, clock, engine, intel
        self.config = {**DEFAULT_CONFIG, **(config or {})}

    def evaluate(self, p: dict) -> dict:
        """p: {agent_id, auction_id, action, amount, source: strategy|user, approved_up_to?}"""
        def reject(code, reason, **extra):
            return {"decision": "REJECTED", "code": code, "reason": reason, **extra}

        def escalate(code, reason):
            return {"decision": "ESCALATED", "code": code, "reason": reason}

        now = self.clock.now()
        agent = self.store.agents.get(p["agent_id"])
        if not agent:
            return reject("AGENT_NOT_FOUND", "unknown agent", permanent=True)
        if agent["status"] != "ACTIVE":
            return reject("AGENT_NOT_ACTIVE", f"agent is {agent['status']}", permanent=True)
        if agent["agent_type"] != "BIDDER":
            return reject("NOT_A_BIDDER", "only bidder agents may bid", permanent=True)
        amount = p["amount"]
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            return reject("INVALID_AMOUNT", "amount must be a positive integer", permanent=True)
        c = agent["constraints"]
        if amount > c["budget_ceiling"]:
            return reject("CEILING_EXCEEDED", f"amount {amount} exceeds budget ceiling {c['budget_ceiling']}", permanent=True)
        a = self.store.auctions.get(p["auction_id"])
        if not a:
            return reject("AUCTION_NOT_FOUND", "unknown auction", permanent=True)
        if not is_open(a) and a["status"] != "SCHEDULED":
            return reject("AUCTION_NOT_OPEN", f"auction is {a['status']}", permanent=True)

        cat = a["product_spec"]["category"]
        if self._breaker_active(cat, now):
            return reject("MARKET_ANOMALY", "autonomous bidding halted for this category (circuit breaker)", permanent=True, notify=True)
        if self._is_anomalous(a, amount):
            self.store.breakers[cat.lower()] = {
                "reason": f"bid {amount} is more than {self.config['anomaly_multiple']}x the historical median",
                "tripped_at": now, "until": now + self.config["breaker_cooldown_ms"]}
            return reject("MARKET_ANOMALY", "price is anomalous versus recent clearing prices; autonomous bidding halted",
                          permanent=True, notify=True)

        one_off = agent["durable_memory"].get("one_off_authorizations", [])
        if (a["auction_type"] not in c["authorized_auction_types"] and a["auction_id"] not in one_off
                and p["source"] == "strategy"):
            return escalate("TYPE_NOT_AUTHORIZED", f"{a['auction_type']} auctions are not pre-authorized for autonomous bidding")

        tier = agent["reputation"]["tier"]
        if tier == "NEW" and amount > self.config["low_stakes_ceiling"]:
            return reject("TIER_LIMIT", f"NEW-tier agents are limited to {self.config['low_stakes_ceiling']} per bid until they complete matches", permanent=True)

        if p["source"] != "user":
            threshold = c["budget_ceiling"] * c["escalation_threshold_pct"] / 100
            if amount > threshold and not (p.get("approved_up_to", 0) >= amount):
                return escalate("ESCALATION_THRESHOLD", f"amount {amount} exceeds {c['escalation_threshold_pct']}% of the budget ceiling")

        window = [t for t in self.store.rate_windows.get(p["agent_id"], []) if now - t < 60_000]
        if len(window) >= self.config["max_proposals_per_minute"]:
            self.store.rate_windows[p["agent_id"]] = window
            return reject("RATE_LIMIT", f"more than {self.config['max_proposals_per_minute']} actions in the last minute")
        window.append(now)
        self.store.rate_windows[p["agent_id"]] = window
        return {"decision": "APPROVED", "code": "OK", "reason": "passed all guardrails"}

    def _is_anomalous(self, a: dict, amount: int) -> bool:
        if a["auction_type"] == "DUTCH":
            return False  # Dutch price follows the seller's own descending schedule
        s = self.intel.historical_clearing_prices(a["product_spec"]["category"])
        return (s["count"] >= self.config["anomaly_min_samples"] and s["median"] > 0
                and amount > s["median"] * self.config["anomaly_multiple"])

    def _breaker_active(self, category: str, now: int) -> bool:
        b = self.store.breakers.get(category.lower())
        if not b:
            return False
        if b["until"] <= now:
            del self.store.breakers[category.lower()]
            return False
        return True

    def reset_breaker(self, category: str) -> None:
        self.store.breakers.pop(category.lower(), None)
