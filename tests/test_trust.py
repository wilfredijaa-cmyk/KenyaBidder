"""Business verification badges, verified-only listings, high-value gating and the dispute workflow."""
import pytest

from kenyabidder.errors import AppError

ADMIN = {"name": "Boss", "id": "admin"}


def verify(env, user, name="Acme Ltd", kind="KRA_PIN", reg="A123456789Z"):
    b = env.trust.apply(user["id"], name, kind, reg)
    env.trust.review(ADMIN, b["id"], True)
    return b


# ------------------------------------------------------------------ verification

def test_application_validation_and_uniqueness(env):
    u1, _ = env.seller()
    u2, _ = env.bidder()
    for kind, reg in (("KRA_PIN", "12345"), ("ID_NUMBER", "abc"), ("BUSINESS_REG", "x")):
        with pytest.raises(AppError) as e:
            env.trust.apply(u1["id"], "Acme", kind, reg)
        assert e.value.code == "INVALID_REGISTRATION"
    env.trust.apply(u1["id"], "Acme Ltd", "KRA_PIN", "a123 456 789z")  # spaces/case are normalised
    with pytest.raises(AppError) as e:
        env.trust.apply(u1["id"], "Acme Ltd", "KRA_PIN", "A123456789Z")
    assert e.value.code == "ALREADY_PENDING"
    with pytest.raises(AppError) as e:  # someone else cannot borrow the same registration
        env.trust.apply(u2["id"], "Copycat", "KRA_PIN", "A123456789Z")
    assert e.value.code == "REGISTRATION_TAKEN"


def test_review_flow_badge_rejection_reapply_and_revoke(env):
    u, _ = env.seller()
    b = env.trust.apply(u["id"], "Acme Ltd", "BUSINESS_REG", "BN-2020/123")
    with pytest.raises(AppError) as e:
        env.trust.review(ADMIN, b["id"], False, "")
    assert e.value.code == "NOTE_REQUIRED"
    env.trust.review(ADMIN, b["id"], False, "certificate unreadable")
    assert not env.trust.is_verified(u["id"])
    with pytest.raises(AppError):
        env.trust.review(ADMIN, b["id"], True)  # already reviewed
    b2 = env.trust.apply(u["id"], "Acme Ltd", "BUSINESS_REG", "BN-2020/123")  # a rejected applicant can try again
    env.trust.review(ADMIN, b2["id"], True)
    assert env.trust.is_verified(u["id"]) and env.store.users[u["id"]]["verified_business"]["name"] == "Acme Ltd"
    with pytest.raises(AppError):
        env.trust.apply(u["id"], "Acme Ltd", "KRA_PIN", "A123456789Z")
    env.trust.revoke(ADMIN, u["id"], "fake certificate")
    assert not env.trust.is_verified(u["id"]) and env.store.businesses[b2["id"]]["status"] == "REVOKED"


def test_verified_only_listings_and_the_high_value_threshold(env):
    _, seller = env.seller()
    ub, verified = env.bidder()
    _, plain = env.bidder()
    verify(env, ub)
    aid = env.english(seller["agent_id"], verified_only=True)["auction_id"]
    assert env.engine.view(env.engine.get_auction(aid))["verified_only"] is True
    r = env.engine.submit_bid(auction_id=aid, agent_id=plain["agent_id"], amount=1500)
    assert r["code"] == "VERIFIED_ONLY"
    assert env.engine.submit_bid(auction_id=aid, agent_id=verified["agent_id"], amount=1500)["ok"]
    open_aid = env.english(seller["agent_id"])["auction_id"]
    env.store.settings["verification_required_above"] = 2_000
    assert env.engine.submit_bid(auction_id=open_aid, agent_id=plain["agent_id"], amount=1_500)["ok"]
    assert env.engine.submit_bid(auction_id=open_aid, agent_id=plain["agent_id"], amount=2_500)["code"] in ("ALREADY_HIGHEST", "VERIFICATION_REQUIRED")
    _, other = env.bidder()
    assert env.engine.submit_bid(auction_id=open_aid, agent_id=other["agent_id"], amount=2_500)["code"] == "VERIFICATION_REQUIRED"
    assert env.engine.submit_bid(auction_id=open_aid, agent_id=verified["agent_id"], amount=2_500)["ok"]


