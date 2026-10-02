"""Trust and decision-quality insights built from data the platform already has: counterparty scorecards, quote comparison,
RFQ savings and plain-language explanations of why an agent was stopped.

Ideas taken from the market research (docs/market-research.md): Alibaba/Ariba-style quote comparison, response time as a trust signal,
and Pactum-style explainable guardrails.
"""
from __future__ import annotations

FINAL = ("COMPLETED", "FELL_THROUGH", "NO_RESPONSE")

EXPLAIN = {
    "CEILING_EXCEEDED": "The amount was above this agent's budget ceiling. Raise the ceiling in Rules if you want it to go higher.",
    "BELOW_FLOOR": "The quote was below this agent's price floor, so it was refused. Lower the floor in Rules if that price is acceptable.",
    "ESCALATION_THRESHOLD": "The amount is above the share of your ceiling that needs your approval — check your Inbox to approve or reject it.",
    "TYPE_NOT_AUTHORIZED": "This auction type isn't pre-authorised for autonomous bidding — approve it in your Inbox or tick the type in Rules.",
    "MARKET_ANOMALY": "The price looked abnormal against recent clearing prices, so autonomous bidding paused for this category. Review and bid manually if it is right.",
    "TIER_LIMIT": "New accounts have a per-bid limit until they complete a few deals. It lifts automatically as you build a track record.",
    "RATE_LIMIT": "The agent hit its per-minute action limit; it resumes by itself.",
    "NOT_A_BIDDER": "Only buyer agents can bid on this kind of auction.",
    "NOT_A_SUPPLIER": "Only seller (supplier) agents can quote on an RFQ.",
    "AGENT_NOT_ACTIVE": "The agent is paused or suspended. Resume it from its page.",
    "AUCTION_NOT_OPEN": "The auction had already closed.",
    "VERIFIED_ONLY": "This listing accepts verified businesses only. Apply for the badge in Profile.",
    "VERIFICATION_REQUIRED": "Amounts above the platform's limit need a verified business. Apply for the badge in Profile.",
    "SELF_BID": "You can't bid on your own listing, or one posted from an account sharing your phone or email.",
    "PLATFORM_PAUSED": "The marketplace is paused by the administrators; things resume when they lift the pause.",
    "NO_TOKENS": "The agent's LLM needs tokens — top up in Wallet and it continues automatically.",
    "TOKEN_CAP": "The agent reached its daily token cap; it continues when the 24-hour window frees up.",
    "RATE_LIMITED": "The agent used its hourly allowance of LLM decisions; it resumes automatically.",
    "BID_TOO_LOW": "Someone outbid the agent between decision and action; it will try again if its limit allows.",
    "BID_TOO_HIGH": "A quote must be at most the current maximum; the agent will retry within its floor.",
    "BELOW_RESERVE": "A sealed bid must be at least the seller's reserve.",
    "PROHIBITED_ITEM": "That item is on the prohibited list and can't be traded here.",
}


def explain(code: str | None, fallback: str = "") -> str:
    return EXPLAIN.get(code or "", fallback or "The platform's safety rules stopped this action.")


