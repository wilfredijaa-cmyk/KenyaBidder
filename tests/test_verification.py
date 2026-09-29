"""SMS/email one-time codes: phone & email verification, self-service password reset, alerts."""
import re

import pytest

from kenyabidder.errors import AppError
from kenyabidder.messaging import AfricasTalkingSms, SmtpEmail


def last_code(env, channel="sms"):
    box = (env.messenger.sms if channel == "sms" else env.messenger.email).sent
    return re.search(r"\b(\d{6})\b", box[-1].get("text")).group(1)


def user(env, name="Wanjiru", phone="+254711000001", email="w@example.com"):
    return env.agents.create_user(name=name, password="password123", phone=phone, email=email)


async def test_phone_verification_happy_path_and_hashed_storage(env):
    u = user(env)
    r = await env.verification.send_phone_code(u["id"])
    assert r["sent_to"].startswith("+254") and "***" in r["sent_to"]
    code = last_code(env)
    assert code not in str(env.store.verifications) and code not in str(env.store.message_log)  # only an HMAC is stored, never the code
    env.verification.confirm_phone(u["id"], code)
    assert env.store.users[u["id"]]["phone_verified"] is True
    with pytest.raises(AppError) as e:
        await env.verification.send_phone_code(u["id"])
    assert e.value.code == "ALREADY_VERIFIED"


async def test_wrong_codes_lock_the_code_and_expiry_is_enforced(env):
    u = user(env)
    await env.verification.send_phone_code(u["id"])
    good = last_code(env)
    for _ in range(5):
        with pytest.raises(AppError):
            env.verification.confirm_phone(u["id"], "000000" if good != "000000" else "111111")
    with pytest.raises(AppError) as e:  # locked: even the right code no longer works
        env.verification.confirm_phone(u["id"], good)
    assert e.value.code == "CODE_INVALID"
    env.clock.advance(61_000)
    await env.verification.send_phone_code(u["id"])
    fresh = last_code(env)
    env.clock.advance(11 * 60_000)
    with pytest.raises(AppError):
        env.verification.confirm_phone(u["id"], fresh)  # expired


async def test_resend_cooldown_hourly_cap_and_supersede(env):
    u = user(env)
    await env.verification.send_phone_code(u["id"])
    first = last_code(env)
    with pytest.raises(AppError) as e:
        await env.verification.send_phone_code(u["id"])
    assert e.value.code == "CODE_COOLDOWN"
    env.clock.advance(61_000)
    await env.verification.send_phone_code(u["id"])
    with pytest.raises(AppError):
        env.verification.confirm_phone(u["id"], first)  # a newer code retires the older one
    for _ in range(3):
        env.clock.advance(61_000)
        await env.verification.send_phone_code(u["id"])
    env.clock.advance(61_000)
    with pytest.raises(AppError) as e:
        await env.verification.send_phone_code(u["id"])
    assert e.value.code == "CODE_LIMIT"


async def test_same_number_cannot_be_used_to_spam_from_many_accounts(env):
    # different accounts can't share a phone, but a number changing hands between accounts is still capped per destination
    users = [user(env, name=f"User{i}", phone=f"+25471100{i:04d}", email=None) for i in range(6)]
    for u in users:
        env.store.users[u["id"]]["phone"] = "+254799999999"  # forced: simulates repeated requests toward one destination
        await env.verification.send_phone_code(u["id"]) if u is not users[-1] else None
    with pytest.raises(AppError) as e:
        await env.verification.send_phone_code(users[-1]["id"])
    assert e.value.code == "CODE_LIMIT"


async def test_changing_the_number_drops_verification_and_stale_codes(env):
    u = user(env)
    await env.verification.send_phone_code(u["id"])
    code = last_code(env)
    env.agents.set_phone(u["id"], "0722 000 111")
    with pytest.raises(AppError):
        env.verification.confirm_phone(u["id"], code)  # code was for the old number
    env.clock.advance(61_000)
    await env.verification.send_phone_code(u["id"])
    env.verification.confirm_phone(u["id"], last_code(env))
    env.agents.set_phone(u["id"], "0733 000 222")
    assert env.store.users[u["id"]]["phone_verified"] is False