async def test_agents_do_not_spend_tokens_on_listings_they_cannot_join(env):
    _, seller = env.seller()
    _, plain = env.bidder()
    aid = env.english(seller["agent_id"], verified_only=True)["auction_id"]
    r = await env.orchestrator.consider(plain["agent_id"], aid, manual=True)
    assert r["status"] == "FILTERED" and "verified" in r["reason"]


def test_reverse_rfq_can_require_verified_suppliers(env):
    _, buyer = env.bidder()
    us, sup = env.seller()
    aid = env.engine.create_rfq(buyer_agent_id=buyer["agent_id"], product_spec={"category": "x", "title": "y", "quantity": 1}, auction_type="REVERSE_ENGLISH",
                                duration_ms=60_000, max_price=5000, verified_only=True)["auction_id"]
    assert env.engine.submit_bid(auction_id=aid, agent_id=sup["agent_id"], amount=4000)["code"] == "VERIFIED_ONLY"
    verify(env, us)
    assert env.engine.submit_bid(auction_id=aid, agent_id=sup["agent_id"], amount=4000)["ok"]


# ------------------------------------------------------------------ disputes

def revealed_match(env):
    us, seller = env.seller()
    ub, buyer = env.bidder()
    aid = env.english(seller["agent_id"])["auction_id"]
    env.engine.submit_bid(auction_id=aid, agent_id=buyer["agent_id"], amount=1500)
    env.clock.advance(61_000)
    env.engine.tick()
    m = next(iter(env.store.matches.values()))
    env.matches.confirm_match(m["match_id"], seller["agent_id"])
    env.matches.confirm_match(m["match_id"], buyer["agent_id"])
    assert m["status"] == "CONTACT_REVEALED"
    return m, seller, buyer


def test_dispute_lifecycle_ruling_changes_reputation(env):
    m, seller, buyer = revealed_match(env)
    d = env.trust.open_dispute(buyer["agent_id"], m["match_id"], "NOT_DELIVERED", "Paid on Monday, nothing arrived.")
    assert m["dispute_open"] and d["against"] == seller["agent_id"]
    with pytest.raises(AppError) as e:
        env.trust.open_dispute(seller["agent_id"], m["match_id"], "OTHER", "Counter-dispute attempt here")
    assert e.value.code == "ALREADY_DISPUTED"
    with pytest.raises(AppError) as e:
        env.trust.rule(ADMIN, d["id"], "SELLER_AT_FAULT", "n")  # the respondent still has time
    assert e.value.code == "AWAITING_RESPONSE"
    env.trust.add_statement(seller["agent_id"], d["id"], "Courier delivered on Tuesday, signed by the buyer's brother.")
    assert env.store.disputes[d["id"]]["status"] == "RESPONDED"
    # automatic outcome logic must not decide the match while an administrator is reviewing it
    env.matches.report_outcome(m["match_id"], buyer["agent_id"], "FELL_THROUGH")
    env.clock.advance(80 * 3600_000)
    env.matches.expire_stale()
    assert m["status"] == "CONTACT_REVEALED" and m["fault_agent_ids"] == []
    env.trust.rule(ADMIN, d["id"], "BUYER_AT_FAULT", "Delivery receipt shows it was signed for.")
    assert m["status"] == "FELL_THROUGH" and m["fault_agent_ids"] == [buyer["agent_id"]] and not m["dispute_open"]
    assert env.store.agents[buyer["agent_id"]]["reputation"]["fell_through_count"] == 1
    assert env.store.agents[seller["agent_id"]]["reputation"]["fell_through_count"] == 0
    with pytest.raises(AppError):
        env.trust.rule(ADMIN, d["id"], "NO_FAULT", "again")


def test_unanswered_dispute_can_be_ruled_after_the_deadline_and_no_fault_keeps_reputation(env):
    m, seller, buyer = revealed_match(env)
    d = env.trust.open_dispute(seller["agent_id"], m["match_id"], "NOT_PAID", "Buyer never paid the balance.")
    env.clock.advance(8 * 24 * 3600_000)
    env.trust.rule(ADMIN, d["id"], "NO_FAULT", "Neither side provided evidence.")
    assert m["status"] == "DISPUTED" and m["fault_agent_ids"] == []
    assert any(n["kind"] == "dispute" and "ruling" in n["message"].lower() for n in env.router.notifications_for(buyer["agent_id"]))


