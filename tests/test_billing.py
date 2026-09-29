import json

import httpx
import pytest

from kenyabidder.errors import AppError
from kenyabidder.payments import MpesaClient, MpesaConfig
from kenyabidder.phone import mpesa_msisdn, normalize_phone



@pytest.fixture
def shop(env):
    """A metered LLM with one pack on sale, an admin, and a customer."""
    admin = env.agents.create_user(name="Boss", password="password123")
    llm = env.llms.add(name="Claude", provider="anthropic", model="m", api_key="k", cost_per_1k_kes=0.5)
    pack = env.billing.add_pack(admin, name="Starter", llm_id=llm["id"], tokens=100_000, price_kes=500)
    user, agent = env.bidder(name="Customer")
    env.billing.manual_instructions  # noqa: B018
    env.billing.set_manual_instructions(admin, "Pay to Till 123456")
    return type("Shop", (), {"env": env, "admin": admin, "llm": llm, "pack": pack, "user": user, "agent": agent})


# ------------------------------------------------------------------ phone

@pytest.mark.parametrize("raw,expected", [
    ("0712 345 678", "+254712345678"), ("+254712345678", "+254712345678"), ("254712345678", "+254712345678"),
    ("712345678", "+254712345678"), ("0112345678", "+254112345678"), ("(0712) 345-678", "+254712345678"),
    ("+44 7911 123456", "+447911123456"),
    ("", None), (None, None), ("abc", None), ("0812345678", None), ("+2547123", None), ("07123456789", None), ("+0712345678", None)])
def test_phone_normalization(raw, expected):
    assert normalize_phone(raw) == expected


def test_mpesa_msisdn_only_for_kenyan_numbers():
    assert mpesa_msisdn("0712345678") == "254712345678"
    assert mpesa_msisdn("+447911123456") is None and mpesa_msisdn(None) is None


# ------------------------------------------------------------------ packs

def test_pack_validation_and_free_llm_cannot_be_sold(shop):
    b, admin, llm = shop.env.billing, shop.admin, shop.llm
    free = shop.env.llms.add(name="Local", provider="openai_compatible", model="l", base_url="http://x/v1", billing_mode="free")
    for kw in ({"tokens": 10}, {"tokens": 5_000, "price_kes": 5}, {"price_kes": 999_999}, {"price_kes": 12.5}, {"name": " "}, {"llm_id": free["id"]}):
        with pytest.raises(AppError) as e:
            b.add_pack(admin, **{"name": "P", "llm_id": llm["id"], "tokens": 5_000, "price_kes": 100, **kw})
        assert e.value.code == "INVALID_PACK"
    assert [p["name"] for p in b.list_packs(enabled_only=True)] == ["Starter"]
    b.update_pack(admin, shop.pack["id"], enabled=False)
    assert b.list_packs(enabled_only=True) == []
    shop.env.llms.update(llm["id"], enabled=False)
    b.update_pack(admin, shop.pack["id"], enabled=True)
    assert b.list_packs(enabled_only=True) == []          # a pack for a disabled LLM is not sold
    assert [a["action"] for a in shop.env.store.admin_log][:2] == ["add_pack", "set_manual_payment_instructions"]


# ------------------------------------------------------------------ grants

def test_signup_grant_is_once_per_phone_number(shop):
    env = shop.env
    env.billing.set_signup_grant(shop.admin, shop.llm["id"], 20_000)
    a = env.agents.create_user(name="Newcomer One", password="password123", phone="0712 000 111")
    assert env.wallet.balance(a["id"], shop.llm["id"]) == 20_000
    b = env.agents.create_user(name="Newcomer Two", password="password123", phone="+254712000111")  # same person, new account
    assert env.wallet.balance(b["id"], shop.llm["id"]) == 0
    c = env.agents.create_user(name="No Phone", password="password123")
    assert env.wallet.balance(c["id"], shop.llm["id"]) == 0
    assert env.billing.grant_signup_tokens(a) == []       # idempotent
    env.billing.set_signup_grant(shop.admin, shop.llm["id"], 0)
    d = env.agents.create_user(name="Late Comer", password="password123", phone="0722000222")
    assert env.wallet.balance(d["id"], shop.llm["id"]) == 0
    with pytest.raises(AppError):
        env.billing.set_signup_grant(shop.admin, shop.llm["id"], -5)


