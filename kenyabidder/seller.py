"""Listing-agent behaviours: auto-relist unsold lots and daily summaries (spec §1 Persona A)."""
from __future__ import annotations

import logging

from .timeutil import eat

log = logging.getLogger("kenyabidder.seller")


class SellerService:
    def __init__(self, store, clock, engine, notify=None):
        self.store, self.clock, self.engine = store, clock, engine
        self.notify = notify or (lambda *a, **k: None)
        engine.events.on("auction.settled", self._on_settled)

    def _on_settled(self, auction_id, result, **_):
        a = self.store.auctions.get(auction_id) or {}
        if result["outcome"] == "NO_SALE" and a.get("direction") == "REVERSE":
            if a.get("poster_agent_id"):
                self.notify(a["poster_agent_id"], "rfq", f"Your RFQ \"{a['product_spec']['title']}\" closed without any supplier quote. Raise the maximum price or widen the specification and repost.", auction_id=auction_id)
        elif result["outcome"] == "NO_SALE":
            self.maybe_relist(auction_id)

    def maybe_relist(self, auction_id: str):
        a = self.store.auctions[auction_id]
        seller = self.store.agents.get(a["seller_agent_id"])
        cfg = (seller or {}).get("durable_memory", {}).get("auto_relist")
        if not cfg or seller["status"] != "ACTIVE":
            return None
        if a.get("hidden") or a["status"] == "CANCELLED":
            return None  # under moderator review or taken down: not for the agent to quietly re-run
        title = a["product_spec"]["title"]
        if a["relist_count"] >= cfg.get("max_relists", 3):
            self.notify(seller["agent_id"], "relist", f"\"{title}\" stayed unsold after {a['relist_count']} relists; the agent stopped relisting.", auction_id=auction_id)
            return None
        factor = 1 - cfg.get("discount_pct", 10) / 100
        reserve = max(seller["constraints"].get("reserve_floor", 0), int(a["reserve_price"] * factor))
        kw = dict(seller_agent_id=seller["agent_id"], product_spec=a["product_spec"], auction_type=a["auction_type"], verified_only=a.get("verified_only", False),
                  reserve_price=reserve, duration_ms=a["ends_at"] - a["starts_at"], relist_of=a["auction_id"],
                  relist_count=a["relist_count"] + 1, demo=a.get("demo", False))  # a relisted demo lot must stay a demo lot
        if a["auction_type"] == "ENGLISH":
            kw.update(start_price=min(reserve, max(0, int(a["start_price"] * factor))),
                      min_increment=a["min_increment"], anti_snipe=a["anti_snipe"])
        elif a["auction_type"] == "DUTCH":
            d = a["dutch"]
            floor_price = max(reserve, int(d["floor_price"] * factor))
            kw["dutch"] = {**d, "start_price": max(floor_price + 1, int(d["start_price"] * factor)), "floor_price": floor_price}
        try:
            created = self.engine.create_listing(**kw)
        except Exception as e:  # noqa: BLE001
            self.notify(seller["agent_id"], "relist", f"Auto-relist failed: {e}", auction_id=auction_id)
            return None
        self.notify(seller["agent_id"], "relist", f"\"{title}\" did not sell. Relisted with reserve {reserve} (attempt {a['relist_count'] + 1}).", auction_id=created["auction_id"])
        return created

    SUMMARY_HOUR_EAT = 18

    def send_daily_summaries(self) -> int:
        """Persona A: 'sends her a WhatsApp summary each evening'. Once per EAT day per seller, from 18:00 EAT,
        and only if there was anything to report."""
        now = self.clock.now()
        local = eat(now)
        if local.hour < self.SUMMARY_HOUR_EAT:
            return 0
        day = local.strftime("%Y-%m-%d")
        n = 0
        for agent in list(self.store.agents.values()):
            mem = agent["durable_memory"]
            if agent["agent_type"] != "SELLER" or agent["status"] != "ACTIVE" or not mem.get("daily_summary") or mem.get("last_summary_day") == day:
                continue
            st = self.stats(agent["agent_id"])
            if st["sold"] + st["unsold"] + st["live"] == 0:
                continue  # nothing to report yet: try again later this evening
            mem["last_summary_day"] = day
            self.notify(agent["agent_id"], "summary", self.summary(agent["agent_id"]))
            n += 1
        return n

    def stats(self, agent_id: str) -> dict:
        since = self.clock.now() - 24 * 3600_000
        sold = revenue = unsold = live = 0
        for a in self.store.auctions.values():
            if a["seller_agent_id"] != agent_id:
                continue
            if a["status"] in ("ACTIVE", "EXTENDING", "SCHEDULED"):
                live += 1
            if a["status"] == "SETTLED" and (a["closed_at"] or 0) >= since:
                if a["result"]["outcome"] == "SOLD":
                    sold += 1
                    revenue += a["result"]["price"]
                else:
                    unsold += 1
        return {"sold": sold, "revenue": revenue, "unsold": unsold, "live": live}

    def summary(self, agent_id: str) -> str:
        st = self.stats(agent_id)
        return f"Daily summary: {st['sold']} sold ({st['revenue']} KES), {st['unsold']} unsold, {st['live']} live listings."
