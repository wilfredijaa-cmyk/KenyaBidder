"""Deterministic Execution Engine (spec §7.2).

Holds pre-approved conditional actions ("triggers") and fires them the instant
conditions are met. No LLM call is ever made from this module — that is the point.

Trigger kinds:
  ENGLISH_INCREMENTAL {max_bid, increment_pct, snipe_window_ms}
  DUTCH_ACCEPT        {threshold}        accept when price <= threshold
  SEALED_BID          {amount}
  REVERSE_UNDERCUT    {min_price, decrement_pct, snipe_window_ms}   supplier: undercut the best quote, never below min_price
  REVERSE_SEALED_BID  {amount}                                      supplier: one sealed quote
"""
from __future__ import annotations

import logging
import math
import time
import uuid

from .engine import effective_end, is_open, is_reverse
from .insights import explain
from .errors import bad, forbidden, not_found

log = logging.getLogger("kenyabidder.execution")

KIND_FOR_TYPE = {"ENGLISH": "ENGLISH_INCREMENTAL", "DUTCH": "DUTCH_ACCEPT",
                 "FIRST_PRICE_SEALED": "SEALED_BID", "SECOND_PRICE_SEALED": "SEALED_BID",
                 "REVERSE_ENGLISH": "REVERSE_UNDERCUT", "REVERSE_SEALED": "REVERSE_SEALED_BID"}
REPEATING = ("ENGLISH_INCREMENTAL", "REVERSE_UNDERCUT")  # kinds that keep bidding as the auction moves
LIVE = ("ACTIVE", "AWAITING_APPROVAL")


class TransientError(Exception):
    """A retryable transport failure (network drop, timeout)."""


class DirectTransport:
    """In-process transport. Swap for a network client (or a flaky test double)."""

    def __init__(self, engine):
        self.engine = engine

    def submit_bid(self, **kw):
        return self.engine.submit_bid(**kw)


