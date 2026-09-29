"""Real-browser journeys through the NiceGUI console."""
import re

import pytest

pytestmark = pytest.mark.e2e


async def create_agent(s, kind):
    await s.p.get_by_role("button", name=f"New {kind} agent").click()
    await s.p.wait_for_url("**/agent/*")
    await s.p.wait_for_selector(f"text={'Buyer' if kind == 'buyer' else 'Seller'} agent")


async def save_rules(s, **fields):
    for label, value in fields.items():
        await s.p.get_by_label(label).fill(str(value))
    await s.p.get_by_role("button", name="Save rules").click()
    await s.toast("Saved")


async def list_english(s, title, *, category="electronics", reserve=1000, start=1000, inc=100, mins=0.4):
    await s.goto("/")
    await s.p.get_by_label("Product").fill(title)
    await s.p.get_by_label("Category").fill(category)
    await s.p.get_by_label("Reserve (hidden from bidders)").fill(str(reserve))
    await s.p.get_by_label("Start price").fill(str(start))
    await s.p.get_by_label("Min increment").fill(str(inc))
    await s.p.get_by_label("Duration (minutes)").fill(str(mins))
    await s.p.get_by_role("button", name="Create listing").click()
    await s.toast("Listing created")


async def test_seller_listing_to_match_and_contact_reveal(make_session):
    seller, buyer = await make_session("Seller"), await make_session("Buyer")
    await create_agent(seller, "seller")
    await save_rules(seller, **{"Price floor (KES) — never list or quote below this": 500})
    await create_agent(buyer, "buyer")
    await save_rules(buyer, **{"Budget ceiling per bid (KES)": 5000, "Category": "cat-journey"})
    await buyer.p.get_by_role("tab", name="Brain, tools & knowledge").click()
    await buyer.p.locator(".q-select").filter(has_text="heuristic").first.click()  # ENGLISH row
    await buyer.p.get_by_role("option", name=re.compile("^baseline")).click()
    await buyer.p.get_by_role("button", name="Save brain, tools & knowledge").click()
    await buyer.toast("Brain configuration saved")

    await list_english(seller, "Journey phones", category="cat-journey")
    await buyer.goto("/")
    await buyer.p.wait_for_selector("text=Journey phones")
    await buyer.p.locator(".q-card", has_text="Journey phones").first.click()
    await buyer.p.wait_for_selector("text=Bids (")

    await buyer.goto("/matches")
    await buyer.p.wait_for_selector("text=Confirm match", timeout=45000)
    await buyer.p.get_by_role("button", name="Confirm match").click()
    await seller.goto("/matches")
    await seller.p.get_by_role("button", name="Confirm match").click()
    await seller.p.wait_for_selector(f"text={buyer.name} ·")  # contact revealed only after both confirmed
    await seller.p.get_by_role("button", name="completed").click()
    await buyer.goto("/matches")
    await buyer.p.get_by_role("button", name="completed").click()
    await buyer.p.wait_for_selector("text=Contact revealed")
    for s in (seller, buyer):
        assert not s.errors, s.errors


async def test_signin_rejects_bad_password_and_admin_pages_are_protected(make_session):
    user = await make_session("Plain")
    await user.goto("/admin")
    await user.p.wait_for_url(lambda u: "/admin" not in u)  # non-admins are bounced, with an explanation
    await user.toast("Administrators only")
    assert await user.p.locator("text=Administration").count() == 0
    admin = await make_session.admin()  # sanity: the admin can reach it
    await admin.goto("/admin")
    await admin.p.wait_for_selector("text=Administration")
    # wrong password is rejected
    ghost = await make_session.admin()
    await ghost.p.context.clear_cookies()
    await ghost.goto("/login")
    await ghost.p.get_by_label("Name").last.fill("Root Admin")
    await ghost.p.get_by_label("Password").last.fill("not-the-password")
    await ghost.p.get_by_role("button", name="Sign in").click()
    await ghost.toast("Wrong name or password")


