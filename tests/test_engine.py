import pytest

from kenyabidder.errors import AppError


def test_english_bids_increment_and_highest_wins(env):
    _, s = env.seller()
    _, b1 = env.bidder()
    _, b2 = env.bidder()
    a = env.english(s["agent_id"])
    bid = lambda b, amount: env.engine.submit_bid(auction_id=a["auction_id"], agent_id=b["agent_id"], amount=amount)
    assert bid(b1, 900)["code"] == "BID_TOO_LOW"
    assert bid(b1, 1000)["ok"]
    assert bid(b2, 1050)["code"] == "BID_TOO_LOW"
    assert bid(b1, 1200)["code"] == "ALREADY_HIGHEST"
    assert bid(b2, 1100)["ok"]
    env.clock.advance(60_000)
    d = env.engine.get_auction_detail(a["auction_id"])
    assert d["status"] == "SETTLED"
    assert d["result"] == {"outcome": "SOLD", "winner_agent_id": b2["agent_id"], "price": 1100}


def test_reserve_not_met_is_no_sale_and_reserve_hidden(env):
    _, s = env.seller()
    _, b = env.bidder()
    a = env.english(s["agent_id"], reserve_price=5000, start_price=1000)
    env.engine.submit_bid(auction_id=a["auction_id"], agent_id=b["agent_id"], amount=1000)
    assert "reserve_price" not in env.engine.get_auction_detail(a["auction_id"], b["agent_id"])
    assert env.engine.get_auction_detail(a["auction_id"], s["agent_id"])["reserve_price"] == 5000
    env.clock.advance(60_000)
    assert env.engine.get_auction_detail(a["auction_id"])["result"]["outcome"] == "NO_SALE"
    assert not env.store.matches


def test_anti_sniping_extends_close(env):
    _, s = env.seller()
    _, b = env.bidder()
    a = env.english(s["agent_id"], anti_snipe={"window_ms": 5000, "extend_ms": 10_000})
    env.clock.advance(57_000)
    assert env.engine.submit_bid(auction_id=a["auction_id"], agent_id=b["agent_id"], amount=1000)["ok"]
    d = env.engine.get_auction_detail(a["auction_id"])
    assert d["status"] == "EXTENDING" and d["extended_until"] == a["ends_at"] + 10_000
    env.clock.advance(5_000)
    assert env.engine.get_auction_detail(a["auction_id"])["status"] == "EXTENDING"
    env.clock.advance(8_000)
    assert env.engine.get_auction_detail(a["auction_id"])["status"] == "SETTLED"


DUTCH = {"start_price": 10_000, "floor_price": 5000, "decrement": 500, "interval_ms": 10_000}


def dutch_listing(env, s, **over):
    return env.app.engine.create_listing(seller_agent_id=s["agent_id"], product_spec={"category": "electronics", "title": "Tablets", "quantity": 5},
                                         auction_type="DUTCH", reserve_price=5000, duration_ms=100_000, dutch={**DUTCH, **over})


def test_dutch_decays_and_first_accept_pays_current_price(env):
    _, s = env.seller()
    _, b = env.bidder()
    a = dutch_listing(env, s)
    env.clock.advance(35_000)
    assert env.engine.get_auction_detail(a["auction_id"])["current_price"] == 8500
    assert env.engine.submit_bid(auction_id=a["auction_id"], agent_id=b["agent_id"], amount=8000)["code"] == "BID_TOO_LOW"
    assert env.engine.submit_bid(auction_id=a["auction_id"], agent_id=b["agent_id"], amount=9000)["ok"]
    d = env.engine.get_auction_detail(a["auction_id"])
    assert d["status"] == "SETTLED" and d["result"]["price"] == 8500


def test_dutch_floor_and_unsold(env):
    _, s = env.seller()
    a = dutch_listing(env, s, decrement=5000, interval_ms=1000)
    env.clock.advance(50_000)
    assert env.engine.get_auction_detail(a["auction_id"])["current_price"] == 5000
    env.clock.advance(50_000)
    assert env.engine.get_auction_detail(a["auction_id"])["result"]["outcome"] == "NO_SALE"