class ExecutionEngine:
    def __init__(self, store, clock, engine, guardrail, audit, notify=None, transport=None, max_retries=3):
        self.store, self.clock, self.engine = store, clock, engine
        self.guardrail, self.audit = guardrail, audit
        self.notify = notify or (lambda *a, **k: None)
        self.transport = transport or DirectTransport(engine)
        self.max_retries = max_retries
        self._evaluating: set[str] = set()
        # auction_id -> ids of its unfinished plans. Finished plans are kept for a week for the UI, so the hot path (every bid,
        # every tick) must not walk them: a plan never comes back from a finished state, so this index only ever shrinks per auction.
        self._by_auction: dict[str, set[str]] = {}
        self._by_n = -1
        # Event-driven: re-evaluate an auction's triggers whenever it changes.
        for ev in ("auction.bid", "auction.price", "auction.extended", "auction.started",
                   "auction.settled", "auction.cancelled"):
            engine.events.on(ev, lambda auction_id, **_: self.evaluate_auction(auction_id))

    # ---------- trigger index ----------

    def _sync_index(self) -> None:
        if self._by_n != len(self.store.triggers):  # loaded, pruned, or edited out of band
            idx: dict[str, set[str]] = {}
            for t in self.store.triggers.values():
                if t["status"] in LIVE:
                    idx.setdefault(t["auction_id"], set()).add(t["id"])
            self._by_auction, self._by_n = idx, len(self.store.triggers)

    def _live_for(self, auction_id: str) -> list[dict]:
        self._sync_index()
        ids = self._by_auction.get(auction_id)
        if not ids:
            return []
        out = []
        for i in list(ids):
            t = self.store.triggers.get(i)
            if t is None or t["status"] not in LIVE:
                ids.discard(i)
            else:
                out.append(t)
        if not ids:
            self._by_auction.pop(auction_id, None)
        return out

    # ---------- trigger management ----------

    def register_trigger(self, *, agent_id, auction_id, kind, params, reasoning="", source="strategy") -> dict:
        agent = self.store.agents.get(agent_id)
        a = self.store.auctions.get(auction_id)
        if not a:
            raise not_found("AUCTION_NOT_FOUND", "auction not found")
        want = "SELLER" if is_reverse(a) else "BIDDER"
        if not agent or agent["agent_type"] != want:
            raise bad("INVALID_AGENT", "supplier (seller) agent required for an RFQ" if want == "SELLER" else "bidder agent required")
        if agent["status"] != "ACTIVE":
            raise forbidden("AGENT_NOT_ACTIVE", "agent is not active")
        if KIND_FOR_TYPE[a["auction_type"]] != kind:
            raise bad("KIND_MISMATCH", f"{kind} cannot be used on a {a['auction_type']} auction")
        _validate_params(kind, params)
        for t in self._live_for(auction_id):
            if t["agent_id"] == agent_id:
                self._cancel(t, "superseded by a newer plan")
        t = {"id": str(uuid.uuid4()), "agent_id": agent_id, "auction_id": auction_id, "kind": kind,
             "params": dict(params), "status": "ACTIVE", "reasoning": reasoning, "source": source,
             "fired_count": 0, "approval_id": None, "approved_up_to": 0, "last_rejection": None,
             "last_error": None, "outcome": None, "created_at": self.clock.now()}
        self._sync_index()  # before adding, so the length check stays meaningful
        self.store.triggers[t["id"]] = t
        self._by_auction.setdefault(auction_id, set()).add(t["id"])
        self._by_n = len(self.store.triggers)
        self.evaluate_auction(auction_id)
        return t

    def cancel_agent_triggers(self, agent_id: str, reason: str = "authority revoked") -> int:
        n = 0
        for t in self.store.triggers.values():
            if t["agent_id"] == agent_id and t["status"] in LIVE:
                self._cancel(t, reason)
                n += 1
        return n

    def _cancel(self, t: dict, reason: str) -> None:
        """Cancel a plan AND withdraw its pending approval card, so nobody is asked to approve something that no longer exists."""
        t["status"], t["last_error"] = "CANCELLED", reason
        ap = self.store.approvals.get(t.get("approval_id") or "")
        if ap and ap["status"] == "PENDING":
            ap["status"], ap["resolved_at"] = "EXPIRED", self.clock.now()

    def triggers_for(self, agent_id: str) -> list[dict]:
        return [t for t in self.store.triggers.values() if t["agent_id"] == agent_id]

    # ---------- evaluation loop ----------

    def evaluate_all(self) -> None:
        self._sync_index()
        for auction_id in [i for i in list(self._by_auction) if self._live_for(i)]:
            try:
                self.evaluate_auction(auction_id)
            except Exception:  # noqa: BLE001
                log.exception("evaluating auction %s failed", auction_id)

    def _retire_dangling(self, auction_id: str) -> None:
        """A plan waiting for approval on an auction that has since closed can never fire: retire it and its approval card."""
        a = self.store.auctions.get(auction_id)
        if a and a["status"] not in ("SETTLED", "CANCELLED"):
            return
        for t in self._live_for(auction_id):
            if t["status"] == "AWAITING_APPROVAL":
                t["status"], t["last_error"] = "CANCELLED", "auction closed while waiting for approval"
                ap = self.store.approvals.get(t.get("approval_id") or "")
                if ap and ap["status"] == "PENDING":
                    ap["status"], ap["resolved_at"] = "EXPIRED", self.clock.now()

    def evaluate_auction(self, auction_id: str) -> None:
        """Evaluate to a fixed point; events raised by our own bids are absorbed by the loop."""
        if auction_id in self._evaluating:
            return
        self._evaluating.add(auction_id)
        try:
            self._retire_dangling(auction_id)
            for _ in range(10_000):
                fired = False
                for t in self._live_for(auction_id):
                    if t["status"] == "ACTIVE":
                        try:
                            fired = self._evaluate(t) or fired
                        except Exception:  # noqa: BLE001  one poisoned record must not starve every other trigger on every tick
                            log.exception("trigger %s failed; blocking it", t["id"])
                            t["status"], t["last_error"] = "BLOCKED", "INTERNAL_ERROR"
                if not fired:
                    break
        finally:
            self._evaluating.discard(auction_id)

    def _evaluate(self, t: dict) -> bool:
        a = self.store.auctions.get(t["auction_id"])
        if not a:
            t["status"] = "CANCELLED"
            return False
        self.engine._advance(a)
        if a["status"] == "SCHEDULED":
            return False
        if not is_open(a):
            t["status"] = "DONE"
            if a["status"] == "SETTLED":
                t["outcome"] = "WON" if (a["result"] or {}).get("winner_agent_id") == t["agent_id"] else "LOST"
            return False
        agent = self.store.agents.get(t["agent_id"])
        if not agent or agent["status"] != "ACTIVE":
            t["status"], t["last_error"] = "CANCELLED", "agent not active"
            return False
        now = self.clock.now()
        p = t["params"]
        if t["kind"] == "ENGLISH_INCREMENTAL":
            if a["bids"] and a["bids"][-1]["agent_id"] == t["agent_id"]:
                return False  # already winning
            needed = self.engine.min_next_bid(a, now)
            if needed > p["max_bid"]:
                t["status"] = "EXHAUSTED"
                self.notify(t["agent_id"], "outbid",
                            f"Outbid on \"{a['product_spec']['title']}\": the next valid bid ({needed}) is above your agent's limit ({p['max_bid']}).",
                            auction_id=a["auction_id"])
                return False
            snipe = p.get("snipe_window_ms") or 0
            if snipe > 0 and effective_end(a) - now > snipe:
                return False
            last = a["bids"][-1] if a["bids"] else None
            # increment_pct may be fractional (LLMs say 2.5): always land on an integer amount
            stepped = math.ceil(last["amount"] * (100 + (p.get("increment_pct") or 0)) / 100) if last else needed
            cap = min(p["max_bid"], self.guardrail.soft_cap(agent, t["approved_up_to"]))  # a step must never overshoot the agent's own limits
            return self._fire(t, a, int(max(needed, min(stepped, cap))))
        if t["kind"] == "REVERSE_UNDERCUT":
            if a["bids"] and a["bids"][-1]["agent_id"] == t["agent_id"]:
                return False  # already the lowest quote
            needed = self.engine.max_next_bid(a)  # the most we may quote to be valid
            if needed < p["min_price"]:
                t["status"] = "EXHAUSTED"
                self.notify(t["agent_id"], "outbid",
                            f"Undercut on \"{a['product_spec']['title']}\": the next valid quote ({needed}) is below your agent's floor ({p['min_price']}).",
                            auction_id=a["auction_id"])
                return False
            snipe = p.get("snipe_window_ms") or 0
            if snipe > 0 and effective_end(a) - now > snipe:
                return False
            last = a["bids"][-1] if a["bids"] else None
            stepped = math.floor(last["amount"] * (100 - (p.get("decrement_pct") or 0)) / 100) if last else needed
            return self._fire(t, a, int(min(needed, max(stepped, p["min_price"]))))
        if t["kind"] == "REVERSE_SEALED_BID":
            return self._fire(t, a, p["amount"])
        if t["kind"] == "DUTCH_ACCEPT":
            price = self.engine.current_price(a, now)
            return self._fire(t, a, price) if price <= p["threshold"] else False
        if t["kind"] == "SEALED_BID":
            return self._fire(t, a, p["amount"])
        return False

    # ---------- firing ----------

    def _fire(self, t: dict, a: dict, amount: int) -> bool:
        started = time.perf_counter()
        proposal = {"agent_id": t["agent_id"], "auction_id": a["auction_id"],
                    "action": "place_sealed_bid" if "SEALED" in a["auction_type"] else "place_bid",
                    "amount": amount, "source": t["source"], "approved_up_to": t["approved_up_to"]}
        d = self.guardrail.evaluate(proposal)
        title = a["product_spec"]["title"]

        if d["decision"] == "REJECTED":
            if t["last_rejection"] != d["code"]:
                t["last_rejection"] = d["code"]
                self.audit.record(agent_id=t["agent_id"], auction_id=a["auction_id"], proposed_action=proposal,
                                  guardrail_decision="REJECTED", rejection_reason=f"{d['code']}: {d['reason']}")
                if d.get("permanent") and not d.get("notify"):
                    self.notify(t["agent_id"], "blocked", f"Your agent stopped on \"{title}\": {explain(d['code'])}", auction_id=a["auction_id"])
                if d.get("notify"):
                    self.notify(t["agent_id"], "anomaly",
                                f"Autonomous bidding halted on \"{title}\": {d['reason']}. Please review and decide manually.",
                                auction_id=a["auction_id"])
            if d.get("permanent"):
                t["status"], t["last_error"] = "BLOCKED", d["code"]
            return False

        if d["decision"] == "ESCALATED":
            ap = self._create_approval(t, a, proposal, d)
            t["status"], t["approval_id"] = "AWAITING_APPROVAL", ap["id"]
            self.audit.record(agent_id=t["agent_id"], auction_id=a["auction_id"], proposed_action=proposal,
                              guardrail_decision="ESCALATED", rejection_reason=f"{d['code']}: {d['reason']}")
            short = ap["id"][:8]
            self.notify(t["agent_id"], "approval_needed",
                        f"Approval needed: bid {amount} on \"{title}\" ({d['reason']}). Reply \"approve {short}\" or \"reject {short}\".",
                        approval_id=ap["id"], auction_id=a["auction_id"])
            return False

        t["last_rejection"] = None
        key = f"{t['id']}:{t['fired_count']}"
        res = self._submit_with_retry(auction_id=a["auction_id"], agent_id=t["agent_id"], amount=amount, idempotency_key=key)
        latency = round((time.perf_counter() - started) * 1000, 3)
        self.audit.record(agent_id=t["agent_id"], auction_id=a["auction_id"], proposed_action=proposal,
                          guardrail_decision="APPROVED",
                          executed_action={"action": proposal["action"], "amount": amount, "idempotency_key": key},
                          execution_result={"ok": res["ok"], "code": res.get("code", "OK"),
                                            "reconciled": bool(res.get("reconciled")), "latency_ms": latency})
        if res["ok"]:
            t["fired_count"] += 1
            if t["kind"] not in REPEATING:
                t["status"] = "DONE"
            return t["kind"] in REPEATING
        if res.get("code") == "AUCTION_NOT_OPEN":
            t["status"] = "DONE"
        elif res.get("code") in ("VERIFIED_ONLY", "VERIFICATION_REQUIRED"):
            t["status"], t["last_error"] = "BLOCKED", res["code"]
            self.notify(t["agent_id"], "verification", f"Your agent could not act on \"{title}\": {res.get('message')}", auction_id=a["auction_id"])
        elif res.get("code") not in ("BID_TOO_LOW", "ALREADY_HIGHEST", "ALREADY_LOWEST", "PLATFORM_PAUSED", "AUCTION_UNDER_REVIEW") and not (res.get("code") == "BID_TOO_HIGH" and t["kind"] in REPEATING):
            # (a sealed quote above the buyer's maximum can never become valid: block it instead of refiring every tick)
            t["status"], t["last_error"] = "BLOCKED", res.get("code")
        return False

    def _submit_with_retry(self, **args) -> dict:
        """Network-drop safe submission (spec §12): after a transport failure, query authoritative
        state before retrying; the idempotency key makes the retry itself safe."""
        attempt = 0
        while True:
            try:
                return self.transport.submit_bid(**args)
            except TransientError as e:
                a = self.store.auctions.get(args["auction_id"])
                if a and any(b.get("idempotency_key") == args["idempotency_key"] for b in a["bids"]):
                    return {"ok": True, "reconciled": True}
                if attempt >= self.max_retries:
                    return {"ok": False, "code": "TRANSPORT_FAILURE", "message": str(e)}
                attempt += 1
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "code": "EXECUTION_ERROR", "message": str(e)}

    # ---------- approvals (request_user_approval) ----------

    def _create_approval(self, t, a, proposal, decision) -> dict:
        ap = {"id": str(uuid.uuid4()), "kind": "BID", "agent_id": t["agent_id"], "auction_id": a["auction_id"],
              "trigger_id": t["id"], "proposal": proposal, "code": decision["code"], "reason": decision["reason"],
              "status": "PENDING", "created_at": self.clock.now(), "resolved_at": None}
        self.store.approvals[ap["id"]] = ap
        return ap

    def resolve_approval(self, approval_id: str, approve: bool) -> dict:
        ap = self.store.approvals.get(approval_id)
        if not ap:
            raise not_found("APPROVAL_NOT_FOUND", "approval not found")
        if ap["status"] != "PENDING":
            raise bad("APPROVAL_RESOLVED", f"approval already {ap['status']}")
        a = self.store.auctions.get(ap["auction_id"])
        if a:
            self.engine._advance(a)  # judge by the clock, not by a status the tick loop has not refreshed yet
        if approve and (not a or a["status"] in ("SETTLED", "CANCELLED")):
            ap["status"], ap["resolved_at"] = "EXPIRED", self.clock.now()
            raise bad("AUCTION_CLOSED", "that auction has already closed — nothing to approve")
        ap["status"] = "APPROVED" if approve else "REJECTED"
        ap["resolved_at"] = self.clock.now()
        t = self.store.triggers.get(ap["trigger_id"])
        if t and t["status"] == "AWAITING_APPROVAL":
            if approve:
                if ap["code"] == "TYPE_NOT_AUTHORIZED":
                    mem = self.store.agents[ap["agent_id"]]["durable_memory"].setdefault("one_off_authorizations", [])
                    if ap["auction_id"] not in mem:
                        mem.append(ap["auction_id"])
                t["approved_up_to"] = max(t["approved_up_to"], ap["proposal"]["amount"])
                t["status"], t["last_rejection"] = "ACTIVE", None
            else:
                t["status"], t["last_error"] = "CANCELLED", "rejected by user"
        if approve:
            self.evaluate_auction(ap["auction_id"])
        return ap


