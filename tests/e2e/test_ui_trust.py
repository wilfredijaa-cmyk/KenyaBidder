"""Real-browser journeys for RFQs, verification, business badges, Kiswahili and account deletion."""
import re

import pytest

from .test_ui_journey import create_agent, save_rules

pytestmark = pytest.mark.e2e


async def test_rfq_journey_buyer_posts_supplier_quotes(make_session):
    buyer, supplier = await make_session("RfqBuyer"), await make_session("RfqSupplier")
    await create_agent(buyer, "buyer")
    await save_rules(buyer, **{"Budget ceiling per bid (KES)": 50000})
    await create_agent(supplier, "seller")
    await save_rules(supplier, **{"Price floor (KES) — never list or quote below this": 1000})
    await buyer.goto("/")
    await buyer.p.get_by_text("Request quotes (RFQ)").click()
    await buyer.p.get_by_label("What do you need?").fill("Rfq chargers")
    await buyer.p.get_by_label("Category").fill("cat-rfq")
    await buyer.p.get_by_label("Maximum total price (KES)").fill("20000")
    await buyer.p.get_by_label("Duration (minutes)").fill("5")
    await buyer.p.get_by_role("button", name="Post request").click()
    await buyer.toast("Request posted")

    await supplier.goto("/")
    await supplier.p.wait_for_selector("text=Rfq chargers")
    await supplier.p.locator(".q-card", has_text="Rfq chargers").first.click()
    await supplier.p.wait_for_selector("text=Request for quotes")
    await supplier.p.get_by_label("Manual quote (KES)").fill("25000")
    await supplier.p.get_by_role("button", name="Quote manually").click()
    await supplier.toast("BID_TOO_HIGH")  # above the buyer's maximum
    await supplier.p.get_by_label("Manual quote (KES)").fill("18000")
    await supplier.p.get_by_role("button", name="Quote manually").click()
    await supplier.toast("Quote placed")
    await supplier.p.wait_for_selector("text=Quotes (1)")
    await buyer.goto("/")
    await buyer.p.locator(".q-card", has_text="Rfq chargers").first.click()
    await buyer.p.wait_for_selector("text=Quotes (1)")
    await buyer.p.wait_for_selector("text=KES 18,000")
    for s in (buyer, supplier):
        assert not s.errors, s.errors


async def test_phone_verification_business_badge_and_swahili(make_session):
    user = await make_session("Trusty")
    await create_agent(user, "buyer")
    await user.goto("/profile")
    await user.p.get_by_role("button", name="Send code").first.click()
    toast = user.p.locator(".q-notification", has_text="dev mode").first
    await toast.wait_for()
    code = re.search(r"your code is (\d{6})", await toast.inner_text()).group(1)
    await user.p.get_by_label("6-digit code").first.fill(code)
    await user.p.get_by_role("button", name="Confirm").first.click()
    await user.p.wait_for_selector("text=verified")

    await user.p.get_by_label("Registration number").fill("A123456789Z")
    await user.p.get_by_role("button", name="Apply for the badge").click()
    await user.toast("Application submitted")
    await user.p.wait_for_selector("text=under review")
    admin = await make_session.admin()
    await admin.goto("/admin")
    await admin.p.get_by_role("tab", name="Trust & disputes").click()
    await admin.p.wait_for_selector("text=A123456789Z")
    await admin.p.locator(".q-card", has_text="A123456789Z").get_by_role("button", name="Approve").click()
    await admin.p.locator(".q-dialog").get_by_role("button", name="Approve").click()
    await admin.p.wait_for_selector("text=No applications waiting")
    await user.goto("/profile")
    await user.p.wait_for_selector("text=✓ verified")

    await user.p.get_by_role("button", name="SW", exact=True).click()  # Kiswahili
    await user.p.wait_for_selector("text=Wasifu wangu")
    await user.goto("/")
    await user.p.wait_for_selector("text=Mawakala wangu")
    await user.p.get_by_role("button", name="EN", exact=True).click()
    await user.p.wait_for_selector("text=My agents")
    for s in (user, admin):
        assert not s.errors, s.errors


async def test_forgot_password_dialog_never_reveals_accounts(make_session):
    user = await make_session("Forgetful")
    await user.p.context.clear_cookies()
    await user.goto("/login")
    await user.p.get_by_label("Name").last.fill("nobody-at-all")
    await user.p.get_by_role("button", name="Forgot password?").click()
    await user.p.get_by_role("button", name="Send code").click()
    await user.toast("If that account has a verified phone or email")
    await user.p.get_by_label("Name").last.fill("Forgetful")  # a real (but unverified) account gets the very same answer
    await user.p.get_by_role("button", name="Send code").click()
    await user.toast("If that account has a verified phone or email")
    assert not user.errors, user.errors


async def test_delete_account_from_profile(make_session):
    user = await make_session("Leaver")
    await user.goto("/profile")
    await user.p.get_by_text("Delete my account").first.click()
    await user.p.get_by_label("Your password").fill("password123")
    await user.p.get_by_role("button", name="Delete my account permanently").click()
    await user.p.wait_for_url("**/login")
    await user.p.get_by_label("Name").last.fill(user.name)
    await user.p.get_by_label("Password").last.fill("password123")
    await user.p.get_by_role("button", name="Sign in").click()
    await user.toast("Wrong name or password")
    assert not user.errors, user.errors
