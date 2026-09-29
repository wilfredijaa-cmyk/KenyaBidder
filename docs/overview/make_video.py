"""Records the overview video (and screenshots) by driving the real app with Playwright.

    python docs/overview/make_video.py          # writes docs/overview/overview.webm (+ overview.mp4 when an H.264 ffmpeg exists) and screens/*.png
"""
import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
SHOTS = HERE / "screens"
CHROMIUM = next((p for p in ["/opt/pw-browsers/chromium-1194/chrome-linux/chrome", shutil.which("chromium"), shutil.which("google-chrome")] if p and Path(p).exists()), None)
W, H = 1280, 760


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


CAPS = []  # (seconds since the recording started, text) -> captions.json, used to time the narration
T0 = [0.0]


async def caption(page, text, ms=None):
    ms = max(ms or 0, int(len(text.split()) * 420 + 900))  # long enough to be read AND spoken
    if T0[0]:
        CAPS.append((round(time.time() - T0[0], 2), text))
    await page.evaluate("""t => { let d = document.getElementById('kb-cap'); if (!d) { d = document.createElement('div'); d.id = 'kb-cap';
        d.style.cssText = 'position:fixed;left:50%;bottom:22px;transform:translateX(-50%);max-width:80%;background:rgba(15,23,42,.92);color:#fff;padding:12px 22px;border-radius:12px;font:500 20px system-ui;z-index:99999;text-align:center;box-shadow:0 6px 24px rgba(0,0,0,.35)';
        document.body.appendChild(d); } d.textContent = t; }""", text)
    await page.wait_for_timeout(ms)


async def shot(page, name):
    await page.screenshot(path=str(SHOTS / f"{name}.png"))


async def goto(page, base, path):
    await page.goto(base + path)
    await page.wait_for_load_state("networkidle")
    try:
        await page.wait_for_function("() => window.socket && window.socket.connected", timeout=5000)
    except Exception:  # noqa: BLE001
        await page.wait_for_timeout(1000)


async def signup(page, base, name, phone):
    await goto(page, base, "/login")
    await page.get_by_role("tab", name="Create account").click()
    await page.get_by_label("Name").last.fill(name)
    await page.get_by_label("Password (8+ characters)").fill("password123")
    await page.get_by_label("Phone").fill(phone)
    await page.get_by_text("I accept the").click()
    await page.get_by_role("button", name="Create account").click()
    await page.wait_for_url("**/agents")