async def test_email_verification(env):
    u = user(env)
    await env.verification.send_email_code(u["id"])
    env.verification.confirm_email(u["id"], last_code(env, "email"))
    assert env.store.users[u["id"]]["email_verified"]
    env.agents.set_email(u["id"], "new@example.com")
    assert env.store.users[u["id"]]["email_verified"] is False


async def test_password_reset_via_verified_phone_and_sessions_end(env):
    u = user(env)
    env.store.users[u["id"]].update(phone_verified=True)
    assert env.agents.authenticate("Wanjiru", "password123")
    before = env.store.users[u["id"]]["session_version"] if "session_version" in env.store.users[u["id"]] else 0
    out = await env.verification.request_password_reset("wanjiru")
    code = last_code(env)
    with pytest.raises(AppError):
        env.verification.reset_password("Wanjiru", "123456" if code != "123456" else "654321", "brand-new-pass")
    env.verification.reset_password("Wanjiru", code, "brand-new-pass")
    assert env.agents.authenticate("Wanjiru", "brand-new-pass") and not env.agents.authenticate("Wanjiru", "password123")
    assert env.store.users[u["id"]]["session_version"] == before + 1
    with pytest.raises(AppError):  # a code works once
        env.verification.reset_password("Wanjiru", code, "another-pass-1")
    assert out["message"]


async def test_password_reset_does_not_reveal_which_accounts_exist(env):
    u = user(env)  # phone NOT verified → nothing is sent
    unknown = await env.verification.request_password_reset("nobody-here")
    unverified = await env.verification.request_password_reset("Wanjiru")
    assert unknown == unverified and not env.messenger.sms.sent and not env.messenger.email.sent
    with pytest.raises(AppError) as e1:
        env.verification.reset_password("nobody-here", "123456", "password-xyz-1")
    with pytest.raises(AppError) as e2:
        env.verification.reset_password("Wanjiru", "123456", "password-xyz-1")
    assert (e1.value.code, e1.value.message) == (e2.value.code, e2.value.message)


async def test_reset_requests_are_throttled(env):
    for _ in range(5):
        await env.verification.request_password_reset("Wanjiru")
    with pytest.raises(AppError) as e:
        await env.verification.request_password_reset("Wanjiru")
    assert e.value.code == "TOO_MANY_REQUESTS"


async def test_daily_sms_budget_caps_the_bill(env):
    env.store.settings["sms_daily_budget"] = 2
    users = [user(env, name=f"Buyer{i}", phone=f"+25471100{i:04d}", email=None) for i in range(3)]
    await env.verification.send_phone_code(users[0]["id"])
    await env.verification.send_phone_code(users[1]["id"])
    with pytest.raises(AppError) as e:
        await env.verification.send_phone_code(users[2]["id"])
    assert e.value.code == "SMS_BUDGET"
    env.clock.advance(24 * 3600_000)
    await env.verification.send_phone_code(users[2]["id"])  # new day, new allowance


async def test_delivery_failure_is_reported_and_retryable(make_env):
    class Broken:
        name, real = "broken", True

        async def send(self, to, text):
            raise RuntimeError("gateway down")
    env = make_env(sms=Broken())
    u = user(env)
    with pytest.raises(AppError) as e:
        await env.verification.send_phone_code(u["id"])
    assert e.value.code == "DELIVERY_FAILED"
    assert env.store.message_log[-1]["ok"] is False and "gateway down" in env.store.message_log[-1]["error"]
    env.messenger.sms = FakeOk()
    env.clock.advance(61_000)
    await env.verification.send_phone_code(u["id"])
    assert env.messenger.sms.sent


class FakeOk:
    name, real = "ok", True

    def __init__(self):
        self.sent = []

    async def send(self, to, text):
        self.sent.append((to, text))


# ------------------------------------------------------------------ what verification unlocks