def _validate_params(kind: str, p: dict) -> None:
    def pos(n):
        return isinstance(n, int) and not isinstance(n, bool) and n > 0
    if not isinstance(p, dict):
        raise bad("INVALID_PARAMS", "params required")
    if kind == "ENGLISH_INCREMENTAL":
        if not pos(p.get("max_bid")):
            raise bad("INVALID_PARAMS", "max_bid must be a positive integer")
        pct = p.get("increment_pct")
        if pct is not None and not (isinstance(pct, (int, float)) and 0 <= pct <= 100):
            raise bad("INVALID_PARAMS", "increment_pct must be 0-100")
        sw = p.get("snipe_window_ms")
        if sw is not None and not (isinstance(sw, int) and sw >= 0):
            raise bad("INVALID_PARAMS", "snipe_window_ms must be >= 0")
    elif kind == "REVERSE_UNDERCUT":
        if not pos(p.get("min_price")):
            raise bad("INVALID_PARAMS", "min_price must be a positive integer")
        pct = p.get("decrement_pct")
        if pct is not None and not (isinstance(pct, (int, float)) and not isinstance(pct, bool) and 0 <= pct <= 100):
            raise bad("INVALID_PARAMS", "decrement_pct must be 0-100")
        sw = p.get("snipe_window_ms")
        if sw is not None and not (isinstance(sw, int) and sw >= 0):
            raise bad("INVALID_PARAMS", "snipe_window_ms must be >= 0")
    elif kind == "REVERSE_SEALED_BID":
        if not pos(p.get("amount")):
            raise bad("INVALID_PARAMS", "amount must be a positive integer")
    elif kind == "DUTCH_ACCEPT":
        if not pos(p.get("threshold")):
            raise bad("INVALID_PARAMS", "threshold must be a positive integer")
    elif kind == "SEALED_BID":
        if not pos(p.get("amount")):
            raise bad("INVALID_PARAMS", "amount must be a positive integer")
    else:
        raise bad("INVALID_KIND", f"unknown trigger kind {kind}")
