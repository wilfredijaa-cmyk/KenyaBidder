"""Ideas from the market research: scorecards, quote comparison, savings, watcher alerts, repost, explanations, moderation."""
import pytest

from kenyabidder.errors import AppError
from kenyabidder.insights import EXPLAIN, explain

SPEC = {"category": "electronics", "title": "50 phone chargers", "quantity": 50}
ADMIN = {"name": "Boss", "id": "admin"}


def rfq(env, buyer, **over):
    base = dict(buyer_agent_id=buyer["agent_id"], product_spec=SPEC, auction_type="REVERSE_ENGLISH", duration_ms=60_000, max_price=10_000, min_decrement=100)
    base.update(over)
    return env.engine.create_rfq(**base)


def deal(env, seller, buyer):
    aid = env.english(seller["agent_id"])["auction_id"]
    env.engine.submit_bid(auction_id=aid, agent_id=buyer["agent_id"], amount=1500)
    env.clock.advance(61_000)
    env.engine.tick()
    m = [m for m in env.store.matches.values() if m["auction_id"] == aid][0]
    env.matches.confirm_match(m["match_id"], seller["agent_id"])
    env.matches.confirm_match(m["match_id"], buyer["agent_id"])
    env.matches.report_outcome(m["match_id"], seller["agent_id"], "COMPLETED")
    env.matches.report_outcome(m["match_id"], buyer["agent_id"], "COMPLETED")
    return m


def test_scorecard_reflects_deals_response_time_and_verification(env):
    us, seller = env.seller()
    ub, buyer = env.bidder()
    assert env.insights.scorecard(buyer["agent_id"])["completion_rate"] is None  # no history: nothing invented
    deal(env, seller, buyer)
    card = env.insights.scorecard(buyer["agent_id"])
    assert card["deals"] == 1 and card["completed"] == 1 and card["completion_rate"] == 1.0 and card["win_rate"] == 1.0 and card["first_quote_s"] == 0
    env.store.users[ub["id"]]["verified_business"] = {"name": "Acme"}
    assert "✓ verified" in env.insights.badge_text(env.insights.scorecard(buyer["agent_id"]))
    assert "100% completed" in env.insights.badge_text(card)


def test_quote_comparison_orders_by_best_price_and_hides_sealed_competitors(env):
    _, buyer = env.bidder()
    _, s1 = env.seller()
    _, s2 = env.seller()
    aid = rfq(env, buyer)["auction_id"]
    env.engine.submit_bid(auction_id=aid, agent_id=s1["agent_id"], amount=9_000)
    env.clock.advance(5_000)
    env.engine.submit_bid(auction_id=aid, agent_id=s2["agent_id"], amount=8_800)
    rows = env.insights.comparison(env.engine.view(env.engine.get_auction(aid), buyer["agent_id"]), reverse=True)
    assert [r["amount"] for r in rows] == [8_800, 9_000] and rows[0]["after_s"] == 5 and rows[0]["summary"]
    sealed = rfq(env, buyer, auction_type="REVERSE_SEALED")["auction_id"]
    env.engine.submit_bid(auction_id=sealed, agent_id=s1["agent_id"], amount=7_000)
    env.engine.submit_bid(auction_id=sealed, agent_id=s2["agent_id"], amount=6_000)
    mine = env.insights.comparison(env.engine.view(env.engine.get_auction(sealed), s1["agent_id"]), reverse=True)
    assert [r["agent_id"] for r in mine] == [s1["agent_id"]]  # a competitor's sealed quote stays hidden


def test_rfq_savings_in_the_match_and_platform_totals(env):
    _, buyer = env.bidder()
    _, s1 = env.seller()
    aid = rfq(env, buyer)["auction_id"]
    env.engine.submit_bid(auction_id=aid, agent_id=s1["agent_id"], amount=8_000)
    env.clock.advance(61_000)
    env.engine.tick()
    a = env.engine.get_auction(aid)
    assert env.insights.rfq_savings(a) == {"max_price": 10_000, "price": 8_000, "saved": 2_000, "saved_pct": 20.0}
    m = next(iter(env.store.matches.values()))
    assert m["agreed_terms"]["saved"] == 2_000
    assert env.insights.platform_savings() == {"rfqs_awarded": 1, "total_saved_kes": 2_000}


async def test_watchers_without_auto_bid_are_alerted_to_matching_new_listings(env):
    _, seller = env.seller()
    _, watcher = env.bidder(memory={"auto_bid": False, "watch": {"category": "electronics"}})
    _, other = env.bidder(memory={"auto_bid": False, "watch": {"category": "furniture"}})
    env.english(seller["agent_id"])
    await env.orchestrator.idle()
    assert any(n["kind"] == "watch" for n in env.router.notifications_for(watcher["agent_id"]))
    assert not any(n["kind"] == "watch" for n in env.router.notifications_for(other["agent_id"]))


def test_repost_runs_a_finished_rfq_or_listing_again_with_a_price_nudge(env):
    _, buyer = env.bidder(ceiling=50_000)
    us, seller = env.seller()
    a1 = rfq(env, buyer, duration_ms=1_000)["auction_id"]
    with pytest.raises(AppError) as e:
        env.engine.repost(auction_id=a1, agent_id=buyer["agent_id"])
    assert e.value.code == "STILL_RUNNING"
    env.clock.advance(2_000)
    env.engine.tick()  # nobody quoted
    v = env.engine.repost(auction_id=a1, agent_id=buyer["agent_id"], price_change_pct=10)
    assert v["direction"] == "REVERSE" and v["max_price"] == 11_000 and v["status"] == "ACTIVE"
    with pytest.raises(AppError):
        env.engine.repost(auction_id=a1, agent_id=seller["agent_id"])  # only the poster
    l1 = env.english(seller["agent_id"], duration_ms=1_000)["auction_id"]
    env.clock.advance(2_000)
    env.engine.tick()
    v = env.engine.repost(auction_id=l1, agent_id=seller["agent_id"], price_change_pct=-20)
    assert v["auction_type"] == "ENGLISH" and v["start_price"] == 800 and v["reserve_price"] == 800


