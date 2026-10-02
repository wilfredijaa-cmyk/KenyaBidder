"""Regressions from the fourth review round."""
import asyncio

import pytest

from kenyabidder.errors import AppError
from kenyabidder.phone import normalize_phone
from kenyabidder.security import Throttle

DUTCH = {"start_price": 10_000, "floor_price": 5000, "decrement": 500, "interval_ms": 10_000}


def test_dev_code_is_never_shown_on_a_production_site(env, monkeypatch):
    u = env.agents.create_user(name="Wanjiru", password="password123", phone="0712345678")
    r = asyncio.run(env.verification.send_phone_code(u["id"]))
    assert "dev_code" in r  # fine while developing without a gateway
    u["phone_verified"] = False
    for k in env.store.verifications:  # let the cooldown pass
        env.store.verifications[k]["created_at"] -= 10 * 60_000
    monkeypatch.setenv("KENYABIDDER_ENV", "production")
    assert "dev_code" not in asyncio.run(env.verification.send_phone_code(u["id"]))


def test_a_production_site_will_not_create_its_first_admin_without_a_setup_code(env, monkeypatch):
    monkeypatch.setenv("KENYABIDDER_ENV", "production")
    monkeypatch.delenv("KENYABIDDER_BOOTSTRAP_TOKEN", raising=False)
    with pytest.raises(AppError) as e:
        env.agents.create_user(name="Scanner", password="password123")
    assert e.value.code == "SETUP_CODE_REQUIRED" and not env.store.users
    assert env.agents.create_user(name="Operator", password="password123", trusted=True)["role"] == "admin"  # the CLI path
    assert env.agents.create_user(name="Customer", password="password123")["role"] == "user"  # only the very first account is special


def test_a_weak_new_password_does_not_burn_the_reset_code(env):
    env.store.settings["password_policy"] = "strong"
    u = env.agents.create_user(name="Strong One", password="correct horse battery", phone="0712345678")
    u["phone_verified"] = True
    r = asyncio.run(env.verification._issue(u, "RESET", "SMS", u["phone"], lambda c: f"code {c}"))
    with pytest.raises(AppError) as e:
        env.verification.reset_password("Strong One", r["dev_code"], "password1234")
    assert e.value.code == "WEAK_PASSWORD"
    env.verification.reset_password("Strong One", r["dev_code"], "mombasa-sunset-42")  # the same code still works
    assert env.agents.authenticate("Strong One", "mombasa-sunset-42")


def test_dutch_price_after_the_sale_is_the_price_it_sold_at(env):
    _, s = env.seller()
    _, b = env.bidder()
    a = env.engine.create_listing(seller_agent_id=s["agent_id"], product_spec={"category": "electronics", "title": "Tablets", "quantity": 5},
                                  auction_type="DUTCH", reserve_price=5000, duration_ms=100_000, dutch=DUTCH)
    env.clock.advance(10_000)
    assert env.engine.submit_bid(auction_id=a["auction_id"], agent_id=b["agent_id"], amount=10_000)["ok"]
    env.clock.advance(80_000)
    d = env.engine.get_auction_detail(a["auction_id"])
    assert d["status"] == "SETTLED" and d["current_price"] == d["result"]["price"] == 9500


def test_dismissing_reports_on_a_finished_listing_reaches_the_database(env):
    from conftest import fresh_database
    from kenyabidder.persist import StatePersistence
    _, s = env.seller()
    users = [env.agents.create_user(name=f"Rep{i}", password="password123") for i in range(3)]
    a = env.english(s["agent_id"], duration_ms=60_000)
    for u in users:
        env.moderation.report(u["id"], a["auction_id"], "SPAM")
    assert env.engine.get_auction(a["auction_id"])["hidden"]
    env.clock.advance(61_000)
    env.engine.tick()
    db = fresh_database()
    p = StatePersistence(db, env.clock)
    p.flush_all(env.store)
    admin = {"id": None, "name": "root"}
    env.moderation.resolve(admin, env.moderation.open_reports()[0]["id"], "DISMISS")
    p.flush_all(env.store)
    assert p.load().auctions[a["auction_id"]]["hidden"] is False


def test_a_hidden_unsold_listing_is_not_quietly_relisted(env):
    _, s = env.seller(memory={"auto_relist": {"max_relists": 2, "discount_pct": 10}})
    users = [env.agents.create_user(name=f"Rep{i}", password="password123") for i in range(3)]
    a = env.english(s["agent_id"], duration_ms=60_000)
    for u in users:
        env.moderation.report(u["id"], a["auction_id"], "SPAM")
    env.clock.advance(61_000)
    env.engine.tick()
    assert len(env.store.auctions) == 1


def test_forgiving_a_success_takes_back_its_own_hit_not_another_requests():
    t = Throttle()
    mine = t.hit("login", "ip", 3, 60)
    t.hit("login", "ip", 3, 60)  # a concurrent failed guess
    t.forgive("login", "ip", mine)
    assert t.count("login", "ip", 60) == 1


def test_only_ascii_digits_make_a_phone_number():
    assert normalize_phone("0712345678") == "+254712345678"
    assert normalize_phone("07١٢345678") is None
    assert normalize_phone("+٢٥٤712345678") is None


def test_saving_your_own_unchanged_phone_is_not_a_conflict(env):
    a = env.agents.create_user(name="First One", password="password123", phone="0712345678")
    b = env.agents.create_user(name="Second One", password="password123", phone="0712345678")
    assert env.agents.set_phone(b["id"], "0712345678")["phone"] == "+254712345678"
    c = env.agents.create_user(name="Third One", password="password123", phone="0722000000")
    with pytest.raises(AppError) as e:
        env.agents.set_phone(c["id"], "0712345678")
    assert e.value.code == "PHONE_TAKEN"


