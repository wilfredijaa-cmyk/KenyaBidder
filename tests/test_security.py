"""Hardening: passwords, 2FA, shill bids, throttles, client-IP trust, headers, body limits, redaction, secrets."""
import asyncio
import logging

import pytest

from kenyabidder import security, totp
from kenyabidder.errors import AppError
from kenyabidder.security import ClientIP, HardeningMiddleware, Throttle, check_password, redact


# ------------------------------------------------------------------ passwords

@pytest.mark.parametrize("pw", ["short", "password123", "Password123", "qwertyuiop", "aaaaaaaaaaaa", "1234567890", "wanjiru-pass-1", "abcdefghijk"])
def test_weak_passwords_are_refused_under_the_strong_policy(pw):
    with pytest.raises(AppError) as e:
        check_password(pw, name="Wanjiru", phone="+254711000001", policy="strong")
    assert e.value.code == "WEAK_PASSWORD"


@pytest.mark.parametrize("pw", ["correct horse battery", "t8!Lx-9vQ2mZ", "mombasa-sunset-42"])
def test_strong_passwords_pass(pw):
    check_password(pw, name="Wanjiru", policy="strong")


def test_policy_is_enforced_on_signup_change_and_reset(env):
    env.store.settings["password_policy"] = "strong"
    with pytest.raises(AppError) as e:
        env.agents.create_user(name="Weak One", password="password123")
    assert e.value.code == "WEAK_PASSWORD"
    u = env.agents.create_user(name="Strong One", password="correct horse battery")
    with pytest.raises(AppError):
        env.agents.change_password(u["id"], "correct horse battery", "Password123")
    with pytest.raises(AppError):
        env.agents.admin_reset_password(u["id"], "12345678")
    env.agents.change_password(u["id"], "correct horse battery", "another long passphrase")


# ------------------------------------------------------------------ two-factor

def test_totp_rfc6238_vector_and_replay_protection():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238 appendix B: ASCII "12345678901234567890"
    assert totp._code(secret, 1) == "287082"
    c = totp.verify(secret, "287082", 59)
    assert c == 1
    assert totp.verify(secret, "287082", 59, last_counter=c) is None  # the same code cannot be used twice
    assert totp.verify(secret, "287082", 59 + 300) is None  # and expires
    assert totp.verify(secret, "12345", 59) is None and totp.verify(secret, "abcdef", 59) is None


