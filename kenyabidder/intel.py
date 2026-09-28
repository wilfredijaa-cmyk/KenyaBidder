"""Market Intelligence (spec §8.3)."""
from __future__ import annotations

from .engine import is_open


def _pct(sorted_vals: list[int], p: float):
    if not sorted_vals:
        return None
    i = (len(sorted_vals) - 1) * p
    lo, hi = int(i), min(int(i) + 1, len(sorted_vals) - 1)
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (i - lo))


class MarketIntel:
    def __init__(self, store, clock, engine):
        self.store, self.clock, self.engine = store, clock, engine

    def historical_clearing_prices(self, category: str, auction_type: str | None = None, since_ms: int | None = None) -> dict:
        cat = (category or "").lower()
        since = self.clock.now() - since_ms if since_ms is not None else float("-inf")
        rows = [r for r in self.store.market_history if r["category"] == cat and (not auction_type or r["auction_type"] == auction_type) and r["at"] >= since]
        prices = sorted(r["price"] for r in rows)
        units = sorted(r["price"] / max(1, r.get("quantity") or 1) for r in rows)  # price per unit: lots of different sizes are comparable
        return {"category": cat, "count": len(prices), "min": prices[0] if prices else None, "p25": _pct(prices, 0.25),
                "median": _pct(prices, 0.5), "p75": _pct(prices, 0.75), "max": prices[-1] if prices else None,
                "unit_median": _pct(units, 0.5), "unit_p25": _pct(units, 0.25)}

    def demand_signal(self, category: str) -> dict:
        cat = (category or "").lower()
        active = bids = 0
        for a in self.store.auctions.values():
            if a["product_spec"]["category"].lower() == cat and is_open(a):
                active += 1
                bids += len(a["bids"])
        watchers = sum(1 for ag in self.store.agents.values()
                       if ag["agent_type"] == "BIDDER" and ag["status"] == "ACTIVE"
                       and ((ag["durable_memory"].get("watch") or {}).get("category") or "").lower() == cat)
        return {"category": cat, "active_auctions": active, "bids_on_active": bids, "bidder_agents_watching": watchers}

    def comparable_active_auctions(self, category: str, title: str = "", exclude_auction_id: str | None = None) -> list[dict]:
        import re
        words = {w for w in re.split(r"\W+", title.lower()) if len(w) > 2}
        out = []
        for a in self.engine.list_active_auctions(category=category):
            if a["auction_id"] == exclude_auction_id:
                continue
            overlap = sum(1 for w in re.split(r"\W+", a["product_spec"]["title"].lower()) if w in words)
            out.append({"auction_id": a["auction_id"], "title": a["product_spec"]["title"],
                        "auction_type": a["auction_type"], "current_price": a["current_price"], "overlap": overlap})
        return sorted(out, key=lambda x: -x["overlap"])

    def recommend_listing(self, category: str, quantity: int = 1, urgency: str = "normal", scarce: bool = False,
                          allowed_types: list[str] | None = None) -> dict:
        """Rule-based seller advisor: Dutch for fast/bulk stock, English for scarce, Vickrey when demand is deep."""
        stats = self.historical_clearing_prices(category)
        demand = self.demand_signal(category)
        if scarce:
            t, why = "ENGLISH", "Scarce or collectible stock: an ascending English auction lets competing bidders reveal their true willingness to pay."
        elif urgency == "high" or quantity >= 20:
            t, why = "DUTCH", "Fast-moving or bulk stock: a descending Dutch auction clears quickly — the first bidder to accept the current price wins."
        elif demand["bidder_agents_watching"] >= 3:
            t, why = "SECOND_PRICE_SEALED", "Several bidder agents are watching this category: a Vickrey auction encourages truthful bids and extracts real value."
        else:
            t, why = "ENGLISH", "Default: English auction is the most transparent format for thin markets."
        if allowed_types and t not in allowed_types:
            alt = allowed_types[0]
            why += f" (This agent is not configured for {t}; falling back to {alt}.)"
            t = alt
        return {"auction_type": t, "reasoning": why, "suggested_reserve": stats["p25"],
                "suggested_start_price": round(stats["p25"] * 0.8) if stats["p25"] is not None else None,
                "market_stats": stats, "demand": demand, "source": "rules"}