class Insights:
    def __init__(self, store, clock):
        self.store, self.clock = store, clock

    # ---------------- scorecards ----------------

    MEMO_MS = 15_000

    def scorecards(self, agent_ids) -> dict[str, dict]:
        """Scorecards for several agents in ONE pass over matches, disputes and auctions (the auction page asks for every bidder every second)."""
        now = self.clock.now()
        memo = self.__dict__.setdefault("_memo", {})
        out, todo = {}, set()
        for aid in set(agent_ids):
            hit = memo.get(aid)
            if hit and now - hit[0] < self.MEMO_MS:
                out[aid] = hit[1]
            else:
                todo.add(aid)
        if todo:
            finals = {a: [] for a in todo}
            for m in self.store.matches.values():
                if m["status"] in FINAL:
                    for aid in (m["seller_agent_id"], m["buyer_agent_id"]):
                        if aid in finals:
                            finals[aid].append(m)
            disputes = dict.fromkeys(todo, 0)
            for d in self.store.disputes.values():
                for aid in (d["opened_by"], d["against"]):
                    if aid in disputes:
                        disputes[aid] += 1
            entered, wins, lags = dict.fromkeys(todo, 0), dict.fromkeys(todo, 0), {a: [] for a in todo}
            for a in self.store.auctions.values():
                first: dict[str, int] = {}
                for b in a["bids"]:
                    if b["agent_id"] in todo and b["agent_id"] not in first:
                        first[b["agent_id"]] = b["at"]
                for aid, at in first.items():
                    lags[aid].append(max(0, at - a["starts_at"]) / 1000)
                    if a["status"] == "SETTLED":
                        entered[aid] += 1
                        wins[aid] += (a["result"] or {}).get("winner_agent_id") == aid
            for aid in todo:
                agent = self.store.agents.get(aid) or {}
                rep = agent.get("reputation") or {}
                user = self.store.users.get(agent.get("principal_user_id") or "") or {}
                fm = finals[aid]
                done = sum(1 for m in fm if m["status"] == "COMPLETED")
                card = {"agent_id": aid, "tier": rep.get("tier", "NEW"), "score": rep.get("score"), "deals": len(fm), "completed": done,
                        "completion_rate": round(done / len(fm), 2) if fm else None, "at_fault": sum(1 for m in fm if aid in m["fault_agent_ids"]),
                        "disputes": disputes[aid], "win_rate": round(wins[aid] / entered[aid], 2) if entered[aid] else None,
                        "first_quote_s": round(sum(lags[aid]) / len(lags[aid])) if lags[aid] else None,
                        "verified": bool(user.get("verified_business")), "verified_name": (user.get("verified_business") or {}).get("name")}
                memo[aid] = (now, card)
                out[aid] = card
            if len(memo) > 5_000:
                memo.clear()
        return out

    def scorecard(self, agent_id: str) -> dict:
        return self.scorecards([agent_id])[agent_id]

    @staticmethod
    def badge_text(card: dict) -> str:
        bits = [card["tier"].lower()]
        if card["deals"]:
            bits.append(f"{card['deals']} deal{'s' if card['deals'] != 1 else ''}")
        if card["completion_rate"] is not None:
            bits.append(f"{round(card['completion_rate'] * 100)}% completed")
        if card["first_quote_s"] is not None:
            bits.append(f"replies in {card['first_quote_s']}s" if card["first_quote_s"] < 90 else f"replies in {round(card['first_quote_s'] / 60)}m")
        if card["verified"]:
            bits.append("✓ verified")
        return " · ".join(bits)

    # ---------------- quote comparison ----------------

    def comparison(self, view: dict, reverse: bool) -> list[dict]:
        """Rows for the bids a viewer is allowed to see (the engine's view already hides sealed bids), best price first, with who they are."""
        rows = []
        cards = self.scorecards({b["agent_id"] for b in view["bids"]})
        for b in view["bids"]:
            card = cards[b["agent_id"]]
            rows.append({"agent_id": b["agent_id"], "amount": b["amount"], "at": b["at"], "after_s": max(0, round((b["at"] - view["starts_at"]) / 1000)),
                         "card": card, "summary": self.badge_text(card)})
        rows.sort(key=lambda r: (r["amount"] if reverse else -r["amount"], r["at"]))
        return rows

    # ---------------- savings ----------------

    def rfq_savings(self, auction: dict) -> dict | None:
        r = auction.get("result") or {}
        if auction.get("direction") != "REVERSE" or r.get("outcome") != "SOLD":
            return None
        saved = auction["max_price"] - r["price"]
        return {"max_price": auction["max_price"], "price": r["price"], "saved": saved, "saved_pct": round(100 * saved / auction["max_price"], 1) if auction["max_price"] else 0.0}

    def platform_savings(self) -> dict:
        total = n = 0
        for a in self.store.auctions.values():
            s = self.rfq_savings(a)
            if s and not a.get("demo"):
                total, n = total + s["saved"], n + 1
        return {"rfqs_awarded": n, "total_saved_kes": total}