def test_two_factor_enrolment_login_step_recovery_and_lockout(env):
    u = env.agents.create_user(name="Admin Boss", password="password123")
    seed = env.agents.totp_begin(u["id"])
    assert seed["uri"].startswith("otpauth://totp/KenyaBidder")
    with pytest.raises(AppError):
        env.agents.totp_confirm(u["id"], "000000")
    t = env.clock.now() / 1000
    codes = env.agents.totp_confirm(u["id"], totp._code(seed["secret"], int(t // 30)))
    assert env.agents.totp_enabled(u) and len(codes) == 8
    env.clock.advance(31_000)
    good = totp._code(seed["secret"], int(env.clock.now() / 1000 // 30))
    assert env.agents.totp_check(u, good) and not env.agents.totp_check(u, good)  # single use
    assert env.agents.totp_check(u, codes[0]) and not env.agents.totp_check(u, codes[0])  # recovery codes too
    with pytest.raises(AppError) as e:  # (the two replays above already counted as failures)
        for _ in range(10):
            assert not env.agents.totp_check(u, "000000")
    assert e.value.code == "TOO_MANY_ATTEMPTS"  # brute-forcing the second factor locks it
    env.clock.advance(6 * 60_000)
    assert env.agents.totp_check(u, codes[1])  # the lockout lifts, and recovery codes still work


def test_admins_are_locked_out_of_the_console_until_two_factor_is_on_when_required(env):
    admin = env.agents.create_user(name="Root", password="password123")
    assert admin["role"] == "admin" and env.agents.admin_2fa_ok(admin)  # optional by default in dev
    env.store.settings["require_admin_2fa"] = True
    assert not env.agents.admin_2fa_ok(admin)
    seed = env.agents.totp_begin(admin["id"])
    env.agents.totp_confirm(admin["id"], totp._code(seed["secret"], int(env.clock.now() / 1000 // 30)))
    assert env.agents.admin_2fa_ok(admin)
    with pytest.raises(AppError) as e:
        env.agents.totp_disable(admin["id"], "password123", "000000")
    assert e.value.code == "2FA_REQUIRED"


# ------------------------------------------------------------------ shill bidding

def test_accounts_sharing_a_phone_or_email_cannot_bid_on_each_others_listings(env):
    us, seller = env.seller()  # phone +254700000001, email amina@example.com
    ub, buyer = env.bidder(phone="+254700000001")  # same phone, different account
    aid = env.english(seller["agent_id"])["auction_id"]
    r = env.engine.submit_bid(auction_id=aid, agent_id=buyer["agent_id"], amount=1500)
    assert r["code"] == "SELF_BID"
    ub2 = env.agents.create_user(name="Sock Puppet", password="password123", phone="+254799123456", email=" AMINA@example.com ")
    sock = env.agents.create_agent(user_id=ub2["id"], type="BIDDER", constraints={"budget_ceiling": 5000})
    assert env.engine.submit_bid(auction_id=aid, agent_id=sock["agent_id"], amount=1500)["code"] == "SELF_BID"
    _, honest = env.bidder(phone="+254711555555")
    assert env.engine.submit_bid(auction_id=aid, agent_id=honest["agent_id"], amount=1500)["ok"]


# ------------------------------------------------------------------ throttles & client IP

def test_throttle_window_and_bounded_memory():
    from kenyabidder.clock import FakeClock
    c = FakeClock()
    t = Throttle(c)
    for _ in range(3):
        t.hit("x", "1.1.1.1", 3, 60)
    with pytest.raises(AppError) as e:
        t.hit("x", "1.1.1.1", 3, 60)
    assert e.value.status == 429
    t.hit("x", "2.2.2.2", 3, 60)  # other keys unaffected
    c.advance(61_000)
    t.hit("x", "1.1.1.1", 3, 60)  # window slid
    for i in range(300):  # an attacker rotating keys cannot grow the table forever
        t.record("junk", f"k{i}")
    c.advance(4_000_000)
    t.hit("x", "3.3.3.3", 3, 60)
    assert len(t._hits) < 10


def test_x_forwarded_for_is_trusted_only_from_configured_proxies():
    none = ClientIP("")
    assert none.resolve("9.9.9.9", "1.2.3.4") == "9.9.9.9"  # a forged header from a stranger is ignored
    p = ClientIP("10.0.0.0/8, 127.0.0.1")
    assert p.resolve("10.1.1.1", "203.0.113.5, 10.2.2.2") == "203.0.113.5"
    assert p.resolve("10.1.1.1", "6.6.6.6, 203.0.113.5") == "203.0.113.5"  # the client cannot prepend a fake hop
    assert p.resolve("8.8.8.8", "1.2.3.4") == "8.8.8.8"  # peer is not a proxy
    assert p.resolve("10.1.1.1", "not-an-ip") == "10.1.1.1"
    with pytest.raises(ValueError):
        ClientIP("nonsense")


# ------------------------------------------------------------------ ASGI hardening

async def call(mw, method="GET", path="/", headers=(), body=b"", client=("1.2.3.4", 1)):
    sent = []
    msgs = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return msgs.pop(0) if msgs else {"type": "http.disconnect"}

    async def send(m):
        sent.append(m)
    await mw({"type": "http", "method": method, "path": path, "headers": list(headers), "client": client}, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], dict(start["headers"]), b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def make_app(**kw):
    async def app(scope, receive, send):
        if scope["type"] == "http":
            while (await receive()).get("more_body"):
                pass
            await send({"type": "http.response.start", "status": 200, "headers": [(b"server", b"uvicorn"), (b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"ok"})
    return HardeningMiddleware(app, **kw)


async def test_security_headers_are_on_every_response_and_the_server_banner_is_gone():
    st, h, _ = await call(make_app(https=True))
    assert st == 200 and b"server" not in h
    assert h[b"x-content-type-options"] == b"nosniff" and h[b"x-frame-options"] == b"DENY" and b"frame-ancestors 'none'" in h[b"content-security-policy"]
    assert h[b"strict-transport-security"].startswith(b"max-age=") and h[b"referrer-policy"]
    st, h, _ = await call(make_app(https=False))
    assert b"strict-transport-security" not in h  # HSTS only when the site really is HTTPS


async def test_per_ip_rate_limit_and_body_caps():
    app = make_app(http_per_minute=3)
    for _ in range(3):
        assert (await call(app))[0] == 200
    st, h, _ = await call(app)
    assert st == 429 and h[b"retry-after"] and h[b"x-frame-options"]  # error replies carry the headers too
    assert (await call(app, client=("5.5.5.5", 1)))[0] == 200  # per IP
    app = make_app()
    assert (await call(app, "POST", "/webhooks/whatsapp", [(b"content-length", b"300000")], b""))[0] == 413
    assert (await call(app, "POST", "/x", [(b"content-length", b"2000000")], b""))[0] == 413
    assert (await call(app, "POST", "/x", [(b"content-length", b"abc")]))[0] == 400
    st, _, _ = await call(app, "POST", "/x", [], b"a" * 1_500_000)  # a lying/missing content-length cannot smuggle a big body
    assert st == 413


async def test_websocket_connections_per_ip_are_capped():
    closed, gate = [], asyncio.Event()

    async def app(scope, receive, send):
        await gate.wait()
    mw = HardeningMiddleware(app, ws_per_ip=2)

    async def send(m):
        closed.append(m)
    scope = {"type": "websocket", "client": ("7.7.7.7", 1), "headers": []}
    t1, t2 = asyncio.create_task(mw(scope, None, send)), asyncio.create_task(mw(scope, None, send))
    await asyncio.sleep(0.01)
    await mw(scope, None, send)  # third connection from the same IP
    assert closed and closed[0]["type"] == "websocket.close"
    gate.set()
    await asyncio.gather(t1, t2)


# ------------------------------------------------------------------ redaction

def test_secrets_and_phone_numbers_are_redacted_from_logs():
    assert "s3cretvalue" not in redact("POST /webhooks/mpesa/s3cretvalue-abc HTTP/1.1")
    assert "sk-ant" not in redact("calling with key sk-ant-api03-abcdefghijklmnop1234")
    assert "hunter22xx" not in redact('login password=hunter22xx failed')
    assert "Bearer abcdefghijkl" not in redact("Authorization: Bearer abcdefghijklmnop")
    assert "711000001" not in redact("sent code to +254711000001")
    rec = logging.LogRecord("x", 20, "f", 1, "token=%s", ("topsecretvalue123",), None)
    assert security.RedactingFilter().filter(rec) and "topsecretvalue123" not in rec.getMessage()


# ------------------------------------------------------------------ startup configuration

def test_production_refuses_insecure_configuration_and_dev_only_warns():
    from kenyabidder import config
    env = {"KENYABIDDER_ENV": "production", "KENYABIDDER_DEV_PAYMENTS": "1", "KENYABIDDER_INSECURE_WEBHOOK": "1", "KENYABIDDER_PASSWORD_POLICY": "basic"}
    r = config.validate(env, durable=False)
    assert not r.ok and len(r.errors) >= 5 and any("PUBLIC_URL" in e for e in r.errors)
    dev = config.validate({"KENYABIDDER_DEV_PAYMENTS": "1"}, durable=True)
    assert dev.ok and any("DEV_PAYMENTS" in w for w in dev.warnings)
    good = config.validate({"KENYABIDDER_ENV": "production", "KENYABIDDER_PUBLIC_URL": "https://kb.example.co.ke", "KENYABIDDER_SECRET": "x" * 32,
                            "KENYABIDDER_ENCRYPTION_KEY": "k", "KENYABIDDER_METRICS_TOKEN": "t", "KENYABIDDER_TRUSTED_PROXIES": "10.0.0.0/8"}, host="0.0.0.0")
    assert good.ok and not good.warnings
    partial = config.validate({"MPESA_CONSUMER_KEY": "a"})
    assert any("partially configured" in w for w in partial.warnings)


def test_admin_configured_urls_cannot_target_cloud_metadata():
    from kenyabidder.security import check_outbound_url
    for bad_url in ("http://169.254.169.254/latest/meta-data", "http://metadata.google.internal/computeMetadata/v1", "http://[fd00:ec2::254]/", "ftp://x.example/",
                    "http://user:pw@example.com/", "http://0.0.0.0:8000"):
        with pytest.raises(AppError):
            check_outbound_url(bad_url)
    check_outbound_url("http://127.0.0.1:11434/v1")  # a local Ollama is legitimate
    check_outbound_url("https://api.openai.com/v1")


def test_llm_and_mcp_registries_apply_the_url_guard(env):
    with pytest.raises(AppError):
        env.llms.add(name="Evil", provider="openai_compatible", model="m", base_url="http://169.254.169.254/v1", api_key="k")
    with pytest.raises(AppError):
        env.mcps.add(name="Evil", transport="http", url="http://169.254.169.254/mcp")


async def test_emergency_stop_halts_listing_bidding_and_agents_and_resume_restores(env):
    _, seller = env.seller()
    ub, buyer = env.bidder(memory={"watch": {"category": "electronics"}})
    aid = env.english(seller["agent_id"])["auction_id"]
    env.store.settings["platform_paused"] = True
    with pytest.raises(AppError) as e:
        env.english(seller["agent_id"])
    assert e.value.code == "PLATFORM_PAUSED"
    with pytest.raises(AppError):
        env.engine.create_rfq(buyer_agent_id=buyer["agent_id"], product_spec={"category": "x", "title": "y", "quantity": 1}, auction_type="REVERSE_ENGLISH", duration_ms=1000, max_price=10)
    assert env.engine.submit_bid(auction_id=aid, agent_id=buyer["agent_id"], amount=1500)["code"] == "PLATFORM_PAUSED"
    assert (await env.orchestrator.consider(buyer["agent_id"], aid, manual=True))["status"] == "FILTERED"
    env.store.settings["platform_paused"] = False
    assert env.engine.submit_bid(auction_id=aid, agent_id=buyer["agent_id"], amount=1500)["ok"]


def test_pause_all_cancels_live_plans_of_every_agent_of_the_user(env):
    _, seller = env.seller()
    ub, b1 = env.bidder()
    b2 = env.agents.create_agent(user_id=ub["id"], type="BIDDER", constraints={"budget_ceiling": 5000})
    aid = env.english(seller["agent_id"], duration_ms=600_000)["auction_id"]
    for a in (b1, b2):
        env.execution.register_trigger(agent_id=a["agent_id"], auction_id=aid, kind="ENGLISH_INCREMENTAL", params={"max_bid": 3000})
    assert env.agents.pause_all(ub["id"]) == 2
    assert all(a["status"] == "PAUSED" for a in (b1, b2)) and not [t for t in env.store.triggers.values() if t["status"] == "ACTIVE"]