async def test_llm_tools_and_kb_end_to_end(make_session, mock_llm):
    """Admin registers a (mock) LLM; the buyer's ENGLISH algorithm uses it with an assigned MCP tool and KB;
    the LLM researches through the tool, proposes, and the deterministic engine places the bid."""
    admin = await make_session.admin()
    await admin.goto("/admin")
    await admin.p.get_by_role("button", name="Add LLM").click()
    await admin.p.get_by_label("Display name").fill("Mock GPT")
    await admin.p.get_by_label("Provider").click()
    await admin.p.get_by_role("option", name=re.compile("OpenAI-compatible")).click()
    await admin.p.get_by_label("Model", exact=True).fill("mock-1")
    await admin.p.get_by_label("Base URL", exact=False).fill(mock_llm["url"])
    await admin.p.get_by_label("API key", exact=False).first.fill("sk-mock")
    await admin.p.get_by_label("Who pays for this model?").click()
    await admin.p.get_by_role("option", name=re.compile("^Free")).click()
    await admin.p.get_by_role("button", name="Save").click()
    await admin.p.wait_for_selector("text=Mock GPT")
    await admin.p.locator(".q-card", has_text="Mock GPT").get_by_role("button", name="Test").click()
    await admin.p.wait_for_selector("text=reachable")
    await admin.p.get_by_role("tab", name="Knowledge bases").click()
    await admin.p.get_by_role("button", name="New knowledge base").click()
    await admin.p.get_by_label("Name").last.fill("Journey KB")
    await admin.p.get_by_role("button", name="Create").click()
    await admin.p.locator(".q-card", has_text="Journey KB").get_by_role("button", name="Add document").click()
    await admin.p.get_by_label("Title").fill("Guide")
    await admin.p.get_by_label("Text").fill("Journey phones clear near 2500 KES.")
    await admin.p.get_by_role("button", name="Add", exact=True).click()
    await admin.p.wait_for_selector("text=1 doc(s)")
    await admin.p.get_by_role("tab", name="MCP servers").click()
    await admin.p.wait_for_selector("text=8 tool(s) discovered")

    seller, buyer = await make_session("LlmSeller"), await make_session("LlmBuyer")
    await create_agent(seller, "seller")
    await create_agent(buyer, "buyer")
    await save_rules(buyer, **{"Budget ceiling per bid (KES)": 5000, "Category": "cat-llm"})
    await buyer.p.get_by_role("tab", name="Brain, tools & knowledge").click()
    await buyer.p.get_by_label("Default LLM").click()
    await buyer.p.get_by_role("option", name=re.compile("Mock GPT")).click()
    await buyer.p.locator(".q-select").filter(has_text="heuristic").first.click()
    await buyer.p.get_by_role("option", name=re.compile("^llm")).click()
    await buyer.p.get_by_text(re.compile("KenyaBidder built-in")).first.click()
    await buyer.p.get_by_text("get_historical_clearing_prices").first.click()
    await buyer.p.get_by_text(re.compile("Journey KB")).first.click()
    await buyer.p.get_by_role("button", name="Save brain, tools & knowledge").click()
    await buyer.toast("Brain configuration saved")

    n_before = len(mock_llm["calls"])
    await list_english(seller, "LLM phones", category="cat-llm", reserve=1000, start=1000, mins=1)
    await buyer.goto("/activity")
    await buyer.p.wait_for_selector("text=LLM phones ·", timeout=20000)
    await buyer.p.get_by_text("LLM phones ·").first.click()
    await buyer.p.wait_for_selector("text=mock LLM: researched the market")
    await buyer.p.wait_for_selector("text=LLM steps")

    calls = mock_llm["calls"][n_before:]
    first, second = calls[0], calls[1]
    names = {t["function"]["name"] for t in first["tools"]}
    assert names == {"kb__get_historical_clearing_prices", "search_knowledge", "propose_action"}  # only what was assigned
    assert "submit_bid" not in str(first["tools"])
    tool_msg = next(m for m in second["messages"] if m["role"] == "tool")
    assert "<untrusted_tool_result>" in tool_msg["content"]  # tool output reached the LLM as untrusted data
    assert "Journey KB" in first["messages"][0]["content"]
    assert "untrusted" in first["messages"][0]["content"]

    await buyer.goto("/")
    await buyer.p.locator(".q-card", has_text="LLM phones").first.click()
    await buyer.p.wait_for_selector("text=You")  # the engine, not the LLM, placed the bid
    for s in (admin, seller, buyer):
        assert not s.errors, s.errors


async def test_escalation_approval_flow(make_session):
    seller, buyer = await make_session("EscSeller"), await make_session("EscBuyer")
    await create_agent(seller, "seller")
    await create_agent(buyer, "buyer")
    await save_rules(buyer, **{"Budget ceiling per bid (KES)": 10000, "Ask me before bidding above this % of the ceiling": 50, "Category": "cat-esc"})
    await buyer.p.get_by_role("tab", name="Brain, tools & knowledge").click()
    await buyer.p.locator(".q-select").filter(has_text="heuristic").first.click()
    await buyer.p.get_by_role("option", name=re.compile("^baseline")).click()
    await buyer.p.get_by_role("button", name="Save brain, tools & knowledge").click()
    await buyer.toast("Brain configuration saved")
    await list_english(seller, "Escalation phones", category="cat-esc", reserve=6000, start=6000, mins=2)
    await buyer.goto("/inbox")
    await buyer.p.wait_for_selector("text=Needs your decision")
    await buyer.p.wait_for_selector("text=Bid of KES 6,000", timeout=15000)
    await buyer.p.get_by_role("button", name="Approve").click()
    await buyer.p.wait_for_selector("text=Nothing waiting on you")
    await buyer.goto("/")
    await buyer.p.locator(".q-card", has_text="Escalation phones").first.click()
    await buyer.p.wait_for_selector("text=KES 6,000")
    assert not buyer.errors, buyer.errors


