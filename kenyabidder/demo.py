"""Demo marketplace: realistic Kenyan B2B listings plus price history, so a fresh install is not an empty room.

Everything created here is flagged ``demo``: demo listings never produce real matches or contact reveals, and
:func:`clear_demo` removes it all again.
"""
from __future__ import annotations

import random
import secrets

from .errors import AppError

HOUR = 3600_000
DAY = 24 * HOUR

# (category, typical clearing price in KES for a lot) — used to seed market_history
HISTORY = {
    "electronics": [13_800, 14_500, 15_200, 14_900, 15_800, 16_300, 14_200, 15_500],
    "furniture": [24_000, 26_500, 25_200, 27_800, 23_900, 26_100],
    "agriculture": [88_000, 91_500, 86_400, 93_200, 90_100, 89_700, 92_800],
    "building": [61_000, 63_500, 59_800, 64_200, 62_100],
}

LOT_SIZE = {"electronics": 10, "furniture": 8, "agriculture": 40, "building": 60}  # typical lot each history price refers to

SELLERS = [
    ("Demo - Nairobi Electronics Hub", "electronics"),
    ("Demo - Kisumu Office Supplies", "furniture"),
    ("Demo - Nakuru Grain Traders", "agriculture"),
    ("Demo - Mombasa Hardware", "building"),
]

LISTINGS = [
    # seller idx, title, category, qty, type, reserve, start/dutch-start, extra
    (0, "Samsung Galaxy A15 (128GB) — carton of 10", "electronics", 10, "ENGLISH", 14_000, 12_000, {"min_increment": 250}),
    (0, "HP 15 laptops, refurbished — lot of 5", "electronics", 5, "SECOND_PRICE_SEALED", 60_000, None, {}),
    (1, "Ergonomic office chairs — 12 units", "furniture", 12, "DUTCH", 22_000, 32_000, {"floor": 24_000, "dec": 500}),
    (1, "Steel filing cabinets — 6 units", "furniture", 6, "ENGLISH", 18_000, 15_000, {"min_increment": 500}),
    (2, "Dry maize, 50kg bags — 40 bags", "agriculture", 40, "DUTCH", 80_000, 100_000, {"floor": 85_000, "dec": 1_000}),
    (2, "Beans (Rosecoco), 90kg bags — 20 bags", "agriculture", 20, "FIRST_PRICE_SEALED", 70_000, None, {}),
    (3, "Bamburi cement 50kg — 100 bags", "building", 100, "ENGLISH", 58_000, 52_000, {"min_increment": 1_000}),
    (3, "Roofing sheets (gauge 28) — 60 sheets", "building", 60, "SECOND_PRICE_SEALED", 55_000, None, {}),
]


def seed_demo(app, *, seed: int | None = None) -> dict:
    """Create demo sellers, price history and live listings. Idempotent: refuses to run twice."""
    store = app.store
    if any(u.get("demo") for u in store.users.values()):
        raise AppError("DEMO_EXISTS", "demo data is already loaded — clear it first", 409)
    rnd = random.Random(seed)
    now = app.clock.now()
    sellers: list[dict] = []
    for name, _cat in SELLERS:
        u = app.agents.create_user(name=name, password=secrets.token_urlsafe(24))  # nobody can log in as a demo seller
        u["demo"] = True
        agent = app.agents.create_agent(user_id=u["id"], type="SELLER", memory={"auto_relist": {"max_relists": 2, "discount_pct": 8}})
        agent["demo"] = True
        sellers.append(agent)
    for cat, prices in HISTORY.items():
        for i, price in enumerate(prices):
            store.market_history.append({"category": cat, "auction_type": rnd.choice(["ENGLISH", "DUTCH", "SECOND_PRICE_SEALED"]), "price": price + rnd.randint(-400, 400),
                                         "quantity": LOT_SIZE[cat], "at": now - (i + 1) * 2 * DAY, "demo": True})
    created = []
    for idx, title, cat, qty, typ, reserve, start, extra in LISTINGS:
        dur = rnd.choice([2, 3, 4, 6]) * HOUR
        kw = dict(seller_agent_id=sellers[idx]["agent_id"], product_spec={"category": cat, "title": title, "quantity": qty}, auction_type=typ,
                  reserve_price=reserve, duration_ms=dur, demo=True)
        if typ == "ENGLISH":
            kw.update(start_price=start, min_increment=extra["min_increment"], anti_snipe={"window_ms": 30_000, "extend_ms": 60_000})
        elif typ == "DUTCH":
            kw["dutch"] = {"start_price": start, "floor_price": extra["floor"], "decrement": extra["dec"], "interval_ms": max(60_000, dur // 12)}
        created.append(app.engine.create_listing(**kw)["auction_id"])
    return {"sellers": len(sellers), "listings": len(created), "history_points": sum(len(v) for v in HISTORY.values())}


def clear_demo(app) -> dict:
    """Remove demo users, their agents, listings, plans and price history (real users' own data is untouched)."""
    store = app.store
    demo_users = {uid for uid, u in store.users.items() if u.get("demo")}
    demo_agents = {aid for aid, a in store.agents.items() if a["principal_user_id"] in demo_users}
    demo_auctions = {i for i, a in store.auctions.items() if a.get("demo") or a["seller_agent_id"] in demo_agents}
    for t in list(store.triggers.values()):
        if t["auction_id"] in demo_auctions or t["agent_id"] in demo_agents:
            del store.triggers[t["id"]]
    for ap in list(store.approvals.values()):
        if ap["auction_id"] in demo_auctions or ap["agent_id"] in demo_agents:
            del store.approvals[ap["id"]]
    for m in list(store.matches.values()):
        if m["auction_id"] in demo_auctions:
            del store.matches[m["match_id"]]
    for i in demo_auctions:
        del store.auctions[i]
    for a in demo_agents:
        del store.agents[a]
    for u in demo_users:
        del store.users[u]
    before = len(store.market_history)
    store.market_history[:] = [h for h in store.market_history if not h.get("demo")]
    store.considered = {k for k in store.considered if k.split(":")[1] not in demo_auctions}
    for k in [k for k in app.orchestrator.blocked if k[1] in demo_auctions]:
        del app.orchestrator.blocked[k]
    return {"users": len(demo_users), "agents": len(demo_agents), "listings": len(demo_auctions), "history_points": before - len(store.market_history)}