def test_admin_grant_adjust_are_audited_and_reasoned(shop):
    b, env = shop.env.billing, shop.env
    b.admin_grant(shop.admin, shop.user["id"], shop.llm["id"], 5_000, "goodwill")
    assert env.wallet.balance(shop.user["id"], shop.llm["id"]) == 5_000
    with pytest.raises(AppError):
        b.admin_adjust(shop.admin, shop.user["id"], shop.llm["id"], -100, "  ")
    b.admin_adjust(shop.admin, shop.user["id"], shop.llm["id"], -1_000, "chargeback")
    assert env.wallet.balance(shop.user["id"], shop.llm["id"]) == 4_000
    with pytest.raises(AppError):
        b.admin_adjust(shop.admin, shop.user["id"], shop.llm["id"], -10_000, "too much")
    with pytest.raises(AppError):
        b.admin_grant(shop.admin, "ghost", shop.llm["id"], 5, "x")
    assert [e["action"] for e in env.store.admin_log if e["action"] in ("grant_tokens", "adjust_tokens")] == ["grant_tokens", "adjust_tokens"]
    assert env.store.admin_log[-1]["admin"] == "Boss"


# ------------------------------------------------------------------ dev + manual

async def test_dev_payment_disabled_unless_enabled_then_credits_once(shop, make_env):
    with pytest.raises(AppError) as e:
        await shop.env.billing.checkout(shop.user, shop.pack["id"], "dev")
    assert e.value.code == "METHOD_UNAVAILABLE"
    env = make_env(dev_payments=True)
    admin = env.agents.create_user(name="Boss", password="password123")
    llm = env.llms.add(name="C", provider="anthropic", model="m", api_key="k")
    pack = env.billing.add_pack(admin, name="P", llm_id=llm["id"], tokens=10_000, price_kes=100)
    u, _ = env.bidder()
    o = await env.billing.checkout(u, pack["id"], "dev")
    assert o["status"] == "PAID" and env.wallet.balance(u["id"], llm["id"]) == 10_000
    again = env.billing.mark_paid(o["id"], receipt=None, method="dev")   # replay
    assert again["already_paid"] and env.wallet.balance(u["id"], llm["id"]) == 10_000


async def test_manual_payment_review_flow(shop):
    env, b = shop.env, shop.env.billing
    o = await b.checkout(shop.user, shop.pack["id"], "manual")
    assert o["status"] == "PENDING" and o["reference"].startswith("KB-")
    for bad in ("", "abc", "has space!!", "TOOLONGTOOLONGTOOLONG"):
        with pytest.raises(AppError) as e:
            b.submit_receipt(shop.user, o["id"], bad)
        assert e.value.code == "INVALID_RECEIPT"
    r = b.submit_receipt(shop.user, o["id"], "sgh7 x2k9lp")
    assert r["status"] == "AWAITING_REVIEW" and r["receipt"] == "SGH7X2K9LP"
    assert env.wallet.balance(shop.user["id"], shop.llm["id"]) == 0          # not credited until an admin approves
    other, _ = env.bidder(name="Cheat")
    o2 = await b.checkout(other, shop.pack["id"], "manual")
    with pytest.raises(AppError) as e:                                        # the same M-Pesa code cannot buy twice
        b.submit_receipt(other, o2["id"], "SGH7X2K9LP")
    assert e.value.code == "DUPLICATE_RECEIPT"
    with pytest.raises(AppError):
        b.submit_receipt(other, o["id"], "ZZZZZZZZZZ")                        # not their order
    paid = b.admin_review(shop.admin, o["id"], True)
    assert paid["status"] == "PAID" and env.wallet.balance(shop.user["id"], shop.llm["id"]) == 100_000
    with pytest.raises(AppError):
        b.admin_review(shop.admin, o["id"], True)                             # cannot be approved twice
    assert env.wallet.verify_integrity() == []


