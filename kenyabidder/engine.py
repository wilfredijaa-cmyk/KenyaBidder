"""Auction Engine (spec §8.1, §9): pure, synchronous, clock-injected.

Supports English, Dutch, First-Price Sealed and Second-Price (Vickrey) Sealed.
Reverse / Multi-Unit / Combinatorial are architecturally reserved (Milestone 4+).
"""
from __future__ import annotations

import copy
import uuid

from .errors import AppError, bad, forbidden, is_nonneg_int, is_pos_int, not_found
from .events import Events

AUCTION_TYPES = ["ENGLISH", "DUTCH", "FIRST_PRICE_SEALED", "SECOND_PRICE_SEALED"]

# Listing text is shown to other users' LLM agents (which cost them tokens) — it must stay small.
SPEC_LIMITS = {"category": 60, "title": 120, "description": 1000, "condition": 60, "brand": 60, "unit": 30}
DEFAULT_LIMITS = {"max_open_listings": 50, "max_new_listings_per_minute": 10}
SEALED = {"FIRST_PRICE_SEALED", "SECOND_PRICE_SEALED"}
OPEN = {"ACTIVE", "EXTENDING"}


def is_open(a: dict) -> bool:
    return a["status"] in OPEN


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

    def create_listing(self, *, seller_agent_id, product_spec, auction_type, duration_ms, reserve_price=0,
                       start_price=None, min_increment=1, starts_at=None, dutch=None, anti_snipe=None,
                       relist_of=None, relist_count=0, demo=False) -> dict:
        now = self.clock.now()
        seller = self.store.agents.get(seller_agent_id)
        if not seller or seller["agent_type"] != "SELLER":
            raise bad("INVALID_SELLER", "seller_agent_id must reference a SELLER agent")
        if seller["status"] != "ACTIVE":
            raise forbidden("AGENT_NOT_ACTIVE", "seller agent is not active")

        spec = self._clean_spec(product_spec)
        if auction_type not in AUCTION_TYPES:
            raise bad("INVALID_AUCTION_TYPE", f"auction_type must be one of {', '.join(AUCTION_TYPES)}")
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
            "seller_agent_id": seller_agent_id,
            "product_spec": spec,
            "reserve_price": reserve_price, "current_price": None, "min_increment": 1, "start_price": None,
            "dutch": None, "anti_snipe": {"window_ms": 0, "extend_ms": 0}, "bids": [],
            "starts_at": starts_at, "ends_at": starts_at + duration_ms, "extended_until": None,
            "result": None, "relist_of": relist_of, "relist_count": relist_count, "created_at": now,
            "closed_at": None, "demo": bool(demo),
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
            if a["seller_agent_id"] != seller_agent_id:
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
        if a["seller_agent_id"] != seller_agent_id:
            raise forbidden("NOT_LISTING_OWNER", "only the listing seller may withdraw")
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

    def min_next_bid(self, a: dict, now: int | None = None) -> int:
        now = self.clock.now() if now is None else now
        t = a["auction_type"]
        if t == "ENGLISH":
            return a["current_price"] + a["min_increment"] if a["bids"] else a["start_price"]
        if t == "DUTCH":
            return self.current_price(a, now)
        return max(a["reserve_price"], 1)

    def view(self, a: dict, viewer_agent_id: str | None = None) -> dict:
        """Client/LLM-safe view: hides the reserve from non-sellers and sealed bids until settlement."""
        now = self.clock.now()
        is_seller = bool(viewer_agent_id) and viewer_agent_id == a["seller_agent_id"]
        sealed_open = is_sealed(a) and a["status"] != "SETTLED"
        v = {
            "auction_id": a["auction_id"], "auction_type": a["auction_type"], "status": a["status"],
            "seller_agent_id": a["seller_agent_id"], "product_spec": copy.deepcopy(a["product_spec"]),
            "current_price": None if sealed_open else self.current_price(a, now),
            "min_next_bid": self.min_next_bid(a, now) if is_open(a) else None,
            "min_increment": a["min_increment"], "start_price": a["start_price"],
            "dutch": copy.deepcopy(a["dutch"]), "anti_snipe": dict(a["anti_snipe"]),
            "starts_at": a["starts_at"], "ends_at": a["ends_at"], "extended_until": a["extended_until"],
            "bid_count": len(a["bids"]), "created_at": a["created_at"], "relist_of": a["relist_of"], "demo": a.get("demo", False),
            "reserve_met": (a["current_price"] >= a["reserve_price"] and bool(a["bids"]))
            if a["auction_type"] == "ENGLISH" else None,
            "result": copy.deepcopy(a["result"]) if a["result"] and (is_seller or a["status"] == "SETTLED") else None,
        }
        if is_seller:
            v["reserve_price"] = a["reserve_price"]
        bids = [b for b in a["bids"] if b["agent_id"] == viewer_agent_id] if sealed_open else a["bids"]
        v["bids"] = [_pub_bid(b) for b in bids]
        return v

    def get_auction_detail(self, auction_id: str, viewer_agent_id: str | None = None) -> dict:
        return self.view(self.get_auction(auction_id), viewer_agent_id)

    def list_active_auctions(self, *, category=None, auction_types=None, min_quantity=None, ends_before=None,
                             max_price=None, q=None, include_scheduled=False, viewer_agent_id=None) -> list[dict]:
        """Deterministic pre-filter (spec §11.3) — no LLM involved."""
        self.tick()
        statuses = {"ACTIVE", "EXTENDING"} | ({"SCHEDULED"} if include_scheduled else set())
        now = self.clock.now()
        ql = q.lower() if q else None
        out = []
        for a in self.store.auctions.values():
            if a["status"] not in statuses:
                continue
            spec = a["product_spec"]
            if category and spec["category"].lower() != category.lower():
                continue
            if auction_types and a["auction_type"] not in auction_types:
                continue
            if min_quantity and spec["quantity"] < min_quantity:
                continue
            if ends_before and effective_end(a) > ends_before:
                continue
            if max_price is not None and self.min_next_bid(a, now) > max_price:
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
        if t == "ENGLISH":
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
                "price": result["price"], "quantity": a["product_spec"]["quantity"], "at": now, **({"demo": True} if a.get("demo") else {})})
        self.events.emit("auction.settled", auction_id=a["auction_id"], result=result)

    # ---------- bidding ----------

    def submit_bid(self, *, auction_id, agent_id, amount, bid_type="BID", idempotency_key=None) -> dict:
        """submit_bid — Execution Engine only, never exposed to an LLM. Idempotent per (agent, key)."""
        key = f"{agent_id}:{idempotency_key}" if idempotency_key else None
        if key and key in self.store.idempotency:
            return {**self.store.idempotency[key], "replayed": True}
        res = self._submit(auction_id, agent_id, amount, bid_type, idempotency_key)
        if key and res["ok"]:
            self.store.idempotency[key] = res
        return res

    def _submit(self, auction_id, agent_id, amount, bid_type, idempotency_key) -> dict:
        def fail(code, message, **extra):
            return {"ok": False, "code": code, "message": message, **extra}

        a = self.store.auctions.get(auction_id)
        if not a:
            return fail("AUCTION_NOT_FOUND", "auction not found")
        agent = self.store.agents.get(agent_id)
        if not agent or agent["agent_type"] != "BIDDER":
            return fail("INVALID_BIDDER", "agent is not a bidder")
        if agent["status"] != "ACTIVE":
            return fail("AGENT_NOT_ACTIVE", "agent is not active")
        self._advance(a)
        if not is_open(a):
            return fail("AUCTION_NOT_OPEN", f"auction is {a['status']}")
        seller = self.store.agents.get(a["seller_agent_id"])
        if seller and seller["principal_user_id"] == agent["principal_user_id"]:
            return fail("SELF_BID", "principal cannot bid on their own listing")
        if not is_pos_int(amount):
            return fail("INVALID_AMOUNT", "amount must be a positive integer")

        now = self.clock.now()
        bid = {"bid_id": str(uuid.uuid4()), "agent_id": agent_id, "amount": amount, "at": now,
               "bid_type": bid_type, "idempotency_key": idempotency_key}
        t = a["auction_type"]
        if t == "ENGLISH":
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
