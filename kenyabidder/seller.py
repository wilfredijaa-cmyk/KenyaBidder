"""Listing-agent behaviours: auto-relist unsold lots and daily summaries (spec §1 Persona A)."""
from __future__ import annotations

import logging

log = logging.getLogger("kenyabidder.seller")


class SellerService:
    def __init__(self, store, clock, engine, notify=None):
        self.store, self.clock, self.engine = store, clock, engine
        self.notify = notify or (lambda *a, **k: None)
        engine.events.on("auction.settled", self._on_settled)

    def _on_settled(self, auction_id, result, **_):
        if result["outcome"] == "NO_SALE":
            self.maybe_relist(auction_id)

    def maybe_relist(self, auction_id: str):
        a = self.store.auctions[auction_id]
        seller = self.store.agents.get(a["seller_agent_id"])
        cfg = (seller or {}).get("durable_memory", {}).get("auto_relist")
        if not cfg or seller["status"] != "ACTIVE":
            return None
        title = a["product_spec"]["title"]
        if a["relist_count"] >= cfg.get("max_relists", 3):
            self.notify(seller["agent_id"], "relist", f"\"{title}\" stayed unsold after {a['relist_count']} relists; the agent stopped relisting.", auction_id=auction_id)
            return None
        factor = 1 - cfg.get("discount_pct", 10) / 100
        reserve = max(seller["constraints"].get("reserve_floor", 0), int(a["reserve_price"] * factor))
        kw = dict(seller_agent_id=seller["agent_id"], product_spec=a["product_spec"], auction_type=a["auction_type"],
                  reserve_price=reserve, duration_ms=a["ends_at"] - a["starts_at"], relist_of=a["auction_id"],
                  relist_count=a["relist_count"] + 1)
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

    def summary(self, agent_id: str) -> str:
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
        return f"Daily summary: {sold} sold ({revenue} KES), {unsold} unsold, {live} live listings."
