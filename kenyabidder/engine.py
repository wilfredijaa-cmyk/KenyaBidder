"""Auction Engine (spec §8.1, §9): pure, synchronous, clock-injected.

Forward auctions (sellers list, buyer agents bid up): English, Dutch, First-Price Sealed, Second-Price (Vickrey) Sealed.
Reverse auctions / RFQs (a buyer agent posts what it needs and its maximum price; supplier agents bid *down*): REVERSE_ENGLISH
(open, each bid must undercut the best by the minimum decrement) and REVERSE_SEALED (lowest sealed bid wins).
Multi-Unit / Combinatorial are architecturally reserved.
"""
from __future__ import annotations

import copy
import uuid

from .errors import AppError, bad, forbidden, is_nonneg_int, is_pos_int, not_found
from .events import Events
from .moderation import check_prohibited

FORWARD_TYPES = ["ENGLISH", "DUTCH", "FIRST_PRICE_SEALED", "SECOND_PRICE_SEALED"]
REVERSE_TYPES = ["REVERSE_ENGLISH", "REVERSE_SEALED"]
AUCTION_TYPES = FORWARD_TYPES + REVERSE_TYPES

# Listing text is shown to other users' LLM agents (which cost them tokens) — it must stay small.
SPEC_LIMITS = {"category": 60, "title": 120, "description": 1000, "condition": 60, "brand": 60, "unit": 30}
DEFAULT_LIMITS = {"max_open_listings": 50, "max_new_listings_per_minute": 10}
SEALED = {"FIRST_PRICE_SEALED", "SECOND_PRICE_SEALED", "REVERSE_SEALED"}
REVERSE = set(REVERSE_TYPES)
OPEN = {"ACTIVE", "EXTENDING"}


def is_open(a: dict) -> bool:
    return a["status"] in OPEN


def is_reverse(a: dict) -> bool:
    return a["auction_type"] in REVERSE


def _verified(store, agent: dict | None) -> bool:
    u = store.users.get((agent or {}).get("principal_user_id") or "")
    return bool(u and u.get("verified_business"))


def same_party(store, agent_a: dict | None, agent_b: dict | None) -> bool:
    """Shill-bid guard: two agents belong to the same party if they share an account — or the same phone number or email, which is what
    someone running a second account to bid up their own listing would reuse."""
    if not agent_a or not agent_b:
        return False
    ua, ub = store.users.get(agent_a["principal_user_id"]), store.users.get(agent_b["principal_user_id"])
    if agent_a["principal_user_id"] == agent_b["principal_user_id"]:
        return True
    if not ua or not ub:
        return False
    for k, norm in (("phone", lambda v: v), ("email", lambda v: v.strip().lower())):
        va, vb = ua.get(k), ub.get(k)
        if va and vb and norm(va) == norm(vb):
            return True
    return False


def poster_of(a: dict) -> str | None:
    """The agent that created the listing: the seller of a forward auction, the buyer of a reverse one (RFQ)."""
    return a.get("poster_agent_id") or a.get("seller_agent_id")


def is_sealed(a: dict) -> bool:
    return a["auction_type"] in SEALED


def effective_end(a: dict) -> int:
    return a["extended_until"] if a["extended_until"] is not None else a["ends_at"]


def _pub_bid(b: dict) -> dict:
    return {"agent_id": b["agent_id"], "amount": b["amount"], "at": b["at"], "bid_type": b["bid_type"]}