async def test_manual_rejection_and_cancel_and_open_order_limit(shop):
    b = shop.env.billing
    o = await b.checkout(shop.user, shop.pack["id"], "manual")
    b.submit_receipt(shop.user, o["id"], "AAAAAAAA11")
    assert b.admin_review(shop.admin, o["id"], False, "no such payment")["status"] == "REJECTED"
    assert shop.env.wallet.balance(shop.user["id"], shop.llm["id"]) == 0
    with pytest.raises(AppError):
        b.mark_paid(o["id"], receipt=None, method="x")                        # a rejected order can't be paid later
    o2 = await b.checkout(shop.user, shop.pack["id"], "manual")
    assert b.cancel_order(shop.user, o2["id"])["status"] == "CANCELLED"
    with pytest.raises(AppError):
        b.cancel_order(shop.user, o2["id"])
    for _ in range(3):
        await b.checkout(shop.user, shop.pack["id"], "manual")
    with pytest.raises(AppError) as e:
        await b.checkout(shop.user, shop.pack["id"], "manual")
    assert e.value.code == "TOO_MANY_ORDERS"


def test_stk_orders_expire_quickly_but_manual_orders_wait_a_week(shop):
    env = shop.env
    stk = env.billing._insert_order(shop.user, shop.pack, "mpesa", "+254712000001")
    manual = env.billing._insert_order(shop.user, shop.pack, "manual", None)
    env.clock.advance(31 * 60_000)
    assert env.billing.expire_stale() == 1
    assert env.billing.get_order(stk["id"])["status"] == "EXPIRED" and env.billing.get_order(manual["id"])["status"] == "PENDING"
    env.clock.advance(7 * 24 * 3600_000)
    assert env.billing.expire_stale() == 1 and env.billing.get_order(manual["id"])["status"] == "EXPIRED"


async def test_slow_customer_who_paid_the_till_can_still_be_credited(shop):
    """Regression: a manual order expired after 30 min and then had no path to being credited."""
    env, b = shop.env, shop.env.billing
    o = await b.checkout(shop.user, shop.pack["id"], "manual")
    env.clock.advance(8 * 24 * 3600_000)
    b.expire_stale()
    assert b.get_order(o["id"])["status"] == "EXPIRED"
    b.submit_receipt(shop.user, o["id"], "LATEPAY001")
    assert b.admin_review(shop.admin, o["id"], True)["status"] == "PAID"
    assert env.wallet.balance(shop.user["id"], shop.llm["id"]) == 100_000


async def test_reconcile_is_not_starved_by_dead_expired_orders(mpesa_shop):
    """Regression: EXPIRED orders whose STK query said FAILED stayed EXPIRED and were re-queried forever, crowding out real ones."""
    s, b, env = mpesa_shop, mpesa_shop.env.billing, mpesa_shop.env
    dead = [b._insert_order(s.env.bidder(name=f"Abandoner{i}")[0], s.pack, "mpesa", f"+2547120000{i:02d}") for i in range(25)]
    for o in dead:
        b._set_status(o["id"], ("PENDING",), "EXPIRED", external_ref=f"ws_dead_{o['id'][:6]}")
    env.clock.advance(60_000)
    s.fake.query_result = {"code": "1032", "desc": "cancelled"}
    await b.reconcile(limit=20)
    await b.reconcile(limit=20)
    assert sum(b.get_order(o["id"])["status"] == "FAILED" for o in dead) == 25       # every dead order was retired
    real = await b.checkout(s.user, s.pack["id"], "mpesa", "0722000999")             # a real customer whose callback was lost
    env.clock.advance(60_000)
    s.fake.query_result = {"code": "0", "desc": "ok"}
    assert await b.reconcile() == 1 and b.get_order(real["id"])["status"] == "PAID"


async def test_non_ascii_callback_path_is_a_403_not_a_crash(mpesa_shop):
    with pytest.raises(AppError) as e:
        await mpesa_shop.env.billing.handle_mpesa_callback("s\u00e9cret\u2603", {})
    assert e.value.status == 403