def test_dispute_rules_of_engagement(env):
    m, seller, buyer = revealed_match(env)
    _, stranger = env.bidder()
    with pytest.raises(AppError):
        env.trust.open_dispute(stranger["agent_id"], m["match_id"], "OTHER", "I am not a party at all.")
    with pytest.raises(AppError) as e:
        env.trust.open_dispute(buyer["agent_id"], m["match_id"], "BOGUS", "Long enough statement.")
    assert e.value.code == "INVALID_CATEGORY"
    with pytest.raises(AppError) as e:
        env.trust.open_dispute(buyer["agent_id"], m["match_id"], "OTHER", "short")
    assert e.value.code == "INVALID_STATEMENT"
    d = env.trust.open_dispute(buyer["agent_id"], m["match_id"], "OTHER", "Something went wrong here.")
    with pytest.raises(AppError):
        env.trust.withdraw(seller["agent_id"], d["id"])  # only the opener may withdraw
    for i in range(2):  # the opening statement counts as the first of three
        env.trust.add_statement(buyer["agent_id"], d["id"], f"More detail number {i}")
    with pytest.raises(AppError) as e:
        env.trust.add_statement(buyer["agent_id"], d["id"], "One statement too many")
    assert e.value.code in ("TOO_MANY_STATEMENTS",)
    env.trust.withdraw(buyer["agent_id"], d["id"])
    assert not m["dispute_open"] and env.store.disputes[d["id"]]["status"] == "WITHDRAWN"


def test_disputes_need_an_exchanged_contact_and_a_recent_one(env):
    us, seller = env.seller()
    ub, buyer = env.bidder()
    aid = env.english(seller["agent_id"])["auction_id"]
    env.engine.submit_bid(auction_id=aid, agent_id=buyer["agent_id"], amount=1500)
    env.clock.advance(61_000)
    env.engine.tick()
    m = next(iter(env.store.matches.values()))
    with pytest.raises(AppError) as e:
        env.trust.open_dispute(buyer["agent_id"], m["match_id"], "OTHER", "Not confirmed yet at all.")
    assert e.value.code == "NOT_DISPUTABLE"
    env.matches.confirm_match(m["match_id"], seller["agent_id"])
    env.matches.confirm_match(m["match_id"], buyer["agent_id"])
    env.clock.advance(31 * 24 * 3600_000)
    with pytest.raises(AppError) as e:
        env.trust.open_dispute(buyer["agent_id"], m["match_id"], "OTHER", "Far too late to complain.")
    assert e.value.code == "WINDOW_CLOSED"


def test_revealed_contact_shows_the_verified_badge(env):
    us, seller = env.seller()
    verify(env, us, name="Amina Traders")
    ub, buyer = env.bidder()
    aid = env.english(seller["agent_id"])["auction_id"]
    env.engine.submit_bid(auction_id=aid, agent_id=buyer["agent_id"], amount=1500)
    env.clock.advance(61_000)
    env.engine.tick()
    m = next(iter(env.store.matches.values()))
    env.matches.confirm_match(m["match_id"], seller["agent_id"])
    env.matches.confirm_match(m["match_id"], buyer["agent_id"])
    assert m["contact_reveal"]["seller_contact"]["verified_business"] == "Amina Traders"
    assert m["contact_reveal"]["buyer_contact"]["verified_business"] is None


def test_withdrawing_a_dispute_lets_pending_reports_take_effect_and_allows_reopening(env):
    m, seller, buyer = revealed_match(env)
    d = env.trust.open_dispute(buyer["agent_id"], m["match_id"], "OTHER", "Something went wrong here.")
    env.matches.report_outcome(m["match_id"], seller["agent_id"], "COMPLETED")
    env.matches.report_outcome(m["match_id"], buyer["agent_id"], "COMPLETED")
    assert m["status"] == "CONTACT_REVEALED"  # held back while the dispute was open
    env.trust.withdraw(buyer["agent_id"], d["id"])
    assert m["status"] == "COMPLETED"
    d2 = env.trust.open_dispute(seller["agent_id"], m["match_id"], "NOT_PAID", "Actually the balance is still owing.")
    assert env.trust.dispute_for_match(m["match_id"])["id"] == d2["id"]