async def main():
    port = free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    env["KENYABIDDER_DEV_PAYMENTS"] = "1"
    proc = subprocess.Popen([sys.executable, "-m", "kenyabidder", "--port", str(port), "--data", ""], env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.25)
    try:
        async with async_playwright() as p:
            b = await p.chromium.launch(executable_path=CHROMIUM)

            async def ctx(record=False):
                kw = dict(viewport={"width": W, "height": H})
                if record:
                    kw.update(record_video_dir=str(HERE / "_rec"), record_video_size={"width": W, "height": H})
                c = await b.new_context(**kw)
                pg = await c.new_page()
                pg.set_default_timeout(20000)
                return c, pg

            # unseen contexts: the administrator and the seller
            _, admin = await ctx()
            await signup(admin, base, "Root Admin", "+254700000001")
            _, seller = await ctx()
            await signup(seller, base, "Amina Traders", "+254700000002")
            await seller.get_by_role("button", name="New seller agent").click()
            await seller.wait_for_url("**/agent/*")
            await seller.get_by_label("Price floor (KES) — never list or quote below this").fill("500")
            await seller.get_by_role("button", name="Save rules").click()

            # ---------------- the recorded story: the buyer's point of view ----------------
            rc, buyer = await ctx(record=True)
            T0[0] = time.time()
            await goto(buyer, base, "/login")
            await caption(buyer, "KenyaBidder — AI agents that buy and sell for you", 2500)
            await shot(buyer, "01-login")
            await buyer.get_by_role("button", name="SW", exact=True).click()
            await buyer.wait_for_selector("text=Ingia")
            await caption(buyer, "Kiswahili and English on every screen", 2000)
            await buyer.get_by_role("button", name="EN", exact=True).click()
            await buyer.wait_for_selector("text=Sign in")
            await buyer.get_by_role("tab", name="Create account").click()
            await buyer.get_by_label("Name").last.fill("David Buyer")
            await buyer.get_by_label("Password (8+ characters)").fill("password123")
            await buyer.get_by_label("Phone").fill("0712 000 003")
            await buyer.get_by_text("I accept the").click()
            await caption(buyer, "Sign up with consent to the Terms & Privacy Notice", 1800)
            await buyer.get_by_role("button", name="Create account").click()
            await buyer.wait_for_url("**/agents")
            await buyer.get_by_role("button", name="New buyer agent").click()
            await buyer.wait_for_url("**/agent/*")
            await caption(buyer, "Each user gets a persistent agent with hard limits the AI cannot override", 2600)
            await buyer.get_by_label("Budget ceiling per bid (KES)").fill("5000")
            await buyer.get_by_label("Category").fill("electronics")
            await shot(buyer, "02-agent-rules")
            await buyer.get_by_role("button", name="Save rules").click()
            await buyer.wait_for_timeout(800)
            await buyer.get_by_role("tab", name="Brain, tools & knowledge").click()
            await caption(buyer, "Pick the LLM, the algorithm per auction type, the MCP tools and knowledge bases", 3000)
            await shot(buyer, "03-agent-brain")

            # seller lists a phone lot
            await goto(seller, base, "/")
            await seller.get_by_label("Product").fill("Samsung A15 phones x10")
            await seller.get_by_label("Category").fill("electronics")
            await seller.get_by_label("Reserve (hidden from bidders)").fill("1000")
            await seller.get_by_label("Start price").fill("1000")
            await seller.get_by_label("Min increment").fill("100")
            await seller.get_by_label("Duration (minutes)").fill("1")
            await seller.get_by_role("button", name="Create listing").click()

            await goto(buyer, base, "/")
            await caption(buyer, "A seller lists stock; your agent sees it and decides — rules first, LLM only proposes", 2800)
            await buyer.locator(".q-card", has_text="Samsung A15").first.click()
            await buyer.get_by_role("button", name="Let my agent bid for me").click()
            await caption(buyer, "The agent's plan runs in a deterministic engine — no LLM in the bid path", 3000)
            await shot(buyer, "04-auction")
            await buyer.get_by_label("Manual bid (KES)").fill("999999")
            await buyer.get_by_role("button", name="Bid manually").click()
            await caption(buyer, "Even manual bids pass the guardrail: over the ceiling is refused", 2600)
            await buyer.wait_for_timeout(45000)
            await goto(buyer, base, "/matches")
            await buyer.wait_for_selector("text=Confirm match", timeout=30000)
            await caption(buyer, "Auction closes → a match. No money moves on the platform", 2600)
            await buyer.get_by_role("button", name="Confirm match").click()
            await goto(seller, base, "/matches")
            await seller.get_by_role("button", name="Confirm match").click()
            await goto(buyer, base, "/matches")
            await caption(buyer, "Both confirm → contact details are revealed; payment and delivery happen directly", 3000)
            await shot(buyer, "05-match")

            # RFQ
            await goto(buyer, base, "/")
            await buyer.get_by_text("Request quotes (RFQ)").click()
            await buyer.get_by_label("What do you need?").fill("50 phone chargers")
            await buyer.get_by_label("Category").fill("electronics")
            await buyer.get_by_label("Maximum total price (KES)").fill("4500")
            await buyer.get_by_role("button", name="Post request").click()
            await caption(buyer, "Reverse auctions: post an RFQ and suppliers' agents bid the price down", 3000)
            await goto(seller, base, "/")
            await seller.locator(".q-card", has_text="phone chargers").first.click()
            await seller.get_by_label("Manual quote (KES)").fill("4000")
            await seller.get_by_role("button", name="Quote manually").click()
            await goto(buyer, base, "/")
            await buyer.locator(".q-card", has_text="phone chargers").first.click()
            await buyer.wait_for_selector("text=Quotes (1)")
            await shot(buyer, "06-rfq")
            await caption(buyer, "The lowest valid quote wins the introduction", 2200)

            # wallet + profile
            await goto(buyer, base, "/wallet")
            await caption(buyer, "Agents that use LLMs run on tokens you buy with M-Pesa; deterministic agents are free", 3000)
            await shot(buyer, "07-wallet")
            await goto(buyer, base, "/profile")
            await caption(buyer, "Verify your phone, apply for a verified-business badge, export or delete your data", 3200)
            await shot(buyer, "08-profile")
            await buyer.get_by_role("button", name="SW", exact=True).click()
            await buyer.wait_for_selector("text=Wasifu wangu")
            await caption(buyer, "…in Kiswahili too", 1800)
            await buyer.get_by_role("button", name="EN", exact=True).click()

            # admin tour (same recording: sign the buyer context out, sign in as admin)
            await buyer.context.clear_cookies()
            await goto(buyer, base, "/login")
            await buyer.get_by_label("Name").last.fill("Root Admin")
            await buyer.get_by_label("Password").last.fill("password123")
            await buyer.get_by_role("button", name="Sign in").click()
            await buyer.wait_for_url(lambda u: "/login" not in u)
            await goto(buyer, base, "/admin")
            await caption(buyer, "Admins register LLMs, MCP servers and knowledge bases, and sell token packs and plans", 3000)
            await shot(buyer, "09-admin-llms")
            for tab, text, name in (("Trust & disputes", "Verification policy", "10-admin-trust"), ("Messaging", "SMS gateway", "11-admin-messaging"),
                                    ("Health & backups", "Health", "12-admin-health")):
                await buyer.get_by_role("tab", name=tab).click()
                await buyer.wait_for_selector(f"text={text}")
                await caption(buyer, {"Trust & disputes": "Verify businesses and rule on disputes", "Messaging": "SMS/email gateways for codes and alerts",
                                      "Health & backups": "Health checks, metrics and verified backups"}[tab], 2600)
                await shot(buyer, name)
            await caption(buyer, "KenyaBidder — python -m kenyabidder", 2500)
            path = await buyer.video.path()
            await rc.close()
            await b.close()
            shutil.move(path, HERE / "overview.webm")
            import json
            (HERE / "captions.json").write_text(json.dumps(CAPS, indent=1))
        shutil.rmtree(HERE / "_rec", ignore_errors=True)
    finally:
        proc.terminate()


asyncio.run(main())