def test_set_limits_is_atomic_and_logs_only_what_changed(shop):
    b, env = shop.env.billing, shop.env
    with pytest.raises(AppError):
        b.set_limits(shop.admin, signup_grants_per_day=5, max_llm_decisions_per_hour=-1)
    assert "signup_grants_per_day" not in env.store.settings                          # nothing half-applied
    n = len(env.store.admin_log)
    b.set_limits(shop.admin, max_llm_decisions_per_hour=30)
    assert env.store.settings["max_llm_decisions_per_hour"] == 30 and env.store.admin_log[-1]["detail"] == {"max_llm_decisions_per_hour": 30} and len(env.store.admin_log) == n + 1
    b.set_limits(shop.admin)
    assert len(env.store.admin_log) == n + 1                                          # a no-op is not logged


def test_one_free_trial_per_account_even_if_the_phone_changes(shop):
    """Regression: changing the phone number re-fired the grant under a new ref, and an admin grant bypassed the budget."""
    env = shop.env
    env.billing.set_signup_grant(shop.admin, shop.llm["id"], 5_000)
    u = env.agents.create_user(name="Phone Hopper", password="password123", phone="0701000001")
    for n in ("0701000002", "0701000003", "0701000004"):
        env.agents.set_phone(u["id"], n)
    assert env.wallet.balance(u["id"], shop.llm["id"]) == 5_000
    env.billing.set_limits(shop.admin, signup_grants_per_day=1)                       # budget is now exhausted…
    env.billing.admin_grant(shop.admin, shop.user["id"], shop.llm["id"], 10, "support gesture")  # …an admin grant must not exempt anyone
    other = env.agents.create_user(name="Latecomer", password="password123", phone="0701000009")
    assert env.wallet.balance(other["id"], shop.llm["id"]) == 0


async def test_refund_reclaims_only_unspent_tokens(shop):
    env, b = shop.env, shop.env.billing
    o = await b.checkout(shop.user, shop.pack["id"], "manual")
    b.submit_receipt(shop.user, o["id"], "REFUND0001")
    b.admin_review(shop.admin, o["id"], True)
    env.wallet.record_usage(shop.user["id"], shop.llm["id"], 30_000, agent_id=shop.agent["agent_id"], metered=True)
    r = b.admin_refund(shop.admin, o["id"], "customer request")
    assert r["status"] == "REFUNDED" and "reclaimed 70,000" in r["note"]
    assert env.wallet.balance(shop.user["id"], shop.llm["id"]) == 0 and env.wallet.verify_integrity() == []
    with pytest.raises(AppError):
        b.admin_refund(shop.admin, o["id"], "again")


# ------------------------------------------------------------------ M-Pesa (fake Safaricom)

class FakeSafaricom:
    """Just enough of Daraja to check what we send and how we react."""

    def __init__(self):
        self.requests, self.query_result, self.push_ok = [], {"pending": True}, True
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        self.requests.append((req.method, req.url.path, body, dict(req.headers)))
        if req.url.path == "/oauth/v1/generate":
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": "3599"})
        if req.url.path == "/mpesa/stkpush/v1/processrequest":
            if not self.push_ok:
                return httpx.Response(400, json={"errorCode": "400.002.02", "errorMessage": "Bad Request - Invalid PhoneNumber"})
            return httpx.Response(200, json={"MerchantRequestID": "m1", "CheckoutRequestID": "ws_CO_1", "ResponseCode": "0",
                                             "ResponseDescription": "Success", "CustomerMessage": "Success. Request accepted for processing"})
        if req.url.path == "/mpesa/stkpushquery/v1/query":
            q = self.query_result
            if q.get("pending"):
                return httpx.Response(500, json={"requestId": "r", "errorCode": "500.001.1001", "errorMessage": "The transaction is being processed"})
            return httpx.Response(200, json={"ResponseCode": "0", "ResultCode": q["code"], "ResultDesc": q["desc"], "CheckoutRequestID": "ws_CO_1"})
        return httpx.Response(404)


