"""Guardrail Interceptor (spec §7.3, Security): a deterministic rules engine — never an LLM.

Every proposed action passes through here before any state-changing call.
Decisions: APPROVED | REJECTED | ESCALATED.
"""
from __future__ import annotations

from .engine import is_open, is_reverse

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
        self._stats: dict[str, tuple[int, tuple[int, int], dict]] = {}

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
        a = self.store.auctions.get(p["auction_id"])
        if not a:
            return reject("AUCTION_NOT_FOUND", "unknown auction", permanent=True)
        reverse = is_reverse(a)  # RFQ: SELLER agents quote the price down; forward: BIDDER agents bid it up
        if agent["agent_type"] != ("SELLER" if reverse else "BIDDER"):
            return reject("NOT_A_SUPPLIER" if reverse else "NOT_A_BIDDER", "only supplier agents may quote on an RFQ" if reverse else "only bidder agents may bid", permanent=True)
        amount = p["amount"]
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            return reject("INVALID_AMOUNT", "amount must be a positive integer", permanent=True)
        c = agent["constraints"]
        if reverse:
            if amount < c.get("reserve_floor", 0):  # the supplier's own floor: never quote below what they can afford to deliver at
                return reject("BELOW_FLOOR", f"quote {amount} is below this agent's price floor {c['reserve_floor']}", permanent=True)
        elif amount > c["budget_ceiling"]:
            return reject("CEILING_EXCEEDED", f"amount {amount} exceeds budget ceiling {c['budget_ceiling']}", permanent=True)
        if not is_open(a) and a["status"] != "SCHEDULED":
            return reject("AUCTION_NOT_OPEN", f"auction is {a['status']}", permanent=True)

        cat = a["product_spec"]["category"]
        # The breaker protects AUTONOMOUS bidding. A human's explicit bid is theirs to make (and must never halt everyone else's agents).
        # Rejections are NOT permanent: the trigger stays live and resumes by itself when the cooldown ends.
        if p["source"] != "user" and not reverse:  # the anomaly logic models buyers overpaying; suppliers are bounded by their floor
            if self._breaker_active(cat, now):
                return reject("MARKET_ANOMALY", "autonomous bidding halted for this category (circuit breaker)", notify=True)
            if self._is_anomalous(a, amount):
                if not a["bids"] and amount <= self.engine.min_next_bid(a, now):
                    # The agent is only matching the seller's own ask. That is one odd listing, not a market-wide event: refuse this lot
                    # and leave every other agent's bidding alone (otherwise a single absurd listing could freeze a whole category).
                    return reject("PRICE_ANOMALOUS", "the asking price is far above recent clearing prices for this category — not bidding on this lot", permanent=True)
                self.store.breakers[cat.lower()] = {
                    "reason": f"bid {amount} is more than {self.config['anomaly_multiple']}x the typical per-unit price",
                    "tripped_at": now, "until": now + self.config["breaker_cooldown_ms"]}
                return reject("MARKET_ANOMALY", "price is anomalous versus recent clearing prices; autonomous bidding halted", notify=True)

        one_off = agent["durable_memory"].get("one_off_authorizations", [])
        if (a["auction_type"] not in c["authorized_auction_types"] and a["auction_id"] not in one_off
                and p["source"] == "strategy"):
            return escalate("TYPE_NOT_AUTHORIZED", f"{a['auction_type']} auctions are not pre-authorized for autonomous bidding")

        tier = agent["reputation"]["tier"]
        if reverse:
            return self._rate_limit(p["agent_id"], now)
        if tier == "NEW" and amount > self.config["low_stakes_ceiling"]:
            return reject("TIER_LIMIT", f"NEW-tier agents are limited to {self.config['low_stakes_ceiling']} per bid until they complete matches", permanent=True)

        if p["source"] != "user":
            threshold = c["budget_ceiling"] * c["escalation_threshold_pct"] / 100
            if amount > threshold and not (p.get("approved_up_to", 0) >= amount):
                return escalate("ESCALATION_THRESHOLD", f"amount {amount} exceeds {c['escalation_threshold_pct']}% of the budget ceiling")

        return self._rate_limit(p["agent_id"], now)

    def _rate_limit(self, agent_id: str, now: int) -> dict:
        window = [t for t in self.store.rate_windows.get(agent_id, []) if now - t < 60_000]
        if len(window) >= self.config["max_proposals_per_minute"]:
            self.store.rate_windows[agent_id] = window
            return {"decision": "REJECTED", "code": "RATE_LIMIT", "reason": f"more than {self.config['max_proposals_per_minute']} actions in the last minute"}
        window.append(now)
        self.store.rate_windows[agent_id] = window
        return {"decision": "APPROVED", "code": "OK", "reason": "passed all guardrails"}

    def soft_cap(self, agent: dict, approved_up_to: int = 0) -> int:
        """Highest amount an autonomous bid can take WITHOUT being rejected or escalated. Strategies clamp their *stepped*
        bids to this so a 20% step past the ceiling becomes 'bid the ceiling', not a permanently blocked plan."""
        c = agent["constraints"]
        cap = c["budget_ceiling"]
        if agent["reputation"]["tier"] == "NEW":
            cap = min(cap, self.config["low_stakes_ceiling"])
        return max(min(cap, int(c["budget_ceiling"] * c["escalation_threshold_pct"] / 100)), min(cap, approved_up_to))

    def _is_anomalous(self, a: dict, amount: int) -> bool:
        if a["auction_type"] == "DUTCH":
            return False  # Dutch price follows the seller's own descending schedule
        cat, now = a["product_spec"]["category"].lower(), self.clock.now()
        hit = self._stats.get(cat)
        if hit and now - hit[0] < 30_000 and hit[1] == (len(self.store.market_history), self.store.market_history.dropped):  # a sort over up to 20k rows per bid is too much in a bidding war
            s = hit[2]
        else:
            s = self.intel.historical_clearing_prices(cat)
            if len(self._stats) > 200:
                self._stats.clear()
            self._stats[cat] = (now, (len(self.store.market_history), self.store.market_history.dropped), s)
        if s["count"] < self.config["anomaly_min_samples"] or not s["unit_median"]:
            return False
        qty = max(1, a["product_spec"]["quantity"])  # compare PER UNIT: a 100-unit lot is not "anomalous" next to single-unit history
        return amount / qty > s["unit_median"] * self.config["anomaly_multiple"]

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