async def test_signup_grant_waits_for_a_verified_phone_when_verification_is_required(env):
    admin = env.agents.create_user(name="Boss", password="password123")
    llm = env.llms.add(name="C", provider="anthropic", model="m", api_key="k")
    env.billing.set_signup_grant(admin, llm["id"], 15_000)
    env.verification.set_phone_required(True)
    u = user(env)
    assert env.wallet.balance(u["id"], llm["id"]) == 0  # unverified: no free tokens for throwaway numbers
    await env.verification.send_phone_code(u["id"])
    env.verification.confirm_phone(u["id"], last_code(env))
    assert env.wallet.balance(u["id"], llm["id"]) == 15_000  # claimed the moment the phone is proven
    env.verification.set_phone_required(False)
    assert env.verification.phone_required() is False


async def test_contact_exchange_requires_a_verified_phone_when_enforced(env):
    _, seller = env.seller()
    ub, buyer = env.bidder()
    aid = env.english(seller["agent_id"])["auction_id"]
    env.engine.submit_bid(auction_id=aid, agent_id=buyer["agent_id"], amount=1500)
    env.clock.advance(61_000)
    env.engine.tick()
    m = next(iter(env.store.matches.values()))
    env.verification.set_phone_required(True)
    with pytest.raises(AppError) as e:
        env.matches.confirm_match(m["match_id"], buyer["agent_id"])
    assert e.value.code == "PHONE_UNVERIFIED"
    env.store.users[ub["id"]]["phone_verified"] = True
    assert env.matches.confirm_match(m["match_id"], buyer["agent_id"])["status"] == "BUYER_CONFIRMED"


def test_verification_is_off_by_default_without_a_real_gateway(env):
    assert env.verification.phone_required() is False
    env.messenger.sms.real = True  # a real gateway switches enforcement on…
    assert env.verification.phone_required() is True
    env.verification.set_phone_required(False)  # …unless an admin overrides it
    assert env.verification.phone_required() is False


async def test_sms_alerts_go_only_to_verified_numbers_and_are_capped(env):
    import asyncio
    u, a = env.bidder(phone="+254711000009")
    env.agents.update(a["agent_id"], memory={"preferred_channel": "SMS"})
    env.router.notify(a["agent_id"], "approval_needed", "Approve a bid")
    await asyncio.sleep(0)
    assert not env.messenger.sms.sent  # phone not verified yet
    env.store.users[u["id"]]["phone_verified"] = True
    for _ in range(20):
        env.router.notify(a["agent_id"], "outbid", "You were outbid")
    env.router.notify(a["agent_id"], "plan", "Chatty plan update")  # not an alert-worthy kind
    await asyncio.sleep(0.05)
    assert len(env.messenger.sms.sent) == 15  # per-user daily cap
    assert all(m["text"].startswith("KenyaBidder:") for m in env.messenger.sms.sent)


async def test_africastalking_adapter_speaks_the_gateway_protocol():
    import httpx
    seen = {}

    def handler(request: httpx.Request):
        seen["headers"], seen["body"], seen["url"] = request.headers, request.content.decode(), str(request.url)
        return httpx.Response(201, json={"SMSMessageData": {"Recipients": [{"statusCode": 101, "status": "Success", "number": "+254711000001"}]}})
    sms = AfricasTalkingSms("sandbox", "KEY", sender_id="KENYABID", sandbox=True, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await sms.send("+254711000001", "hello")
    assert "sandbox.africastalking.com" in seen["url"] and seen["headers"]["apiKey"] == "KEY" and "username=sandbox" in seen["body"] and "from=KENYABID" in seen["body"]
    refuse = AfricasTalkingSms("u", "k", client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(201, json={"SMSMessageData": {"Recipients": [{"statusCode": 403, "status": "InvalidPhoneNumber"}]}}))))
    with pytest.raises(RuntimeError):
        await refuse.send("+254711000001", "x")


def test_env_configuration_picks_the_real_adapters(monkeypatch):
    monkeypatch.setenv("AT_USERNAME", "u")
    monkeypatch.setenv("AT_API_KEY", "k")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    assert AfricasTalkingSms.from_env().real and SmtpEmail.from_env().host == "smtp.example.com"
    monkeypatch.delenv("AT_API_KEY")
    assert AfricasTalkingSms.from_env() is None
