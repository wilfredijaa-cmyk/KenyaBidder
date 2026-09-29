"""Data export and account deletion (Kenya Data Protection Act)."""
import json

import pytest

from kenyabidder.errors import AppError


def deal(env):
    us, seller = env.seller(name="Amina")
    ub, buyer = env.bidder(name="David", phone="+254700000009")
    aid = env.english(seller["agent_id"])["auction_id"]
    env.engine.submit_bid(auction_id=aid, agent_id=buyer["agent_id"], amount=1500)
    env.clock.advance(61_000)
    env.engine.tick()
    m = next(iter(env.store.matches.values()))
    env.matches.confirm_match(m["match_id"], seller["agent_id"])
    env.matches.confirm_match(m["match_id"], buyer["agent_id"])
    return us, seller, ub, buyer, m


def test_export_has_my_data_and_nothing_secret_or_foreign(env):
    us, seller, ub, buyer, m = deal(env)
    env.wallet.credit(ub["id"], "L1", 500, "GRANT", ref="g1")
    out = json.loads(env.privacy.export_json(ub["id"]))
    assert out["profile"]["name"] == "David" and out["profile"]["phone"] == "+254700000009"
    assert [a["agent_id"] for a in out["agents"]] == [buyer["agent_id"]]
    assert out["matches"][0]["counterparty"] == "Amina" and out["token_balances"] == {"L1": 500} and len(out["token_ledger"]) == 1
    text = json.dumps(out)
    assert "password_hash" not in text and "scrypt$" not in text
    assert us["phone"] not in text and "amina@example.com" not in text  # the counterparty's private details are not ours to hand out


def test_deletion_requires_the_password_and_resolves_blockers_first(env):
    us, seller, ub, buyer, m = deal(env)
    with pytest.raises(AppError) as e:
        env.privacy.delete_account(ub["id"], "wrong-password")
    assert e.value.code == "WRONG_PASSWORD"
    with pytest.raises(AppError) as e:
        env.privacy.delete_account(ub["id"], "password123")
    assert e.value.code == "DELETION_BLOCKED" and "live deal" in e.value.message  # contact revealed, outcome not reported yet
    env.matches.report_outcome(m["match_id"], seller["agent_id"], "COMPLETED")
    env.matches.report_outcome(m["match_id"], buyer["agent_id"], "COMPLETED")
    env.privacy.delete_account(ub["id"], "password123")


def test_deletion_anonymises_personal_data_but_keeps_financial_records(env):
    us, seller, ub, buyer, m = deal(env)
    env.matches.report_outcome(m["match_id"], seller["agent_id"], "COMPLETED")
    env.matches.report_outcome(m["match_id"], buyer["agent_id"], "COMPLETED")
    env.wallet.credit(ub["id"], "L1", 500, "TOPUP", ref="o1")
    with pytest.raises(AppError) as e:
        env.privacy.delete_account(ub["id"], "password123")
    assert e.value.code == "TOKENS_HELD"
    env.privacy.delete_account(ub["id"], "password123", forfeit_tokens=True)
    u = env.store.users[ub["id"]]
    assert u["name"].startswith("deleted-") and u["phone"] is None and u["email"] is None and u["suspended"] and u["deleted_at"]
    assert env.agents.authenticate("David", "password123") is None
    assert env.agents.authenticate(u["name"], "password123") is None
    assert env.store.agents[buyer["agent_id"]]["status"] == "SUSPENDED"
    assert env.wallet.balance(ub["id"], "L1") == 0 and len(env.wallet.ledger(user_id=ub["id"])) >= 2  # ledger stays (forfeit is recorded)
    assert env.wallet.verify_integrity() == []
    assert m["contact_reveal"]["buyer_contact"] == {"name": "deleted user", "phone": None, "email": None, "verified_business": None}
    assert m["contact_reveal"]["seller_contact"]["name"] == "Amina"  # the other party's data is untouched
    assert "David" not in json.dumps([n for n in env.store.notifications])
    env.agents.create_user(name="David", password="password123", phone="+254700000009")  # the name and phone are free again
    with pytest.raises(AppError):
        env.privacy.export(ub["id"])


def test_deleted_accounts_cannot_request_codes_or_reset_passwords(env):
    import asyncio
    us, seller, ub, buyer, m = deal(env)
    env.matches.report_outcome(m["match_id"], seller["agent_id"], "COMPLETED")
    env.matches.report_outcome(m["match_id"], buyer["agent_id"], "COMPLETED")
    env.privacy.delete_account(ub["id"], "password123")
    asyncio.run(env.verification.request_password_reset("David"))
    assert not env.messenger.sms.sent


def test_the_only_administrator_cannot_delete_themselves(env):
    admin = env.agents.create_user(name="Boss", password="password123")
    assert admin["role"] == "admin"
    with pytest.raises(AppError) as e:
        env.privacy.delete_account(admin["id"], "password123")
    assert "only administrator" in e.value.message


def test_deleted_users_notifications_do_not_come_back_from_the_database(env):
    from kenyabidder.db import open_database
    from kenyabidder.persist import StatePersistence
    us, seller, ub, buyer, m = deal(env)
    env.matches.report_outcome(m["match_id"], seller["agent_id"], "COMPLETED")
    env.matches.report_outcome(m["match_id"], buyer["agent_id"], "COMPLETED")
    assert any(n["agent_id"] == buyer["agent_id"] for n in env.store.notifications)
    db = open_database(":memory:")
    p = StatePersistence(db)
    p.flush_all(env.store)  # the notifications were durable before the deletion…
    env.privacy.delete_account(ub["id"], "password123")
    p.flush_all(env.store)
    back = StatePersistence(db).load()  # …and are gone after a restart
    assert not any(n["agent_id"] == buyer["agent_id"] for n in back.notifications)
    assert "David" not in json.dumps(back.users) and back.users[ub["id"]]["phone"] is None
