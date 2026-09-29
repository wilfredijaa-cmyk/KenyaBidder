"""Session Manager / LLM Orchestrator (spec §7.3, §11.3).

Reacts to auction events, runs the deterministic pre-filter, asks the agent's *mapped* strategy for a
proposal, and hands it to the Execution Engine as a conditional trigger.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from .engine import effective_end, is_open, is_reverse, poster_of

log = logging.getLogger("kenyabidder.orchestrator")


class Orchestrator:
    def __init__(self, store, clock, engine, intel, execution, strategies: dict, notify=None):
        self.store, self.clock, self.engine, self.intel, self.execution = store, clock, engine, intel, execution
        self.strategies = strategies  # {"baseline": ..., "heuristic": ..., "llm": ...}
        self.notify = notify or (lambda *a, **k: None)
        self.pending: set[asyncio.Task] = set()
        self.blocked: dict[tuple[str, str], str] = {}  # (agent, auction) -> reason code, waiting for tokens / cap window
        self._blocked_notified: dict[str, int] = {}
        engine.events.on("auction.created", self._on_created)

    def _on_created(self, auction_id, **_):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.debug("no running event loop; auto-bidding for %s skipped", auction_id)
            return
        t = loop.create_task(self.on_auction_created(auction_id))
        self.pending.add(t)
        t.add_done_callback(self._done)

    def spawn(self, coro) -> bool:
        """Run a coroutine in the background, tracked so idle()/shutdown can await it. No-op without a running loop."""
        try:
            t = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return False
        self.pending.add(t)
        t.add_done_callback(self._done)
        return True

    def _done(self, t: asyncio.Task) -> None:
        self.pending.discard(t)
        if not t.cancelled() and t.exception():
            log.error("orchestrator task failed", exc_info=t.exception())

    async def idle(self) -> None:
        """Await all in-flight strategy calls (tests / graceful shutdown)."""
        while self.pending:
            await asyncio.gather(*list(self.pending), return_exceptions=True)

    async def on_auction_created(self, auction_id: str) -> None:
        for agent in list(self.store.agents.values()):
            mem = agent["durable_memory"]
            a = self.store.auctions.get(auction_id)
            if not a or agent["agent_type"] != ("SELLER" if is_reverse(a) else "BIDDER") or agent["status"] != "ACTIVE" or not mem.get("auto_bid") or not mem.get("watch"):
                continue
            try:
                await self.consider(agent["agent_id"], auction_id)
            except Exception:  # noqa: BLE001  one agent's failure must not starve the others
                log.exception("consider failed for agent %s", agent["agent_id"])

    async def retry_blocked(self, user_id: str | None = None) -> int:
        """Re-run agents that were held back (no tokens / daily cap). Called after a top-up and periodically."""
        n = 0
        for (agent_id, auction_id), _code in list(self.blocked.items()):
            agent, a = self.store.agents.get(agent_id), self.store.auctions.get(auction_id)
            if not agent or not a or a["status"] not in ("ACTIVE", "EXTENDING", "SCHEDULED"):
                self.blocked.pop((agent_id, auction_id), None)  # gone, or the auction closed while waiting
                continue
            if user_id and agent["principal_user_id"] != user_id:
                continue
            try:
                r = await self.consider(agent_id, auction_id)
                n += r["status"] == "PLANNED"
            except Exception:  # noqa: BLE001
                log.exception("retry_blocked failed")
        return n

    async def reconsider_user(self, user_id: str) -> int:
        return await self.retry_blocked(user_id)

    def _note_blocked(self, agent: dict, a: dict, code: str, reason: str) -> None:
        self.blocked[(agent["agent_id"], a["auction_id"])] = code
        now = self.clock.now()
        if now - self._blocked_notified.get(agent["agent_id"], -10**18) >= 6 * 3600_000:  # one nudge per 6h, not one per auction
            self._blocked_notified[agent["agent_id"]] = now
            hint = {"NO_TOKENS": "Top up in Wallet and it will pick up the open auctions automatically.",
                    "RATE_LIMITED": "It resumes automatically when its hourly decision allowance frees up."}.get(code, "It resumes when its 24-hour window frees up.")
            self.notify(agent["agent_id"], "no_tokens", f"Your agent is waiting — {reason}. {hint}", auction_id=a["auction_id"])

    def prefilter(self, agent: dict, a: dict, *, spec: bool = True) -> str | None:
        """Deterministic pre-filter (§11.3). None if eligible, else a human-readable reason."""
        w = agent["durable_memory"].get("watch") if spec else None
        now = self.clock.now()
        if agent["status"] != "ACTIVE":
            return "agent not active"
        if not is_open(a) and a["status"] != "SCHEDULED":
            return f"auction is {a['status']}"
        if agent["agent_type"] != ("SELLER" if is_reverse(a) else "BIDDER"):
            return "this agent's type cannot take part in this kind of auction"
        poster = self.store.agents.get(poster_of(a))
        if poster and poster["principal_user_id"] == agent["principal_user_id"]:
            return "own listing"
        if w:
            s = a["product_spec"]
            if w.get("category") and w["category"].lower() != s["category"].lower():
                return "category mismatch"
            if w.get("min_quantity") and s["quantity"] < w["min_quantity"]:
                return "quantity below minimum"
            if w.get("deadline_at") and effective_end(a) > w["deadline_at"]:
                return "auction closes after the delivery deadline"
            if w.get("keywords"):
                text = f"{s['title']} {s.get('description', '')}".lower()
                if not any(k.lower() in text for k in w["keywords"]):
                    return "keywords do not match"
        if is_reverse(a):
            if self.engine.max_next_bid(a) < max(agent["constraints"].get("reserve_floor", 0), 1):
                return "the buyer's price is already below this agent's floor"
            return None
        opening = a["dutch"]["floor_price"] if a["auction_type"] == "DUTCH" else self.engine.min_next_bid(a, now)
        if opening > agent["constraints"]["budget_ceiling"]:
            return "cheapest possible price is above the budget ceiling"
        return None

    def strategy_for(self, agent: dict, auction_type: str):
        name = agent["config"]["algorithms"].get(auction_type, {}).get("strategy", "heuristic")
        return self.strategies.get(name) or self.strategies["heuristic"]

    async def consider(self, agent_id: str, auction_id: str, *, manual: bool = False) -> dict:
        """Run the agent's mapped strategy for an auction and register the resulting trigger.

        ``manual`` skips the watch-spec filter (the user explicitly asked) but never the hard checks.
        """
        agent, a = self.store.agents.get(agent_id), self.store.auctions.get(auction_id)
        if not agent or not a:
            return {"status": "ERROR", "reason": "unknown agent or auction"}
        key = f"{agent_id}:{auction_id}"
        if not manual and key in self.store.considered:
            return {"status": "SKIPPED", "reason": "already considered"}
        self.engine.get_auction(auction_id)  # advance lifecycle
        why = self.prefilter(agent, a, spec=not manual)
        if why:
            return {"status": "FILTERED", "reason": why}
        self.store.considered.add(key)
        one_off = agent["durable_memory"].setdefault("one_off_authorizations", [])
        if manual and auction_id not in one_off:
            one_off.append(auction_id)  # an explicit user request authorizes this one auction
        if not manual and a["auction_type"] not in agent["constraints"]["authorized_auction_types"] and auction_id not in one_off:
            return self._request_participation(agent, a)

        stats = self.intel.historical_clearing_prices(a["product_spec"]["category"])
        if is_reverse(a):  # what comparable RFQs cleared at, else the wider market
            rev = self.intel.historical_clearing_prices(a["product_spec"]["category"], direction="REVERSE")
            stats = rev if rev["count"] >= 3 else stats
        strat = self.strategy_for(agent, a["auction_type"])
        proposal = await strat.propose({"agent": agent, "auction": a, "intel": {"stats": stats}})

        if proposal["action"] == "BLOCKED":  # no tokens / cap reached and policy is 'block'
            self.store.considered.discard(key)  # so a top-up (or a freed cap window) can retry it
            self._note_blocked(agent, a, proposal["code"], proposal["reasoning"])
            return {"status": "BLOCKED", "reason": proposal["reasoning"], "code": proposal["code"], "strategy": strat.name}
        self.blocked.pop((agent_id, auction_id), None)

        # re-check after the (possibly slow) strategy call — the world may have moved on
        if agent["status"] != "ACTIVE":
            return {"status": "CANCELLED", "reason": "agent no longer active"}
        fresh = self.engine.get_auction(auction_id)
        if not is_open(fresh) and fresh["status"] != "SCHEDULED":
            return {"status": "FILTERED", "reason": "auction closed while deciding"}
        title = a["product_spec"]["title"]
        if proposal["action"] == "SKIP":
            self.notify(agent_id, "skip", f"Skipping \"{title}\": {proposal['reasoning']}", auction_id=auction_id)
            return {"status": "SKIPPED", "reason": proposal["reasoning"], "strategy": strat.name}
        trigger = self.execution.register_trigger(agent_id=agent_id, auction_id=auction_id, kind=proposal["kind"],
                                                  params=proposal["params"], reasoning=proposal["reasoning"], source="strategy")
        trigger["strategy"] = strat.name
        if proposal.get("trace"):
            trigger["trace"] = proposal["trace"]
        self.notify(agent_id, "plan", f"Plan for \"{title}\" ({a['auction_type']}, {strat.name}): {proposal['reasoning']}",
                    auction_id=auction_id, trigger_id=trigger["id"])
        return {"status": "PLANNED", "trigger": trigger, "reasoning": proposal["reasoning"], "strategy": strat.name}

    def _request_participation(self, agent: dict, a: dict) -> dict:
        ap = {"id": str(uuid.uuid4()), "kind": "PARTICIPATE", "agent_id": agent["agent_id"], "auction_id": a["auction_id"],
              "trigger_id": None, "proposal": None, "code": "TYPE_NOT_AUTHORIZED",
              "reason": f"{a['auction_type']} auctions are not pre-authorized for autonomous bidding",
              "status": "PENDING", "created_at": self.clock.now(), "resolved_at": None}
        self.store.approvals[ap["id"]] = ap
        short = ap["id"][:8]
        self.notify(agent["agent_id"], "approval_needed",
                    f"\"{a['product_spec']['title']}\" is a {a['auction_type']} auction, which you have not pre-authorized. Reply \"approve {short}\" to let the agent participate, or \"reject {short}\".",
                    approval_id=ap["id"], auction_id=a["auction_id"])
        return {"status": "APPROVAL_REQUESTED", "approval_id": ap["id"]}

    async def resolve_approval(self, approval_id: str, approve: bool) -> dict:
        """Resolve any approval kind: BID → executor; PARTICIPATE → start the agent's strategy."""
        ap = self.store.approvals.get(approval_id)
        if not ap or ap["kind"] != "PARTICIPATE":
            return self.execution.resolve_approval(approval_id, approve)
        if ap["status"] != "PENDING":
            return ap
        ap["status"], ap["resolved_at"] = ("APPROVED" if approve else "REJECTED"), self.clock.now()
        if approve:
            await self.consider(ap["agent_id"], ap["auction_id"], manual=True)
        return ap

    def find_approval(self, agent_id: str, id_or_prefix: str) -> dict | None:
        return next((a for a in self.store.approvals.values()
                     if a["agent_id"] == agent_id and a["status"] == "PENDING" and a["id"].startswith(id_or_prefix)), None)