@pytest.mark.parametrize("kind,expected", [("FIRST_PRICE_SEALED", 9000), ("SECOND_PRICE_SEALED", 7000)])
def test_sealed_pricing_and_secrecy(env, kind, expected):
    _, s = env.seller()
    _, b1 = env.bidder()
    _, b2 = env.bidder()
    a = env.english(s["agent_id"], auction_type=kind, reserve_price=1000, duration_ms=10_000, start_price=None)
    put = lambda b, amt: env.engine.submit_bid(auction_id=a["auction_id"], agent_id=b["agent_id"], amount=amt, bid_type="SEALED")
    assert put(b1, 500)["code"] == "BELOW_RESERVE"
    assert put(b1, 9000)["ok"] and put(b2, 7000)["ok"]
    seen = env.engine.get_auction_detail(a["auction_id"], b1["agent_id"])
    assert seen["current_price"] is None and len(seen["bids"]) == 1
    env.clock.advance(10_000)
    d = env.engine.get_auction_detail(a["auction_id"])
    assert d["result"]["winner_agent_id"] == b1["agent_id"] and d["result"]["price"] == expected


def test_vickrey_single_bidder_pays_reserve(env):
    _, s = env.seller()
    _, b = env.bidder()
    a = env.english(s["agent_id"], auction_type="SECOND_PRICE_SEALED", reserve_price=1000, duration_ms=1000, start_price=None)
    env.engine.submit_bid(auction_id=a["auction_id"], agent_id=b["agent_id"], amount=8000)
    env.clock.advance(1000)
    assert env.engine.get_auction_detail(a["auction_id"])["result"]["price"] == 1000


def test_idempotent_submit(env):
    _, s = env.seller()
    _, b = env.bidder()
    a = env.english(s["agent_id"])
    args = dict(auction_id=a["auction_id"], agent_id=b["agent_id"], amount=1000, idempotency_key="k1")
    assert env.engine.submit_bid(**args)["ok"]
    again = env.engine.submit_bid(**args)
    assert again["ok"] and again["replayed"]
    assert len(env.engine.get_auction(a["auction_id"])["bids"]) == 1


def test_self_bid_and_listing_validation(env):
    u, s = env.seller(reserve_floor=500)
    a = env.english(s["agent_id"])
    own = env.app.agents.create_agent(user_id=u["id"], type="BIDDER", constraints={"budget_ceiling": 10_000})
    assert env.engine.submit_bid(auction_id=a["auction_id"], agent_id=own["agent_id"], amount=1000)["code"] == "SELF_BID"
    for over, code in [({"reserve_price": 100}, "RESERVE_BELOW_FLOOR"), ({"auction_type": "BOGUS"}, "INVALID_AUCTION_TYPE"),
                       ({"duration_ms": 0}, "INVALID_DURATION"), ({"product_spec": {"category": "x", "title": "y", "quantity": 0}}, "INVALID_SPEC")]:
        with pytest.raises(AppError) as e:
            env.english(s["agent_id"], **over)
        assert e.value.code == code


def test_seller_agent_restricted_to_its_auction_types(env):
    _, s = env.seller(constraints={"authorized_auction_types": ["ENGLISH"]})
    with pytest.raises(AppError) as e:
        env.english(s["agent_id"], auction_type="SECOND_PRICE_SEALED", start_price=None)
    assert e.value.code == "TYPE_NOT_ALLOWED"


def test_scheduled_and_withdraw(env):
    _, s = env.seller()
    _, b = env.bidder()
    a = env.english(s["agent_id"], starts_at=env.clock.now() + 5000)
    assert a["status"] == "SCHEDULED"
    assert env.engine.submit_bid(auction_id=a["auction_id"], agent_id=b["agent_id"], amount=1000)["code"] == "AUCTION_NOT_OPEN"
    env.clock.advance(5000)
    env.engine.submit_bid(auction_id=a["auction_id"], agent_id=b["agent_id"], amount=1000)
    with pytest.raises(AppError) as e:
        env.engine.withdraw_listing(listing_id=a["auction_id"], seller_agent_id=s["agent_id"])
    assert e.value.code == "HAS_BIDS"
    c = env.english(s["agent_id"])
    assert env.engine.withdraw_listing(listing_id=c["auction_id"], seller_agent_id=s["agent_id"])["status"] == "CANCELLED"


def test_prefilter(env):
    _, s = env.seller()
    env.english(s["agent_id"])
    env.english(s["agent_id"], product_spec={"category": "furniture", "title": "Desk", "quantity": 2})
    lst = env.engine.list_active_auctions
    assert len(lst(category="Electronics")) == 1
    assert len(lst(max_price=500)) == 0
    assert len(lst(min_quantity=5)) == 1
    assert len(lst(q="desk")) == 1