def test_account_deletion_scrubs_the_number_and_name_from_logs(env):
    env.agents.create_user(name="Boss", password="password123")  # the first account is the administrator
    u = env.agents.create_user(name="Wanjiru", password="password123", phone="0712345678")
    env.store.outbox.append({"channel": "WHATSAPP", "to": "254712345678", "text": "hi Wanjiru", "at": 1})
    env.store.admin_log.append({"at": 1, "admin": "root", "action": "suspend", "detail": {"user": "Wanjiru", "phone": "+254712345678"}})
    env.store.admin_log.append({"at": 1, "admin": "root", "action": "suspend", "detail": {"user": "Wanjiruku"}})
    env.privacy.delete_account(u["id"], "password123")
    text = str(env.store.outbox) + str(env.store.admin_log)
    assert "'Wanjiru'" not in text and "hi Wanjiru" not in text and "254712345678" not in text
    assert "Wanjiruku" in text  # other people's records are left alone


def test_deletion_scrub_of_old_log_rows_reaches_the_database(env):
    from conftest import fresh_database
    from kenyabidder.persist import StatePersistence
    env.agents.create_user(name="Boss", password="password123")
    u = env.agents.create_user(name="Wanjiru", password="password123", phone="0712345678")
    env.store.outbox.append({"channel": "WHATSAPP", "to": "254712345678", "text": "hi", "at": 1})
    env.store.admin_log.append({"at": 1, "admin": "admin", "action": "admin", "detail": {"phone": "0712 345 678", "n": "0712345678"}})
    for i in range(300):  # push the sensitive rows far from the tail the incremental writer re-checks
        env.store.outbox.append({"channel": "WHATSAPP", "to": "x", "text": str(i), "at": i})
    db = fresh_database()
    p = StatePersistence(db, env.clock)
    p.flush_all(env.store)
    env.privacy.delete_account(u["id"], "password123")
    p.flush_all(env.store)
    loaded = p.load()
    assert "254712345678" not in str(loaded.outbox) and "0712345678" not in str(loaded.admin_log)
    assert loaded.admin_log[0]["admin"] == "admin" and loaded.admin_log[0]["action"] == "admin"  # actor/action fields untouched


def test_an_unsold_dutch_lot_shows_its_clock_price_not_a_stale_one(env):
    _, s = env.seller()
    a = env.engine.create_listing(seller_agent_id=s["agent_id"], product_spec={"category": "electronics", "title": "Tablets", "quantity": 5},
                                  auction_type="DUTCH", reserve_price=5000, duration_ms=100_000, dutch=DUTCH)
    env.clock.advance(100_000)
    d = env.engine.get_auction_detail(a["auction_id"])
    assert d["status"] == "SETTLED" and d["result"]["outcome"] == "NO_SALE" and d["current_price"] == 5000


def test_a_war_that_climbs_by_itself_pauses_instead_of_blocking_for_good(env):
    _, s = env.seller()
    _, rival = env.bidder(ceiling=10**7)
    _, b = env.bidder(ceiling=10**7)
    for _ in range(5):
        env.store.market_history.append({"category": "electronics", "auction_type": "ENGLISH", "price": 1000, "quantity": 1, "at": 1})
    a = env.english(s["agent_id"], start_price=1000, reserve_price=1000, duration_ms=3600_000, product_spec={"category": "electronics", "title": "T", "quantity": 1})
    assert env.app.manual_bid(rival["agent_id"], a["auction_id"], 90_000)["ok"]  # a human takes the price far above the going rate
    r = env.guardrail.evaluate({"agent_id": b["agent_id"], "auction_id": a["auction_id"], "action": "bid", "amount": 90_100, "source": "strategy"})
    assert r["code"] == "MARKET_ANOMALY" and not r.get("permanent")


def test_create_admin_from_the_shell_works_even_when_a_setup_code_is_configured(env, monkeypatch):
    monkeypatch.setenv("KENYABIDDER_BOOTSTRAP_TOKEN", "abc")
    with pytest.raises(AppError):
        env.agents.create_user(name="Scanner", password="password123")
    assert env.agents.create_user(name="Operator", password="password123", trusted=True)["role"] == "admin"


def test_config_warns_about_a_missing_bootstrap_token_in_production():
    from kenyabidder.config import validate
    good = {"KENYABIDDER_ENV": "production", "KENYABIDDER_PUBLIC_URL": "https://x.example.com"}
    assert any("BOOTSTRAP" in w for w in validate(good).warnings)
    assert not any("BOOTSTRAP" in w for w in validate({**good, "KENYABIDDER_BOOTSTRAP_TOKEN": "abc"}).warnings)


def test_spaced_phone_spellings_and_actor_names_are_scrubbed_from_logs(env):
    env.agents.create_user(name="Boss", password="password123")
    u = env.agents.create_user(name="Wanjiru", password="password123", phone="0712345678")
    env.store.admin_log.append({"at": 1, "admin": "Wanjiru", "action": "x", "detail": {"t": "call +254 712 345 678 or 0712-345-678"}})
    env.privacy.delete_account(u["id"], "password123")
    text = str(env.store.admin_log)
    assert "Wanjiru" not in text and "[deleted]" in text
    assert "345 678" not in text and "345-678" not in text