@pytest.fixture
def mpesa_shop(make_env):
    fake = FakeSafaricom()
    cfg = MpesaConfig("ck", "cs", "174379", "passkey", "https://kb.example.com", callback_secret="s3cret")
    env = make_env(mpesa_client=MpesaClient(cfg, http=httpx.AsyncClient(transport=fake.transport), clock=None))
    admin = env.agents.create_user(name="Boss", password="password123")
    llm = env.llms.add(name="Claude", provider="anthropic", model="m", api_key="k")
    pack = env.billing.add_pack(admin, name="Starter", llm_id=llm["id"], tokens=100_000, price_kes=500)
    u, a = env.bidder(name="Payer")
    return type("S", (), {"env": env, "fake": fake, "admin": admin, "llm": llm, "pack": pack, "user": u, "agent": a, "cfg": cfg})


def cb(checkout="ws_CO_1", code=0, receipt="RCP12345AB"):
    body = {"MerchantRequestID": "m1", "CheckoutRequestID": checkout, "ResultCode": code, "ResultDesc": "x"}
    if code == 0:
        body["CallbackMetadata"] = {"Item": [{"Name": "Amount", "Value": 500}, {"Name": "MpesaReceiptNumber", "Value": receipt}]}
    return {"Body": {"stkCallback": body}}


async def test_stk_push_request_shape_and_pending_order(mpesa_shop):
    s = mpesa_shop
    o = await s.env.billing.checkout(s.user, s.pack["id"], "mpesa", "0712 345 678")
    assert o["status"] == "PENDING" and o["external_ref"] == "ws_CO_1" and o["phone"] == "+254712345678"
    _, path, body, headers = next(r for r in s.fake.requests if "stkpush/v1/process" in r[1])
    assert body["PhoneNumber"] == body["PartyA"] == "254712345678" and body["Amount"] == 500 and body["BusinessShortCode"] == "174379"
    assert body["CallBackURL"] == "https://kb.example.com/webhooks/mpesa/s3cret" and body["TransactionType"] == "CustomerPayBillOnline"
    assert len(body["AccountReference"]) <= 12 and len(body["TransactionDesc"]) <= 13
    import base64
    assert base64.b64decode(body["Password"]).decode() == "174379passkey" + body["Timestamp"]
    assert headers["authorization"] == "Bearer tok-1"
    oauth = next(r for r in s.fake.requests if r[1] == "/oauth/v1/generate")
    assert oauth[3]["authorization"] == "Basic " + base64.b64encode(b"ck:cs").decode()


async def test_forged_callback_cannot_mint_tokens_only_safaricoms_answer_can(mpesa_shop):
    s, b = mpesa_shop, mpesa_shop.env.billing
    o = await b.checkout(s.user, s.pack["id"], "mpesa", "0712345678")
    with pytest.raises(AppError) as e:                                   # wrong path secret
        await b.handle_mpesa_callback("guess", cb())
    assert e.value.status == 403
    # attacker knows the secret and forges a "success" callback, but Safaricom says the customer has not paid
    s.fake.query_result = {"pending": True}
    assert (await b.handle_mpesa_callback("s3cret", cb()))["ResultCode"] == 0
    assert b.get_order(o["id"])["status"] == "PENDING" and s.env.wallet.balance(s.user["id"], s.llm["id"]) == 0
    # Safaricom says the payment failed (user cancelled): the order fails, nothing is credited
    s.fake.query_result = {"code": "1032", "desc": "Request cancelled by user"}
    await b.handle_mpesa_callback("s3cret", cb(code=1032))
    assert b.get_order(o["id"])["status"] == "FAILED" and s.env.wallet.balance(s.user["id"], s.llm["id"]) == 0
    # junk payloads and unknown checkouts are acknowledged and ignored
    assert (await b.handle_mpesa_callback("s3cret", {"nonsense": 1}))["ResultCode"] == 0
    assert (await b.handle_mpesa_callback("s3cret", cb(checkout="ws_unknown")))["ResultCode"] == 0


