"""Regressions from the fourth review round."""
import asyncio

import pytest

from kenyabidder.config import validate
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


def test_production_refuses_to_start_without_a_bootstrap_token():
    good = {"KENYABIDDER_ENV": "production", "KENYABIDDER_PUBLIC_URL": "https://x.example.com"}
    assert any("BOOTSTRAP" in e for e in validate(good).errors)
    assert not any("BOOTSTRAP" in e for e in validate({**good, "KENYABIDDER_BOOTSTRAP_TOKEN": "abc"}).errors)


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
