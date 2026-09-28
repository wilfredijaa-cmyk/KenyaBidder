"""Browser end-to-end tests. Skipped automatically when Playwright/Chromium are unavailable.

Starts the real app (``python -m kenyabidder``) in a subprocess plus a mock OpenAI-compatible LLM server,
then drives the NiceGUI console with Playwright exactly as a user would.
"""
import itertools
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

pw = pytest.importorskip("playwright.async_api")

CHROMIUM = os.environ.get("CHROMIUM_PATH") or next(
    (p for p in ["/opt/pw-browsers/chromium-1194/chrome-linux/chrome", shutil.which("chromium"), shutil.which("chromium-browser"), shutil.which("google-chrome")] if p and Path(p).exists()), None)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def mock_llm():
    """OpenAI-compatible server that researches with the first offered tool, then calls propose_action."""
    import uvicorn
    from fastapi import FastAPI, Request

    app, calls = FastAPI(), []

    def tool_call(name, args):
        import json
        return {"choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
            {"id": f"call_{len(calls)}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    @app.post("/v1/chat/completions")
    async def chat(req: Request):
        body = await req.json()
        calls.append(body)
        tools = [t["function"]["name"] for t in body.get("tools", [])]
        if not tools:
            return {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}], "usage": {}}
        researched = any(m["role"] == "tool" for m in body["messages"])
        research = [t for t in tools if t not in ("propose_action", "propose_listing", "search_knowledge")]
        if research and not researched:
            return tool_call(research[0], {"category": "electronics"})
        if "propose_listing" in tools:
            return tool_call("propose_listing", {"auction_type": "ENGLISH", "reserve_price": 1500, "start_price": 1200, "reasoning": "mock advisor: english at 1500"})
        return tool_call("propose_action", {"action": "BID", "max_bid": 3000, "increment_pct": 0, "snipe_window_ms": 0, "reasoning": "mock LLM: researched the market, bid up to 3000"})

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    yield {"url": f"http://127.0.0.1:{port}/v1", "calls": calls}
    server.should_exit = True


@pytest.fixture(scope="session")
def base_url():
    port = free_port()
    # NiceGUI switches into a test mode when it sees PYTEST_* variables — the app under test must not inherit them
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST") and k != "KENYABIDDER_DATA"}
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen([sys.executable, "-m", "kenyabidder", "--port", str(port), "--data", ""], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=Path(__file__).parents[2])
    url = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            if httpx.get(f"{url}/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.25)
    else:
        proc.kill()
        pytest.fail("server did not start")
    yield url
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
async def browser():
    if not CHROMIUM:
        pytest.skip("no Chromium available (set CHROMIUM_PATH)")
    async with pw.async_playwright() as p:
        b = await p.chromium.launch(executable_path=CHROMIUM)
        yield b
        await b.close()


_n = itertools.count(1)


class Session:
    """One signed-in user in their own browser context."""

    def __init__(self, page, base, name):
        self.p, self.base, self.name = page, base, name
        self.errors = []
        page.set_default_timeout(15000)
        page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: self.errors.append(f"console: {m.text}") if m.type == "error" else None)

    async def goto(self, path):
        await self.p.goto(self.base + path)
        await self.p.wait_for_load_state("networkidle")
        try:
            await self.p.wait_for_function("() => window.socket && window.socket.connected", timeout=5000)
        except Exception:  # noqa: BLE001
            await self.p.wait_for_timeout(1000)

    async def toast(self, text, timeout=10000):
        await self.p.locator(".q-notification", has_text=text).first.wait_for(timeout=timeout)

    async def signup(self, phone="+254700000000"):
        await self.goto("/login")
        await self.p.get_by_role("tab", name="Create account").click()
        await self.p.wait_for_selector("text=Password (8+ characters)")
        await self.p.get_by_label("Name").last.fill(self.name)
        await self.p.get_by_label("Password (8+ characters)").fill("password123")
        await self.p.get_by_label("Phone").fill(phone)
        await self.p.get_by_role("button", name="Create account").click()
        await self.p.wait_for_url("**/agents")

    async def sign_in(self):
        await self.goto("/login")
        await self.p.get_by_label("Name").last.fill(self.name)
        await self.p.get_by_label("Password").last.fill("password123")
        await self.p.get_by_role("button", name="Sign in").click()
        await self.p.wait_for_url(lambda u: "/login" not in u)


ADMIN = "Root Admin"


@pytest.fixture
async def make_session(browser, base_url):
    """Factory for signed-in users. The very first account on a fresh server is the administrator,
    so the factory makes sure that is 'Root Admin' before anyone else registers."""
    state = _state.setdefault(base_url, {"admin": False})

    async def new(name, *, signup=True):
        ctx = await browser.new_context(viewport={"width": 1280, "height": 1000})
        s = Session(await ctx.new_page(), base_url, name)
        if signup:
            await s.signup(f"+2547{next(_n):08d}")
        else:
            await s.sign_in()
        return s

    if not state["admin"]:
        (await new(ADMIN)).p.context.close  # noqa: B018  (registers the admin; context left open until session end)
        state["admin"] = True

    async def make(prefix):
        return await new(f"{prefix} {next(_n)}")

    async def admin():
        return await new(ADMIN, signup=False)

    make.admin = admin
    return make


_state: dict = {}