async def test_confirmed_payment_credits_once_even_if_callback_repeats(mpesa_shop):
    s, b = mpesa_shop, mpesa_shop.env.billing
    o = await b.checkout(s.user, s.pack["id"], "mpesa", "0712345678")
    s.fake.query_result = {"code": "0", "desc": "The service request is processed successfully."}
    for _ in range(3):
        await b.handle_mpesa_callback("s3cret", cb())
    paid = b.get_order(o["id"])
    assert paid["status"] == "PAID" and paid["receipt"] == "RCP12345AB"
    assert s.env.wallet.balance(s.user["id"], s.llm["id"]) == 100_000       # exactly once
    assert len(s.env.wallet.ledger(user_id=s.user["id"], kind="TOPUP")) == 1 and s.env.wallet.verify_integrity() == []


async def test_lost_callback_is_recovered_by_reconcile_and_user_check(mpesa_shop):
    s, b, env = mpesa_shop, mpesa_shop.env.billing, mpesa_shop.env
    o = await b.checkout(s.user, s.pack["id"], "mpesa", "0712345678")
    assert await b.reconcile() == 0                                        # too fresh: gives the callback a chance first
    env.clock.advance(60_000)
    s.fake.query_result = {"pending": True}
    assert await b.reconcile() == 0 and b.get_order(o["id"])["status"] == "PENDING"
    s.fake.query_result = {"code": "0", "desc": "ok"}
    assert await b.reconcile() == 1
    assert env.wallet.balance(s.user["id"], s.llm["id"]) == 100_000
    o2 = await b.checkout(s.user, s.pack["id"], "mpesa", "0722111222")
    assert (await b.check_order(s.user, o2["id"]))["status"] == "PAID"     # the user's own "check payment" button
    other, _ = env.bidder(name="Nosy")
    with pytest.raises(AppError):
        await b.check_order(other, o2["id"])


async def test_late_stk_approval_after_expiry_still_credits(mpesa_shop):
    s, b, env = mpesa_shop, mpesa_shop.env.billing, mpesa_shop.env
    o = await b.checkout(s.user, s.pack["id"], "mpesa", "0712345678")
    env.clock.advance(31 * 60_000)
    b.expire_stale()
    assert b.get_order(o["id"])["status"] == "EXPIRED"
    s.fake.query_result = {"code": "0", "desc": "ok"}
    await b.handle_mpesa_callback("s3cret", cb())                          # customer approved late: money was taken
    assert b.get_order(o["id"])["status"] == "PAID" and env.wallet.balance(s.user["id"], s.llm["id"]) == 100_000


async def test_mpesa_failures_and_abuse_limits(mpesa_shop):
    s, b = mpesa_shop, mpesa_shop.env.billing
    with pytest.raises(AppError) as e:
        await b.checkout(s.user, s.pack["id"], "mpesa", "+447911123456")   # not a Safaricom number
    assert e.value.code == "INVALID_PHONE"
    s.fake.push_ok = False
    with pytest.raises(AppError) as e:
        await b.checkout(s.user, s.pack["id"], "mpesa", "0712345678")
    assert e.value.code == "MPESA_REJECTED" and b.list_orders(user_id=s.user["id"])[0]["status"] == "FAILED"
    s.fake.push_ok = True
    victim = "0799000111"                                                  # nobody can be spammed with payment prompts
    users = [s.env.bidder(name=f"Spammer{i}")[0] for i in range(5)]
    for u in users[:3]:
        await b.checkout(u, s.pack["id"], "mpesa", victim)
    with pytest.raises(AppError) as e:
        await b.checkout(users[3], s.pack["id"], "mpesa", victim)
    assert e.value.code == "PHONE_RATE_LIMIT"


async def test_callback_disabled_without_mpesa(shop):
    with pytest.raises(AppError) as e:
        await shop.env.billing.handle_mpesa_callback("x", {})
    assert e.value.code == "MPESA_DISABLED"


async def test_mpesa_bad_credentials_are_reported_clearly(make_env):
    def deny(req):
        return httpx.Response(400, json={"errorMessage": "Invalid credentials"})
    cfg = MpesaConfig("ck", "cs", "174379", "pk", "https://kb.example.com")
    c = MpesaClient(cfg, http=httpx.AsyncClient(transport=httpx.MockTransport(deny)))
    with pytest.raises(AppError) as e:
        await c.token()
    assert e.value.code == "MPESA_AUTH"