class AuctionEngine:
    def __init__(self, store, clock, events: Events | None = None, limits: dict | None = None):
        self.store = store
        self.clock = clock
        self.events = events or Events()
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}

    # ---------- listings ----------

    def _check_running(self) -> None:
        if self.store.settings.get("platform_paused"):
            raise AppError("PLATFORM_PAUSED", "the marketplace is temporarily paused by the administrators — please try again shortly", 503)

    def create_listing(self, *, seller_agent_id, product_spec, auction_type, duration_ms, reserve_price=0,
                       start_price=None, min_increment=1, starts_at=None, dutch=None, anti_snipe=None,
                       relist_of=None, relist_count=0, demo=False, verified_only=False) -> dict:
        self._check_running()
        now = self.clock.now()
        seller = self.store.agents.get(seller_agent_id)
        if not seller or seller["agent_type"] != "SELLER":
            raise bad("INVALID_SELLER", "seller_agent_id must reference a SELLER agent")
        if seller["status"] != "ACTIVE":
            raise forbidden("AGENT_NOT_ACTIVE", "seller agent is not active")

        spec = self._clean_spec(product_spec)
        check_prohibited(self.store, spec)
        if auction_type not in FORWARD_TYPES:
            raise bad("INVALID_AUCTION_TYPE", f"auction_type must be one of {', '.join(FORWARD_TYPES)} (use create_rfq for reverse auctions)")
        if auction_type not in seller["constraints"]["authorized_auction_types"]:
            raise forbidden("TYPE_NOT_ALLOWED", f"this seller agent is not configured for {auction_type} auctions")
        if not is_nonneg_int(reserve_price):
            raise bad("INVALID_PRICE", "reserve_price must be a non-negative integer")
        floor = seller["constraints"].get("reserve_floor", 0)
        if reserve_price < floor:
            raise bad("RESERVE_BELOW_FLOOR", f"reserve_price is below the agent's reserve floor ({floor})")
        if not is_pos_int(duration_ms):
            raise bad("INVALID_DURATION", "duration_ms must be a positive integer")
        starts_at = now if starts_at is None else starts_at
        if not isinstance(starts_at, int):
            raise bad("INVALID_START", "starts_at must be an epoch-ms integer")
        self._check_listing_limits(seller_agent_id, now, relist=relist_of is not None)

        a = {
            "auction_id": str(uuid.uuid4()), "auction_type": auction_type,
            "status": "SCHEDULED" if starts_at > now else "ACTIVE",
            "seller_agent_id": seller_agent_id, "poster_agent_id": seller_agent_id, "direction": "FORWARD",
            "product_spec": spec,
            "reserve_price": reserve_price, "current_price": None, "min_increment": 1, "start_price": None,
            "dutch": None, "anti_snipe": {"window_ms": 0, "extend_ms": 0}, "bids": [],
            "starts_at": starts_at, "ends_at": starts_at + duration_ms, "extended_until": None,
            "result": None, "relist_of": relist_of, "relist_count": relist_count, "created_at": now,
            "closed_at": None, "demo": bool(demo), "verified_only": bool(verified_only),
        }
        if auction_type == "ENGLISH":
            sp = reserve_price if start_price is None else start_price
            if not is_nonneg_int(sp):
                raise bad("INVALID_PRICE", "start_price must be a non-negative integer")
            if not is_pos_int(min_increment):
                raise bad("INVALID_INCREMENT", "min_increment must be a positive integer")
            a.update(start_price=sp, current_price=sp, min_increment=min_increment)
            s = anti_snipe or {}
            if is_pos_int(s.get("window_ms")) and is_pos_int(s.get("extend_ms")):
                a["anti_snipe"] = {"window_ms": s["window_ms"], "extend_ms": s["extend_ms"]}
        elif auction_type == "DUTCH":
            d = dutch or {}
            if not (is_pos_int(d.get("start_price")) and is_nonneg_int(d.get("floor_price"))
                    and is_pos_int(d.get("decrement")) and is_pos_int(d.get("interval_ms"))):
                raise bad("INVALID_DUTCH", "dutch {start_price, floor_price, decrement, interval_ms} required")
            if d["start_price"] <= d["floor_price"]:
                raise bad("INVALID_DUTCH", "dutch.start_price must exceed floor_price")
            if d["floor_price"] < reserve_price:
                raise bad("INVALID_DUTCH", "dutch.floor_price must be >= reserve_price")
            a["dutch"] = dict(d)
            a["start_price"] = d["start_price"]
            a["current_price"] = d["start_price"]

        self.store.auctions[a["auction_id"]] = a
        self.events.emit("auction.created", auction_id=a["auction_id"])
        return self.view(a, seller_agent_id)

    def create_rfq(self, *, buyer_agent_id, product_spec, auction_type, duration_ms, max_price, min_decrement=1, starts_at=None,
                   anti_snipe=None, demo=False, verified_only=False) -> dict:
        """Post a request for quotes: suppliers (SELLER agents) bid the price DOWN from ``max_price``; the lowest valid bid wins."""
        self._check_running()
        now = self.clock.now()
        buyer = self.store.agents.get(buyer_agent_id)
        if not buyer or buyer["agent_type"] != "BIDDER":
            raise bad("INVALID_BUYER", "buyer_agent_id must reference a BIDDER agent")
        if buyer["status"] != "ACTIVE":
            raise forbidden("AGENT_NOT_ACTIVE", "buyer agent is not active")
        spec = self._clean_spec(product_spec)
        check_prohibited(self.store, spec)
        if auction_type not in REVERSE_TYPES:
            raise bad("INVALID_AUCTION_TYPE", f"auction_type must be one of {', '.join(REVERSE_TYPES)}")
        if not is_pos_int(max_price):
            raise bad("INVALID_PRICE", "max_price (the most you will pay in total) must be a positive integer")
        if max_price > buyer["constraints"]["budget_ceiling"]:
            raise forbidden("CEILING_EXCEEDED", f"max_price exceeds this agent's budget ceiling ({buyer['constraints']['budget_ceiling']})")
        if not is_pos_int(duration_ms):
            raise bad("INVALID_DURATION", "duration_ms must be a positive integer")
        starts_at = now if starts_at is None else starts_at
        if not isinstance(starts_at, int):
            raise bad("INVALID_START", "starts_at must be an epoch-ms integer")
        self._check_listing_limits(buyer_agent_id, now, relist=False)
        a = {
            "auction_id": str(uuid.uuid4()), "auction_type": auction_type,
            "status": "SCHEDULED" if starts_at > now else "ACTIVE",
            "seller_agent_id": None, "poster_agent_id": buyer_agent_id, "direction": "REVERSE",
            "product_spec": spec, "max_price": max_price,
            "reserve_price": 0, "current_price": max_price, "min_increment": 1, "start_price": max_price,
            "dutch": None, "anti_snipe": {"window_ms": 0, "extend_ms": 0}, "bids": [],
            "starts_at": starts_at, "ends_at": starts_at + duration_ms, "extended_until": None,
            "result": None, "relist_of": None, "relist_count": 0, "created_at": now, "closed_at": None, "demo": bool(demo),
            "verified_only": bool(verified_only),
        }
        if auction_type == "REVERSE_ENGLISH":
            if not is_pos_int(min_decrement):
                raise bad("INVALID_INCREMENT", "min_decrement must be a positive integer")
            a["min_increment"] = min_decrement  # for a reverse auction: the minimum amount a new bid must undercut the best one by
            s = anti_snipe or {}
            if is_pos_int(s.get("window_ms")) and is_pos_int(s.get("extend_ms")):
                a["anti_snipe"] = {"window_ms": s["window_ms"], "extend_ms": s["extend_ms"]}
        self.store.auctions[a["auction_id"]] = a
        self.events.emit("auction.created", auction_id=a["auction_id"])
        return self.view(a, buyer_agent_id)

    def repost(self, *, auction_id: str, agent_id: str, duration_ms: int | None = None, price_change_pct: float = 0) -> dict:
        """Run a finished listing / RFQ again (the 'reorder' every B2B marketplace offers), optionally nudging the price.
        For an RFQ the buyer's maximum moves by ``price_change_pct`` (e.g. +10 when nobody quoted); for a listing the reserve and start prices move."""
        a = self._get(auction_id)
        self._advance(a)
        if poster_of(a) != agent_id:
            raise forbidden("NOT_LISTING_OWNER", "only the agent that posted it can run it again")
        if a["status"] not in ("SETTLED", "CANCELLED"):
            raise AppError("STILL_RUNNING", "this one is still running", 409)
        if not (-90 <= price_change_pct <= 500):
            raise bad("INVALID_PRICE", "price change must be between -90% and +500%")
        k = 1 + price_change_pct / 100
        dur = duration_ms or (a["ends_at"] - a["starts_at"])
        spec = dict(a["product_spec"])
        if is_reverse(a):
            return self.create_rfq(buyer_agent_id=agent_id, product_spec=spec, auction_type=a["auction_type"], duration_ms=dur, max_price=max(1, round(a["max_price"] * k)),
                                   min_decrement=a["min_increment"], anti_snipe=a["anti_snipe"] if a["anti_snipe"]["window_ms"] else None, verified_only=a.get("verified_only", False))
        kw = dict(seller_agent_id=agent_id, product_spec=spec, auction_type=a["auction_type"], duration_ms=dur, reserve_price=round(a["reserve_price"] * k),
                  verified_only=a.get("verified_only", False))
        if a["auction_type"] == "ENGLISH":
            kw.update(start_price=min(kw["reserve_price"], round((a["start_price"] or 0) * k)), min_increment=a["min_increment"], anti_snipe=a["anti_snipe"] if a["anti_snipe"]["window_ms"] else None)
        elif a["auction_type"] == "DUTCH":
            d = a["dutch"]
            floor = max(kw["reserve_price"], round(d["floor_price"] * k))
            kw["dutch"] = {**d, "start_price": max(floor + 1, round(d["start_price"] * k)), "floor_price": floor}
        return self.create_listing(**kw)

    @staticmethod
    def _clean_spec(spec) -> dict:
        if not isinstance(spec, dict):
            raise bad("INVALID_SPEC", "product_spec is required")
        unknown = set(spec) - set(SPEC_LIMITS) - {"quantity"}
        if unknown:
            raise bad("INVALID_SPEC", f"unknown product_spec field(s): {', '.join(sorted(unknown))}")
        for k in ("category", "title"):
            if not isinstance(spec.get(k), str) or not spec[k].strip():
                raise bad("INVALID_SPEC", f"product_spec.{k} is required")
        if not is_pos_int(spec.get("quantity")):
            raise bad("INVALID_SPEC", "product_spec.quantity must be a positive integer")
        out: dict = {"quantity": spec["quantity"]}
        for k, limit in SPEC_LIMITS.items():
            if k not in spec or spec[k] in (None, ""):
                continue
            if not isinstance(spec[k], str):
                raise bad("INVALID_SPEC", f"product_spec.{k} must be text")
            v = spec[k].strip()
            if len(v) > limit:
                raise bad("INVALID_SPEC", f"product_spec.{k} is too long ({len(v)} > {limit} characters)")
            out[k] = v
        return out

    def _check_listing_limits(self, seller_agent_id: str, now: int, relist: bool) -> None:
        """Stop a seller flooding the marketplace (and, transitively, every watching agent's LLM budget)."""
        open_n = recent = 0
        for a in self.store.auctions.values():
            if poster_of(a) != seller_agent_id:
                continue
            self._advance(a)  # judge by the clock, not by a status the tick loop has not refreshed yet
            if a["status"] in ("SCHEDULED", "ACTIVE", "EXTENDING"):
                open_n += 1
            if now - a["created_at"] < 60_000:
                recent += 1
        if open_n >= self.limits["max_open_listings"]:
            raise AppError("TOO_MANY_LISTINGS", f"this agent already has {open_n} open listings (limit {self.limits['max_open_listings']})", 429)
        if not relist and recent >= self.limits["max_new_listings_per_minute"]:
            raise AppError("LISTING_RATE_LIMIT", "too many listings created in the last minute — slow down", 429)

    def withdraw_listing(self, *, listing_id, seller_agent_id, reason="") -> dict:
        a = self._get(listing_id)
        if poster_of(a) != seller_agent_id:
            raise forbidden("NOT_LISTING_OWNER", "only the agent that posted the listing may withdraw it")
        self._advance(a)
        if a["status"] != "SCHEDULED" and not is_open(a):
            raise AppError("NOT_WITHDRAWABLE", f"auction is {a['status']}", 409)
        if a["bids"]:
            raise AppError("HAS_BIDS", "cannot withdraw a listing that already has bids", 409)
        a["status"] = "CANCELLED"
        a["result"] = {"outcome": "CANCELLED", "reason": reason}
        self.events.emit("auction.cancelled", auction_id=a["auction_id"])
        return self.view(a, seller_agent_id)

    # ---------- reads ----------

    def _get(self, auction_id: str) -> dict:
        a = self.store.auctions.get(auction_id)
        if not a:
            raise not_found("AUCTION_NOT_FOUND", f"auction {auction_id} not found")
        return a

    def get_auction(self, auction_id: str) -> dict:
        """Raw internal record — trusted harness code only, never shown to LLMs or clients."""
        a = self._get(auction_id)
        self._advance(a)
        return a

    def current_price(self, a: dict, now: int | None = None):
        now = self.clock.now() if now is None else now
        if a["auction_type"] == "DUTCH":
            d = a["dutch"]
            at = min(max(now, a["starts_at"]), a["ends_at"])
            steps = (at - a["starts_at"]) // d["interval_ms"]
            return max(d["floor_price"], d["start_price"] - steps * d["decrement"])
        return a["current_price"]

    def max_next_bid(self, a: dict) -> int:
        """Reverse auctions: the HIGHEST amount a new bid may be (everything above is refused)."""
        if a["auction_type"] == "REVERSE_ENGLISH" and a["bids"]:
            return a["current_price"] - a["min_increment"]
        return a["max_price"]

    def min_next_bid(self, a: dict, now: int | None = None) -> int:
        now = self.clock.now() if now is None else now
        t = a["auction_type"]
        if t in REVERSE:
            return 1
        if t == "ENGLISH":
            return a["current_price"] + a["min_increment"] if a["bids"] else a["start_price"]
        if t == "DUTCH":
            return self.current_price(a, now)
        return max(a["reserve_price"], 1)

    def view(self, a: dict, viewer_agent_id: str | None = None) -> dict:
        """Client/LLM-safe view: hides the reserve from non-sellers and sealed bids until settlement."""
        now = self.clock.now()
        is_seller = bool(viewer_agent_id) and viewer_agent_id == poster_of(a)  # the poster sees the private side of their own listing
        sealed_open = is_sealed(a) and a["status"] != "SETTLED"
        v = {
            "auction_id": a["auction_id"], "auction_type": a["auction_type"], "status": a["status"],
            "seller_agent_id": a["seller_agent_id"], "poster_agent_id": poster_of(a), "direction": a.get("direction", "FORWARD"),
            "product_spec": copy.deepcopy(a["product_spec"]),
            "current_price": None if sealed_open else self.current_price(a, now),
            "min_next_bid": self.min_next_bid(a, now) if is_open(a) and not is_reverse(a) else None,
            "max_next_bid": self.max_next_bid(a) if is_open(a) and is_reverse(a) else None, "max_price": a.get("max_price"),
            "min_increment": a["min_increment"], "start_price": a["start_price"],
            "dutch": copy.deepcopy(a["dutch"]), "anti_snipe": dict(a["anti_snipe"]),
            "starts_at": a["starts_at"], "ends_at": a["ends_at"], "extended_until": a["extended_until"],
            "bid_count": len(a["bids"]), "created_at": a["created_at"], "relist_of": a["relist_of"], "demo": a.get("demo", False),
            "hidden": bool(a.get("hidden")), "verified_only": a.get("verified_only", False), "poster_verified": _verified(self.store, self.store.agents.get(poster_of(a) or "")),
            "reserve_met": (a["current_price"] >= a["reserve_price"] and bool(a["bids"]))
            if a["auction_type"] == "ENGLISH" else None,
            "result": copy.deepcopy(a["result"]) if a["result"] and (is_seller or a["status"] == "SETTLED") else None,
        }
        if is_seller and not is_reverse(a):
            v["reserve_price"] = a["reserve_price"]
        bids = [b for b in a["bids"] if b["agent_id"] == viewer_agent_id] if sealed_open else a["bids"]
        v["bids"] = [_pub_bid(b) for b in bids]
        return v

    def get_auction_detail(self, auction_id: str, viewer_agent_id: str | None = None) -> dict:
        return self.view(self.get_auction(auction_id), viewer_agent_id)

    def list_active_auctions(self, *, category=None, auction_types=None, min_quantity=None, ends_before=None,
                             max_price=None, q=None, include_scheduled=False, viewer_agent_id=None, direction="FORWARD") -> list[dict]:
        """Deterministic pre-filter (spec §11.3) — no LLM involved."""
        self.tick()
        statuses = {"ACTIVE", "EXTENDING"} | ({"SCHEDULED"} if include_scheduled else set())
        now = self.clock.now()
        ql = q.lower() if q else None
        out = []
        for a in self.store.auctions.values():
            if a["status"] not in statuses:
                continue
            if a.get("hidden"):
                continue  # under moderator review
            if direction and a.get("direction", "FORWARD") != direction:
                continue  # bidder agents browse listings; supplier agents browse RFQs
            spec = a["product_spec"]
            if category and spec["category"].lower() != category.lower():
                continue
            if auction_types and a["auction_type"] not in auction_types:
                continue
            if min_quantity and spec["quantity"] < min_quantity:
                continue
            if ends_before and effective_end(a) > ends_before:
                continue
            if max_price is not None and not is_reverse(a) and self.min_next_bid(a, now) > max_price:
                continue
            if ql and ql not in f"{spec['title']} {spec.get('description', '')}".lower():
                continue
            out.append(self.view(a, viewer_agent_id))
        return sorted(out, key=lambda x: x["ends_at"])

    # ---------- lifecycle ----------

    def tick(self) -> None:
        for a in list(self.store.auctions.values()):
            self._advance(a)

    def _advance(self, a: dict) -> None:
        now = self.clock.now()
        if a["status"] == "SCHEDULED" and now >= a["starts_at"]:
            a["status"] = "ACTIVE"
            self.events.emit("auction.started", auction_id=a["auction_id"])
        if not is_open(a):
            return
        if now >= effective_end(a):
            self._close(a, now)
        elif a["auction_type"] == "DUTCH":
            p = self.current_price(a, now)
            if p != a["current_price"]:
                a["current_price"] = p
                self.events.emit("auction.price", auction_id=a["auction_id"], price=p)

    def _close(self, a: dict, now: int, dutch_winner: dict | None = None) -> None:
        if a["status"] in ("SETTLED", "CLOSING"):  # a lot can only be settled once
            return
        a["status"] = "CLOSING"
        reserve = a["reserve_price"]
        result = {"outcome": "NO_SALE", "winner_agent_id": None, "price": None}
        t = a["auction_type"]
        if t == "REVERSE_ENGLISH":
            top = a["bids"][-1] if a["bids"] else None  # each bid undercuts the last, so the newest is the lowest
            if top:
                result = {"outcome": "SOLD", "winner_agent_id": top["agent_id"], "price": top["amount"]}
        elif t == "REVERSE_SEALED":
            ranked = sorted(a["bids"], key=lambda b: (b["amount"], b["at"]))  # lowest wins; ties go to whoever bid first
            if ranked:
                result = {"outcome": "SOLD", "winner_agent_id": ranked[0]["agent_id"], "price": ranked[0]["amount"]}
        elif t == "ENGLISH":
            top = a["bids"][-1] if a["bids"] else None
            if top and top["amount"] >= reserve:
                result = {"outcome": "SOLD", "winner_agent_id": top["agent_id"], "price": top["amount"]}
        elif t == "DUTCH":
            if dutch_winner:
                result = {"outcome": "SOLD", "winner_agent_id": dutch_winner["agent_id"], "price": dutch_winner["amount"]}
        else:
            ranked = sorted((b for b in a["bids"] if b["amount"] >= reserve), key=lambda b: (-b["amount"], b["at"]))
            if ranked:
                if t == "FIRST_PRICE_SEALED":
                    price = ranked[0]["amount"]
                else:
                    price = max(ranked[1]["amount"] if len(ranked) > 1 else 0, reserve)
                result = {"outcome": "SOLD", "winner_agent_id": ranked[0]["agent_id"], "price": price}
        a["result"] = result
        a["status"] = "SETTLED"
        a["closed_at"] = now
        if t == "DUTCH" and result["price"] is not None:
            a["current_price"] = result["price"]
        if result["outcome"] == "SOLD":
            self.store.market_history.append({
                "category": a["product_spec"]["category"].lower(), "auction_type": t,
                "price": result["price"], "quantity": a["product_spec"]["quantity"], "at": now,
                **({"demo": True} if a.get("demo") else {}), **({"reverse": True} if t in REVERSE else {})})
        self.events.emit("auction.settled", auction_id=a["auction_id"], result=result)

    # ---------- bidding ----------

    def submit_bid(self, *, auction_id, agent_id, amount, bid_type="BID", idempotency_key=None) -> dict:
        """submit_bid — Execution Engine only, never exposed to an LLM. Idempotent per (agent, key)."""
        key = f"{agent_id}:{idempotency_key}" if idempotency_key else None
        if key and key in self.store.idempotency:
            return {**self.store.idempotency[key], "replayed": True}
        res = self._submit(auction_id, agent_id, amount, bid_type, idempotency_key)
        if key and res["ok"]:
            self.store.idempotency[key] = {"ok": True, "bid": res["bid"]}  # small on purpose: a replay needs the outcome, not the auction
        return res

    def _submit(self, auction_id, agent_id, amount, bid_type, idempotency_key) -> dict:
        def fail(code, message, **extra):
            return {"ok": False, "code": code, "message": message, **extra}

        if self.store.settings.get("platform_paused"):
            return fail("PLATFORM_PAUSED", "the marketplace is temporarily paused")
        a = self.store.auctions.get(auction_id)
        if not a:
            return fail("AUCTION_NOT_FOUND", "auction not found")
        agent = self.store.agents.get(agent_id)
        want = "SELLER" if is_reverse(a) else "BIDDER"
        if not agent or agent["agent_type"] != want:
            return fail("INVALID_BIDDER", "only supplier (seller) agents may quote on an RFQ" if want == "SELLER" else "agent is not a bidder")
        if agent["status"] != "ACTIVE":
            return fail("AGENT_NOT_ACTIVE", "agent is not active")
        self._advance(a)
        if not is_open(a):
            return fail("AUCTION_NOT_OPEN", f"auction is {a['status']}")
        if a.get("hidden"):
            return fail("AUCTION_UNDER_REVIEW", "this listing is under moderator review")
        poster = self.store.agents.get(poster_of(a))
        if same_party(self.store, poster, agent):
            return fail("SELF_BID", "you cannot bid on your own listing (or one posted from an account sharing your phone or email)")
        if not is_pos_int(amount):
            return fail("INVALID_AMOUNT", "amount must be a positive integer")
        if not _verified(self.store, agent):  # trust gates: verified-only listings, and high-value deals for verified businesses
            if a.get("verified_only"):
                return fail("VERIFIED_ONLY", "this listing accepts verified businesses only — apply for the badge in Profile")
            limit = self.store.settings.get("verification_required_above") or 0
            if limit and amount > limit:
                return fail("VERIFICATION_REQUIRED", f"amounts above {limit:,} KES require a verified business — apply for the badge in Profile")

        now = self.clock.now()
        bid = {"bid_id": str(uuid.uuid4()), "agent_id": agent_id, "amount": amount, "at": now,
               "bid_type": bid_type, "idempotency_key": idempotency_key}
        t = a["auction_type"]
        if t == "REVERSE_ENGLISH":
            hi = self.max_next_bid(a)
            if amount > hi:
                return fail("BID_TOO_HIGH", f"a quote must be at most {hi}", max_allowed=hi)
            if a["bids"] and a["bids"][-1]["agent_id"] == agent_id:
                return fail("ALREADY_LOWEST", "agent already holds the lowest quote")
            a["bids"].append(bid)
            a["current_price"] = amount
            end = effective_end(a)
            if a["anti_snipe"]["window_ms"] > 0 and end - now <= a["anti_snipe"]["window_ms"]:
                a["extended_until"] = end + a["anti_snipe"]["extend_ms"]
                a["status"] = "EXTENDING"
                self.events.emit("auction.extended", auction_id=auction_id, extended_until=a["extended_until"])
            self.events.emit("auction.bid", auction_id=auction_id, agent_id=agent_id, amount=amount)
        elif t == "REVERSE_SEALED":
            if amount > a["max_price"]:
                return fail("BID_TOO_HIGH", f"a quote must be at most {a['max_price']}", max_allowed=a["max_price"])
            a["bids"] = [b for b in a["bids"] if b["agent_id"] != agent_id]  # revisable until close
            a["bids"].append(bid)
            self.events.emit("auction.bid", auction_id=auction_id, agent_id=agent_id, sealed=True)
        elif t == "ENGLISH":
            lo = self.min_next_bid(a, now)
            if amount < lo:
                return fail("BID_TOO_LOW", f"minimum bid is {lo}", min_required=lo)
            if a["bids"] and a["bids"][-1]["agent_id"] == agent_id:
                return fail("ALREADY_HIGHEST", "agent already holds the highest bid")
            a["bids"].append(bid)
            a["current_price"] = amount
            end = effective_end(a)
            if a["anti_snipe"]["window_ms"] > 0 and end - now <= a["anti_snipe"]["window_ms"]:
                a["extended_until"] = end + a["anti_snipe"]["extend_ms"]
                a["status"] = "EXTENDING"
                self.events.emit("auction.extended", auction_id=auction_id, extended_until=a["extended_until"])
            self.events.emit("auction.bid", auction_id=auction_id, agent_id=agent_id, amount=amount)
        elif t == "DUTCH":
            price = self.current_price(a, now)
            if amount < price:
                return fail("BID_TOO_LOW", f"current price is {price}", min_required=price)
            bid["amount"] = price  # accepting the current asking price
            a["bids"].append(bid)
            self._close(a, now, bid)  # settle first: listeners reacting to the bid must see a closed auction
            self.events.emit("auction.bid", auction_id=auction_id, agent_id=agent_id, amount=price)
        else:
            if amount < a["reserve_price"]:
                return fail("BELOW_RESERVE", f"sealed bid must be at least {a['reserve_price']}",
                            min_required=a["reserve_price"])
            a["bids"] = [b for b in a["bids"] if b["agent_id"] != agent_id]  # revisable until close
            a["bids"].append(bid)
            self.events.emit("auction.bid", auction_id=auction_id, agent_id=agent_id, sealed=True)
        return {"ok": True, "bid": _pub_bid(bid), "auction": self.view(a, agent_id)}