def test_every_guardrail_code_has_a_plain_language_explanation():
    for code in ("CEILING_EXCEEDED", "BELOW_FLOOR", "ESCALATION_THRESHOLD", "MARKET_ANOMALY", "NO_TOKENS", "VERIFIED_ONLY", "PLATFORM_PAUSED"):
        assert explain(code) != explain("SOMETHING_NEW") and len(EXPLAIN[code]) > 30
    assert "safety rules" in explain(None)


async def test_the_owner_is_told_why_the_agent_stopped(env):
    _, seller = env.seller()
    _, buyer = env.bidder(ceiling=900)  # the lot opens at 1,000: above the ceiling
    aid = env.english(seller["agent_id"])["auction_id"]
    env.execution.register_trigger(agent_id=buyer["agent_id"], auction_id=aid, kind="ENGLISH_INCREMENTAL", params={"max_bid": 5_000})
    msgs = [n["message"] for n in env.router.notifications_for(buyer["agent_id"]) if n["kind"] == "blocked"]
    assert msgs and "budget ceiling" in msgs[0]


# ------------------------------------------------------------------ moderation

def test_prohibited_items_are_refused_at_creation_and_the_list_is_admin_editable(env):
    _, seller = env.seller()
    with pytest.raises(AppError) as e:
        env.english(seller["agent_id"], product_spec={"category": "misc", "title": "Rhino horn powder", "quantity": 1})
    assert e.value.code == "PROHIBITED_ITEM"
    env.english(seller["agent_id"], product_spec={"category": "tools", "title": "Glue gun set", "quantity": 1})  # no false positive on 'gun'
    _, buyer = env.bidder()
    with pytest.raises(AppError):
        rfq(env, buyer, product_spec={"category": "x", "title": "Need stolen phones", "quantity": 1})
    env.store.settings["prohibited_terms"] = ["glue gun"]
    with pytest.raises(AppError):
        env.english(seller["agent_id"], product_spec={"category": "tools", "title": "Glue guns", "quantity": 1})


def test_reports_auto_hide_unbid_listings_and_admins_resolve_them(env):
    us, seller = env.seller()
    aid = env.english(seller["agent_id"])["auction_id"]
    users = [env.bidder(name=f"Reporter{i}", phone=f"+25471100{i:04d}")[0] for i in range(4)]
    with pytest.raises(AppError):
        env.moderation.report(us["id"], aid, "FRAUD")  # not your own
    env.moderation.report(users[0]["id"], aid, "FRAUD", "asks for deposit")
    with pytest.raises(AppError):
        env.moderation.report(users[0]["id"], aid, "SPAM")  # once per user
    env.moderation.report(users[1]["id"], aid, "FRAUD")
    assert not env.engine.get_auction(aid).get("hidden")
    env.moderation.report(users[2]["id"], aid, "SPAM")
    assert env.engine.get_auction(aid)["hidden"] and aid not in [a["auction_id"] for a in env.engine.list_active_auctions()]
    _, b = env.bidder()
    assert env.engine.submit_bid(auction_id=aid, agent_id=b["agent_id"], amount=1500)["code"] == "AUCTION_UNDER_REVIEW"
    rid = env.moderation.open_reports()[0]["id"]
    with pytest.raises(AppError):
        env.moderation.resolve(ADMIN, rid, "TAKEDOWN", "")
    env.moderation.resolve(ADMIN, rid, "TAKEDOWN", "deposit scam")
    a = env.engine.get_auction(aid)
    assert a["status"] == "CANCELLED" and not a["hidden"] and not env.moderation.open_reports()


def test_a_dismissed_report_unhides_and_a_listing_with_bids_is_never_hidden_by_users(env):
    us, seller = env.seller()
    aid = env.english(seller["agent_id"])["auction_id"]
    reporters = [env.bidder(name=f"R{i}", phone=f"+25471200{i:04d}")[0] for i in range(3)]
    for r in reporters:
        env.moderation.report(r["id"], aid, "MISLEADING")
    assert env.engine.get_auction(aid)["hidden"]
    env.moderation.resolve(ADMIN, env.moderation.open_reports()[0]["id"], "DISMISS")
    assert not env.engine.get_auction(aid)["hidden"]
    bid_aid = env.english(seller["agent_id"])["auction_id"]
    _, b = env.bidder()
    env.engine.submit_bid(auction_id=bid_aid, agent_id=b["agent_id"], amount=1500)
    more = [env.bidder(name=f"Q{i}", phone=f"+25471300{i:04d}")[0] for i in range(4)]
    for r in more:
        env.moderation.report(r["id"], bid_aid, "FRAUD")
    assert not env.engine.get_auction(bid_aid).get("hidden")  # a griefing mob cannot freeze a live auction


def test_ban_poster_suspends_the_account_and_takes_the_listing_down(env):
    env.agents.create_user(name="Boss2", password="password123")  # not the only admin / first user irrelevant
    us, seller = env.seller()
    aid = env.english(seller["agent_id"])["auction_id"]
    r, = [env.bidder(name="Rep", phone="+254711999999")[0]]
    env.moderation.report(r["id"], aid, "FRAUD")
    env.moderation.resolve(ADMIN, env.moderation.open_reports()[0]["id"], "BAN_POSTER", "repeat scammer")
    assert env.store.users[us["id"]]["suspended"] and env.engine.get_auction(aid)["status"] == "CANCELLED"