def test_mpesa_config_from_env(monkeypatch):
    assert MpesaConfig.from_env() is None
    for k, v in {"MPESA_CONSUMER_KEY": "a", "MPESA_CONSUMER_SECRET": "b", "MPESA_SHORTCODE": "1", "MPESA_PASSKEY": "p",
                 "MPESA_CALLBACK_BASE_URL": "https://x.example/"}.items():
        monkeypatch.setenv(k, v)
    c = MpesaConfig.from_env(fallback_secret="persisted")
    assert c.callback_base_url == "https://x.example" and c.callback_secret == "persisted" and c.env == "sandbox"
    monkeypatch.setenv("MPESA_ENV", "moon")
    with pytest.raises(ValueError):
        MpesaConfig.from_env()


# ------------------------------------------------------------------ reports & export

async def test_revenue_margin_report_and_csv_hardening(shop):
    env, b = shop.env, shop.env.billing
    o = await b.checkout(shop.user, shop.pack["id"], "manual")
    b.submit_receipt(shop.user, o["id"], "REPORT0001")
    b.admin_review(shop.admin, o["id"], True)
    env.wallet.record_usage(shop.user["id"], shop.llm["id"], 40_000, agent_id=shop.agent["agent_id"], metered=True)
    r = b.revenue_report()
    row = r["by_llm"][0]
    assert r["revenue_kes"] == 500 and row["tokens_sold"] == 100_000 and row["tokens_used"] == 40_000 and row["tokens_outstanding"] == 60_000
    assert row["est_cost_kes"] == 20.0 and r["margin_kes"] == 480.0 and r["margin_pct"] == 96.0
    with pytest.raises(AppError):  # names cannot even contain formula characters
        env.agents.create_user(name='=HYPERLINK("http://evil")', password="password123")
    env.billing.admin_grant(shop.admin, shop.user["id"], shop.llm["id"], 1_000, "=cmd|' /C calc'!A0")
    hostile = b.add_pack(shop.admin, name="+SUM(A1)", llm_id=shop.llm["id"], tokens=5_000, price_kes=50)
    await b.checkout(shop.user, hostile["id"], "manual")
    csv_text = b.ledger_csv()
    assert "'=cmd" in csv_text and ",=cmd" not in csv_text
    assert "'+SUM(A1)" in b.orders_csv() and ",+SUM(A1)" not in b.orders_csv()
    assert "REPORT0001" in b.orders_csv() and len(b.orders_csv(user_id=shop.user["id"]).splitlines()) == 3


async def test_a_timeout_after_the_push_leaves_the_order_recoverable_by_receipt(make_env):
    """If the request left but the answer never came, Safaricom may have prompted the customer: the order must NOT be failed
    (a payment nobody can match), it stays pending so the customer can finish it with the M-Pesa code."""
    def handler(req):
        if "oauth" in str(req.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": "3599"})
        raise httpx.ReadTimeout("slow")
    cfg = MpesaConfig("ck", "cs", "174379", "passkey", "https://kb.example.com", callback_secret="s")
    env = make_env(mpesa_client=MpesaClient(cfg, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), clock=None))
    admin = env.agents.create_user(name="Boss", password="password123")
    llm = env.llms.add(name="Claude", provider="anthropic", model="m", api_key="k")
    pack = env.billing.add_pack(admin, name="Starter", llm_id=llm["id"], tokens=100_000, price_kes=500)
    u, _ = env.bidder(name="Payer")
    with pytest.raises(AppError) as e:
        await env.billing.checkout(u, pack["id"], "mpesa", "0712 345 678")
    assert e.value.code == "MPESA_UNCERTAIN"
    o = env.billing.list_orders(user_id=u["id"])[0]
    assert o["status"] == "PENDING" and not o["external_ref"]
    env.billing.submit_receipt(u, o["id"], "SGH7X2K9LP")  # the customer did get the prompt and pay: they enter the code
    assert env.billing.get_order(o["id"])["status"] == "AWAITING_REVIEW"
    env.billing.admin_review(admin, o["id"], True)
    assert env.wallet.balance(u["id"], llm["id"]) == 100_000