async def test_pause_revokes_and_manual_bid_hits_guardrail(make_session):
    seller, buyer = await make_session("PSeller"), await make_session("PBuyer")
    await create_agent(seller, "seller")
    await create_agent(buyer, "buyer")
    await save_rules(buyer, **{"Budget ceiling per bid (KES)": 2000})
    await list_english(seller, "Guardrail phones", category="cat-guard", reserve=1000, start=1000, mins=3)
    await buyer.goto("/")
    await buyer.p.locator(".q-card", has_text="Guardrail phones").first.click()
    await buyer.p.get_by_label("Manual bid (KES)").fill("999999")
    await buyer.p.get_by_role("button", name="Bid manually").click()
    await buyer.toast("CEILING_EXCEEDED")
    await buyer.p.get_by_label("Manual bid (KES)").fill("1000")
    await buyer.p.get_by_role("button", name="Bid manually").click()
    await buyer.toast("Bid placed")
    await buyer.goto("/agents")
    await buyer.p.locator(".q-card", has_text="Buyer agent").first.click()
    await buyer.p.get_by_role("button", name="Pause agent").click()
    await buyer.toast("Paused")
    await buyer.p.wait_for_selector("text=Resume agent")
    assert not buyer.errors, buyer.errors


async def test_token_gate_purchase_and_resume(make_session, mock_llm):
    """An agent on a *metered* LLM cannot decide without tokens; the owner buys a pack and the agent takes over."""
    admin = await make_session.admin()
    await admin.goto("/admin")
    await admin.p.get_by_role("button", name="Add LLM").click()
    await admin.p.get_by_label("Display name").fill("Metered GPT")
    await admin.p.get_by_label("Provider").click()
    await admin.p.get_by_role("option", name=re.compile("OpenAI-compatible")).click()
    await admin.p.get_by_label("Model", exact=True).fill("mock-2")
    await admin.p.get_by_label("Base URL", exact=False).fill(mock_llm["url"])
    await admin.p.get_by_label("API key", exact=False).first.fill("sk-mock")
    await admin.p.get_by_role("button", name="Save").click()
    await admin.p.wait_for_selector("text=Metered GPT")
    await admin.p.get_by_role("tab", name="Billing").click()
    await admin.p.get_by_role("button", name="Add pack").click()
    await admin.p.get_by_label("Pack name").fill("Trial pack")
    await admin.p.get_by_label("Tokens").fill("50000")
    await admin.p.get_by_label("Price (KES)").fill("250")
    await admin.p.get_by_role("button", name="Add", exact=True).click()
    await admin.p.wait_for_selector("text=Trial pack")

    seller, buyer = await make_session("TokSeller"), await make_session("TokBuyer")
    await create_agent(seller, "seller")
    await create_agent(buyer, "buyer")
    await save_rules(buyer, **{"Budget ceiling per bid (KES)": 5000, "Category": "cat-tok"})
    await buyer.p.get_by_role("tab", name="Brain, tools & knowledge").click()
    await buyer.p.get_by_label("Default LLM").click()
    await buyer.p.get_by_role("option", name=re.compile("Metered GPT")).click()
    await buyer.p.locator(".q-select").filter(has_text="heuristic").first.click()
    await buyer.p.get_by_role("option", name=re.compile("^llm")).click()
    await buyer.p.get_by_role("button", name="Save brain, tools & knowledge").click()
    await buyer.toast("Brain configuration saved")
    await buyer.p.wait_for_selector("text=needs at least")          # the agent page says plainly it cannot operate yet

    await list_english(seller, "Token phones", category="cat-tok", reserve=1000, start=1000, mins=3)
    await buyer.goto("/inbox")
    await buyer.p.wait_for_selector("text=Your agent is waiting", timeout=20000)   # told once, with a pointer to the wallet
    await buyer.goto("/")
    await buyer.p.locator(".q-card", has_text="Token phones").first.click()
    await buyer.p.wait_for_selector("text=Bids (")
    assert await buyer.p.get_by_role("cell", name="You", exact=True).count() == 0    # it has not bid

    await buyer.goto("/wallet")
    await buyer.p.get_by_role("button", name="Buy", exact=True).first.click()
    await buyer.p.get_by_role("button", name="Continue").click()
    await buyer.toast("Tokens added to your wallet")
    await buyer.p.wait_for_selector("text=50,000")
    await buyer.goto("/")                                                            # the top-up woke the agent: it now bids
    await buyer.p.locator(".q-card", has_text="Token phones").first.click()
    await buyer.p.get_by_role("cell", name="You", exact=True).first.wait_for(timeout=20000)
    await buyer.goto("/wallet")
    await buyer.p.wait_for_selector("text=LLM calls")                                # usage is itemised per agent
    for s in (admin, seller, buyer):
        assert not s.errors, s.errors
