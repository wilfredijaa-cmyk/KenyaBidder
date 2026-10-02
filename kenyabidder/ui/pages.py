"""User-facing pages: login, auctions, auction detail, inbox, matches, activity, platform."""
from __future__ import annotations

import json
import os

from nicegui import ui

from ..engine import AUCTION_TYPES, FORWARD_TYPES, REVERSE_TYPES
from ..i18n import Verbatim
from .common import (active_agent, agent_picker, badge, core, current_user, empty, fmt_time, frame, guard, kes, left,
                     client_ip, lang_toggle, login_user, my_agents, pretty, require_user, rt, set_active_agent, theme)


def register() -> None:
    ui.page("/login")(login_page)
    ui.page("/")(auctions_page)
    ui.page("/auction/{auction_id}")(auction_page)
    ui.page("/inbox")(inbox_page)
    ui.page("/matches")(matches_page)
    ui.page("/activity")(activity_page)
    ui.page("/platform")(platform_page)


# ----------------------------------------------------------------------------- login

def login_page():
    theme()
    if current_user():
        ui.navigate.to("/")
        return
    first = not core().store.users
    with ui.column().classes("absolute-center items-stretch w-96 max-w-full gap-3 p-2"):
        with ui.row().classes("w-full justify-end"):
            lang_toggle()
        with ui.row().classes("items-center justify-center gap-0"):
            ui.label("Kenya").classes("text-3xl font-semibold")
            ui.label("Bidder").classes("text-3xl font-semibold text-primary")
        ui.label("AI agents that buy and sell for you. The platform finds the match — payment and delivery stay between the two of you.").classes("text-center text-sm opacity-70")
        with ui.card().classes("w-full"):
            with ui.tabs().classes("w-full") as tabs:
                t_in = ui.tab("Sign in")
                t_up = ui.tab("Create account")
            with ui.tab_panels(tabs, value=t_up if first else t_in).classes("w-full"):
                with ui.tab_panel(t_in):
                    name = ui.input("Name").props("autofocus").classes("w-full")
                    pw = ui.input("Password", password=True, password_toggle_button=True).classes("w-full")

                    @guard
                    async def sign_in():
                        ip = client_ip()
                        # counted BEFORE the awaited password check, so a burst of concurrent guesses cannot all slip under the cap
                        core().throttle.hit("login_fail_ip", ip, 25, 600, message="too many failed sign-in attempts from your network — try again in a few minutes")
                        u = await core().agents.authenticate_async(name.value, pw.value)
                        if not u:
                            ui.notify("Wrong name or password", type="negative")
                            return
                        core().throttle.forgive("login_fail_ip", ip)
                        if core().agents.totp_enabled(u):
                            two_factor_dialog(u, ip)
                            return
                        login_user(u)
                        ui.navigate.to("/")
                    pw.on("keydown.enter", sign_in)
                    ui.button("Sign in", on_click=sign_in).props("unelevated color=primary").classes("w-full")
                    ui.button("Forgot password?", on_click=lambda: forgot_dialog(name.value)).props("flat dense no-caps").classes("w-full")
                with ui.tab_panel(t_up):
                    if first:
                        ui.label("You are the first user, so this account becomes the administrator.").classes("text-sm text-primary")
                    n2 = ui.input("Name").classes("w-full")
                    p2 = ui.input("Password (8+ characters)" if core().agents.password_policy() == "basic" else "Password (10+ characters)",
                                  password=True, password_toggle_button=True).classes("w-full")
                    ph = ui.input("Phone (shown to a counterparty only after both confirm a match)").classes("w-full")
                    em = ui.input("Email").classes("w-full")
                    with ui.row().classes("items-center gap-1 no-wrap"):
                        agree = ui.checkbox("I accept the")
                        ui.link("Terms & Privacy Notice", "/terms", new_tab=True).classes("-ml-3 text-sm")

                    @guard
                    def sign_up():
                        core().throttle.hit("signup_ip", client_ip(), int(os.environ.get("KENYABIDDER_SIGNUPS_PER_IP_HOUR", 6)), 3600, message="too many accounts created from your network — try again later")
                        u = core().agents.create_user(name=n2.value, password=p2.value, phone=ph.value, email=em.value, accepted_terms=bool(agree.value))
                        login_user(u)
                        ui.navigate.to("/agents")
                    ui.button("Create account", on_click=sign_up).props("unelevated color=primary").classes("w-full")


def two_factor_dialog(user: dict, ip: str) -> None:
    """Second sign-in step for accounts with an authenticator app. The password alone never creates a session."""
    with ui.dialog().props("persistent") as d, ui.card().classes("w-96 max-w-full"):
        ui.label("Two-factor authentication").classes("text-lg font-medium")
        ui.label("Enter the 6-digit code from your authenticator app (or a recovery code).").classes("text-sm opacity-70")
        code = ui.input("Code").props("autofocus inputmode=numeric autocomplete=one-time-code").classes("w-full")

        @guard
        def verify():
            core().throttle.hit("login_fail_ip", ip, 25, 600)
            if not core().agents.totp_check(user, code.value):
                ui.notify("That code is not right", type="negative")
                return
            core().throttle.forgive("login_fail_ip", ip)
            d.close()
            login_user(user)
            ui.navigate.to("/")
        code.on("keydown.enter", verify)
        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=d.close).props("flat no-caps")
            ui.button("Verify", on_click=verify).props("unelevated color=primary no-caps")
    d.open()


def forgot_dialog(prefill: str = "") -> None:
    """Self-service reset: a code goes to the account's VERIFIED phone (or email); the answer never reveals whether the account exists."""
    with ui.dialog() as d, ui.card().classes("w-96 max-w-full"):
        ui.label("Reset your password").classes("text-lg font-medium")
        ui.label("Enter your name. If the account has a verified phone or email, we send a 6-digit code there.").classes("text-sm opacity-70")
        nm = ui.input("Name", value=prefill or "").classes("w-full")
        code = ui.input("6-digit code").classes("w-full")
        newpw = ui.input("New password (8+ characters)", password=True, password_toggle_button=True).classes("w-full")

        @guard
        async def send():
            core().throttle.hit("reset_ip", client_ip(), 10, 3600)
            r = await core().verification.request_password_reset(nm.value)
            ui.notify(r["message"], type="info", multi_line=True)

        @guard
        def finish():
            core().verification.reset_password(nm.value, code.value, newpw.value)
            ui.notify("Password changed — sign in with your new password", type="positive")
            d.close()
        with ui.row().classes("w-full justify-between"):
            ui.button("Send code", on_click=send).props("outline no-caps")
            ui.button("Set new password", on_click=finish).props("unelevated color=primary no-caps")
        ui.label("No verified phone or email? Ask support — an administrator can set a temporary password.").classes("text-xs opacity-60")
    d.open()


# ----------------------------------------------------------------------------- auctions

def auctions_page():
    user = require_user()
    if not user:
        return
    with frame(user, "/"):
        agent = active_agent(user)
        if not agent:
            with ui.card().classes("w-full items-center"):
                ui.label("Create your first agent to start").classes("text-lg")
                ui.label("A Buyer agent bids for you; a Seller agent lists and manages your stock.").classes("opacity-70")
                ui.button("Create an agent", on_click=lambda: ui.navigate.to("/agents")).props("unelevated color=primary")
            return
        agent_picker(user, lambda: ui.navigate.reload())
        checklist(user, agent)
        if agent["agent_type"] == "SELLER":
            listing_form(agent)
        else:
            rfq_form(agent)
        ui.label("Auctions & requests for quotes").classes("text-lg font-medium")

        @ui.refreshable
        def grid():
            core().engine.tick()
            rows = sorted(core().store.auctions.values(), key=lambda a: -a["created_at"])
            if not rows:
                empty("No listings yet — create one above." if agent["agent_type"] == "SELLER" else "No auctions yet. Sellers will appear here as they list.")
                return
            with ui.grid().classes("w-full gap-3 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3"):
                for a in rows:
                    v = core().engine.view(a, agent["agent_id"])
                    with ui.card().classes("cursor-pointer").on("click", lambda _, i=a["auction_id"]: ui.navigate.to(f"/auction/{i}")):
                        with ui.row().classes("w-full items-center"):
                            ui.label(Verbatim(v["product_spec"]["title"])).classes("font-medium")
                            ui.space()
                            status_badge(v)
                        ui.label(f"{v['product_spec']['category']} · qty {v['product_spec']['quantity']} · {pretty(v['auction_type'])}").classes("text-xs opacity-70")
                        ui.label("sealed" if v["current_price"] is None else kes(v["current_price"])).classes("text-2xl font-bold")
                        if v["direction"] == "REVERSE":
                            ui.label("RFQ — suppliers quote the price down" + (f" (buyer's max {kes(v['max_price'])})" if v["max_price"] else "")).classes("text-xs text-primary")
                        with ui.row().classes("text-xs opacity-70 gap-3"):
                            ui.label(f"{v['bid_count']} bid{'s' if v['bid_count'] != 1 else ''}")
                            if v["status"] in ("ACTIVE", "EXTENDING", "SCHEDULED"):
                                ui.label(left(v["extended_until"] or v["ends_at"]))
                            if v["poster_agent_id"] == agent["agent_id"]:
                                badge("yours", "primary")
                            if v["poster_verified"]:
                                badge("✓ verified business", "positive")
                            if v["verified_only"]:
                                badge("verified bidders only", "warning")
        grid()
        ui.timer(1.0, grid.refresh)


def checklist(user: dict, agent: dict) -> None:
    """Getting-started card — shown until every step is done, then disappears."""
    c = core()
    steps = []
    cfg = agent["config"]
    if agent["agent_type"] == "BIDDER":
        steps.append(("Tell your agent what to hunt for (category & budget)", bool((agent["durable_memory"].get("watch") or {}).get("category")), f"/agent/{agent['agent_id']}"))
    else:
        steps.append(("Create your first listing", any(a["poster_agent_id"] == agent["agent_id"] for a in c.store.auctions.values()), "/"))
    if c.meter.llm_ids_for(agent):
        st = c.meter.status_for_agent(agent)
        steps.append(("Top up tokens so your agent's LLM can decide", st["ok"], "/wallet"))
    elif any(c.meter.is_metered(e) for e in c.llms.list(enabled_only=True)):
        steps.append(("Give your agent an LLM brain (optional)", bool(cfg.get("llm_id")), f"/agent/{agent['agent_id']}"))
    steps.append(("Link WhatsApp to get alerts on your phone", bool(agent["channel_identity_map"]), f"/agent/{agent['agent_id']}"))
    steps.append(("Add and verify your phone number", bool(user.get("phone")) and (bool(user.get("phone_verified")) or not c.verification.phone_required()), "/profile"))
    if all(done for _, done, _ in steps):
        return
    with ui.card().classes("w-full border border-primary"):
        ui.label("Getting started").classes("font-medium")
        for text, done, link in steps:
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.icon("check_circle" if done else "radio_button_unchecked").classes("text-primary" if done else "opacity-50")
                ui.link(text, link).classes("text-sm" + (" line-through opacity-60" if done else ""))


def status_badge(v: dict) -> None:
    if v["status"] == "SETTLED":
        sold = (v["result"] or {}).get("outcome") == "SOLD"
        badge("sold" if sold else "unsold", "positive" if sold else "warning")
    elif v["status"] == "CANCELLED":
        badge("cancelled", "negative")
    else:
        badge(v["status"].lower())


def listing_form(agent: dict) -> None:
    allowed = [t for t in agent["constraints"]["authorized_auction_types"] if t in FORWARD_TYPES] or list(FORWARD_TYPES)
    f = {"title": "", "category": "", "qty": 1, "type": allowed[0], "reserve": 1000, "start": 1000, "inc": 100, "mins": 5,
         "dstart": 5000, "dfloor": 1000, "ddec": 250, "dstep": 20, "vonly": False}
    with ui.expansion("New listing", icon="add_circle", value=not any(a["poster_agent_id"] == agent["agent_id"] for a in core().store.auctions.values())).classes("w-full border rounded"):
        with ui.column().classes("w-full gap-2 p-2"):
            with ui.row().classes("w-full"):
                ui.input("Product", on_change=lambda e: f.update(title=e.value)).classes("grow")
                ui.input("Category", placeholder="electronics", on_change=lambda e: f.update(category=e.value)).classes("w-48")
                ui.number("Quantity", value=1, min=1, precision=0, on_change=lambda e: f.update(qty=int(e.value or 1))).classes("w-28")
            with ui.row().classes("w-full items-end"):
                typ = ui.select({t: pretty(t) for t in allowed}, value=f["type"], label="Auction type", on_change=lambda e: (f.update(type=e.value), fields.refresh())).classes("w-64")
                rec = ui.label().classes("text-sm text-primary grow")

                @guard
                async def ask():
                    r = await core().advisor.recommend(agent, f["category"] or "general", f["qty"])
                    typ.set_value(r["auction_type"])
                    if r["suggested_reserve"]:
                        f["reserve"], f["start"] = r["suggested_reserve"], r["suggested_start_price"] or f["start"]
                        fields.refresh()
                    rec.set_text(f"Agent recommends {pretty(r['auction_type'])} ({r['source']}): {r['reasoning']}")
                ui.button("Ask my agent", icon="psychology", on_click=ask).props("outline")

            ui.checkbox("Only verified businesses may bid", value=False, on_change=lambda e: f.update(vonly=e.value))

            @ui.refreshable
            def fields():
                with ui.row().classes("w-full"):
                    num("Reserve (hidden from bidders)", "reserve", 0)
                    if f["type"] == "ENGLISH":
                        num("Start price", "start", 0)
                        num("Min increment", "inc", 1)
                    elif f["type"] == "DUTCH":
                        num("Start price", "dstart", 1)
                        num("Floor price", "dfloor", 0)
                        num("Drop per step", "ddec", 1)
                        num("Step (seconds)", "dstep", 1)
                    num("Duration (minutes)", "mins", 0.1, precision=1)

            def num(label, key, mn, precision=0):
                ui.number(label, value=f[key], min=mn, precision=precision, on_change=lambda e: f.update({key: e.value})).classes("w-44")
            fields()

            @guard
            def create():
                kw = dict(seller_agent_id=agent["agent_id"], product_spec={"title": f["title"], "category": f["category"], "quantity": f["qty"]},
                          auction_type=f["type"], reserve_price=int(f["reserve"] or 0), duration_ms=int(float(f["mins"] or 0) * 60_000), verified_only=f["vonly"])
                if f["type"] == "ENGLISH":
                    kw.update(start_price=int(f["start"] or 0), min_increment=int(f["inc"] or 1), anti_snipe={"window_ms": 10_000, "extend_ms": 15_000})
                if f["type"] == "DUTCH":
                    kw["dutch"] = {"start_price": int(f["dstart"]), "floor_price": int(f["dfloor"]), "decrement": int(f["ddec"]), "interval_ms": int(f["dstep"]) * 1000}
                core().engine.create_listing(**kw)
                ui.notify("Listing created", type="positive")
            ui.button("Create listing", icon="check", on_click=create).props("unelevated color=primary")


def rfq_form(agent: dict) -> None:
    """A buyer posts what it needs and the most it will pay; supplier agents compete by quoting the price down."""
    f = {"title": "", "category": "", "qty": 1, "type": "REVERSE_ENGLISH", "max": 50_000, "dec": 100, "mins": 30, "vonly": False}
    with ui.expansion("Request quotes (RFQ)", icon="request_quote").classes("w-full border rounded"):
        with ui.column().classes("w-full gap-2 p-2"):
            ui.label("Post what you need and the most you would pay in total. Suppliers' agents bid the price down — the lowest valid quote wins the introduction.").classes("text-sm opacity-70")
            with ui.row().classes("w-full"):
                ui.input("What do you need?", on_change=lambda e: f.update(title=e.value)).classes("grow")
                ui.input("Category", placeholder="electronics", on_change=lambda e: f.update(category=e.value)).classes("w-48")
                ui.number("Quantity", value=1, min=1, precision=0, on_change=lambda e: f.update(qty=int(e.value or 1))).classes("w-28")
            with ui.row().classes("w-full items-end"):
                ui.select({t: pretty(t) for t in REVERSE_TYPES}, value=f["type"], label="Format", on_change=lambda e: (f.update(type=e.value), dec.set_visibility(e.value == "REVERSE_ENGLISH"))).classes("w-64")
                ui.number("Maximum total price (KES)", value=f["max"], min=1, precision=0, on_change=lambda e: f.update(max=int(e.value or 0))).classes("w-56")
                dec = ui.number("Min. undercut (KES)", value=f["dec"], min=1, precision=0, on_change=lambda e: f.update(dec=int(e.value or 1))).classes("w-44")
                ui.number("Duration (minutes)", value=f["mins"], min=0.1, precision=1, on_change=lambda e: f.update(mins=e.value)).classes("w-44")

            ui.checkbox("Only verified businesses may quote", value=False, on_change=lambda e: f.update(vonly=e.value))

            @guard
            def post():
                kw = dict(buyer_agent_id=agent["agent_id"], product_spec={"title": f["title"], "category": f["category"], "quantity": f["qty"]},
                          auction_type=f["type"], max_price=int(f["max"] or 0), duration_ms=int(float(f["mins"] or 0) * 60_000), min_decrement=int(f["dec"] or 1), verified_only=f["vonly"])
                if f["type"] == "REVERSE_ENGLISH":
                    kw["anti_snipe"] = {"window_ms": 10_000, "extend_ms": 15_000}
                core().engine.create_rfq(**kw)
                ui.notify("Request posted — supplier agents have been notified", type="positive")
            ui.button("Post request", icon="send", on_click=post).props("unelevated color=primary")


# ----------------------------------------------------------------------------- auction detail

def auction_page(auction_id: str):
    user = require_user()
    if not user:
        return
    with frame(user, "/"):
        agent = active_agent(user)
        a = core().store.auctions.get(auction_id)
        ui.button("All auctions", icon="arrow_back", on_click=lambda: ui.navigate.to("/")).props("flat no-caps")
        if not a or not agent:
            empty("Auction not found." if not a else "Create an agent first.")
            return
        mine = a["poster_agent_id"] == agent["agent_id"]
        reverse = a["direction"] == "REVERSE"

        @ui.refreshable
        def info():
            v = core().engine.get_auction_detail(auction_id, agent["agent_id"])
            with ui.card().classes("w-full"):
                with ui.row().classes("w-full items-center"):
                    ui.label(Verbatim(v["product_spec"]["title"])).classes("text-xl font-medium")
                    ui.space()
                    status_badge(v)
                ui.label(f"{v['product_spec']['category']} · qty {v['product_spec']['quantity']} · {pretty(v['auction_type'])}"
                         + (" · relisted" if v["relist_of"] else "")).classes("text-xs opacity-70")
                ui.label("Sealed bids" if v["current_price"] is None else kes(v["current_price"])).classes("text-3xl font-bold")
                bits = []
                if v["status"] in ("ACTIVE", "EXTENDING", "SCHEDULED"):
                    bits.append(left(v["extended_until"] or v["ends_at"]))
                if v["status"] == "EXTENDING":
                    bits.append("extended by a late bid")
                if v["min_next_bid"] is not None:
                    bits.append(f"next valid bid {kes(v['min_next_bid'])}")
                if v["max_next_bid"] is not None:
                    bits.append(f"next quote must be at most {kes(v['max_next_bid'])}")
                if v["poster_verified"] or v["verified_only"]:
                    with ui.row().classes("gap-1"):
                        if v["poster_verified"]:
                            badge("✓ verified business", "positive")
                        if v["verified_only"]:
                            badge("verified bidders only", "warning")
                if reverse:
                    ui.label(f"Request for quotes — the buyer will pay at most {kes(v['max_price'])}; the lowest quote wins.").classes("text-sm text-primary")
                ui.label(" · ".join(bits)).classes("text-sm opacity-70")
                if mine and "reserve_price" in v:
                    ui.label(f"Reserve {kes(v['reserve_price'])} (hidden from bidders)").classes("text-sm")
                if v["status"] == "SETTLED":
                    r = v["result"]
                    won = r["winner_agent_id"] == agent["agent_id"]
                    ui.label((f"{'Awarded' if reverse else 'Sold'} at {kes(r['price'])}" + (" — you won! See Matches." if won else "")) if r["outcome"] == "SOLD"
                             else ("Closed without any quote." if reverse else "Closed without a sale.")).classes("font-medium text-primary")
                    sv = core().insights.rfq_savings(core().store.auctions[auction_id])
                    if sv and mine:
                        ui.label(f"You saved {kes(sv['saved'])} ({sv['saved_pct']}%) against your maximum price.").classes("text-sm text-positive")
            with ui.card().classes("w-full"):
                ui.label(f"{'Quotes' if reverse else 'Bids'} ({v['bid_count']})").classes("font-medium")
                if v["bids"]:
                    rows = core().insights.comparison(v, reverse)
                    ui.table(columns=[{"name": "who", "label": "Supplier" if reverse else "Bidder", "field": "who", "align": "left"}, {"name": "amount", "label": "Amount", "field": "amount", "align": "left"},
                                      {"name": "track", "label": "Track record", "field": "track", "align": "left"},
                                      {"name": "at", "label": "Time", "field": "at", "align": "left"}, {"name": "after", "label": "Replied after", "field": "after", "align": "left"}],
                             rows=[{"id": i, "who": "You" if r["agent_id"] == agent["agent_id"] else f"agent {r['agent_id'][:6]}", "amount": kes(r["amount"]), "track": r["summary"],
                                    "at": fmt_time(r["at"]), "after": f"{r['after_s']}s" if r["after_s"] < 120 else f"{r['after_s'] // 60}m"} for i, r in enumerate(rows)],
                             row_key="id").classes("w-full").props("dense flat")
                else:
                    ui.label("Sealed bids stay hidden until the auction closes." if "SEALED" in v["auction_type"] and v["status"] != "SETTLED" else "No bids yet.").classes("opacity-70")
            return v
        v0 = info()
        ui.timer(1.0, info.refresh)

        if not mine and agent["agent_type"] == ("SELLER" if reverse else "BIDDER") and a["status"] in ("ACTIVE", "EXTENDING", "SCHEDULED"):
            with ui.card().classes("w-full"):
                ui.label("Quote" if reverse else "Bid").classes("font-medium")
                strat = agent["config"]["algorithms"].get(a["auction_type"], {}).get("strategy", "heuristic")

                @guard
                async def delegate():
                    r = await core().orchestrator.consider(agent["agent_id"], auction_id, manual=True)
                    msg = f"Agent ({r.get('strategy', '-')}): {r['reasoning']}" if r["status"] == "PLANNED" else f"Agent: {pretty(r['status'])} — {r.get('reason', '')}"
                    ui.notify(msg, type="positive" if r["status"] == "PLANNED" else "warning", multi_line=True)
                with ui.row().classes("items-center gap-3"):
                    ui.button("Let my agent quote for me" if reverse else "Let my agent bid for me", icon="smart_toy", on_click=delegate).props("unelevated color=primary")
                    ui.label(f"uses the {strat} algorithm for {pretty(a['auction_type'])} auctions").classes("text-xs opacity-70")
                ui.separator()
                amount = ui.number("Manual quote (KES)" if reverse else "Manual bid (KES)", value=(v0["max_next_bid"] if reverse else v0["min_next_bid"]) or 0, precision=0).classes("w-48")

                @guard
                def manual():
                    r = core().manual_bid(agent["agent_id"], auction_id, int(amount.value or 0))
                    ui.notify(("Quote placed" if reverse else "Bid placed") if r["ok"] else f"{r['code']}: {r['message']}", type="positive" if r["ok"] else "negative")
                ui.button("Quote manually" if reverse else "Bid manually", on_click=manual).props("outline")
                ui.label((f"Your agent's price floor is {kes(agent['constraints']['reserve_floor'])} — it will not quote below it." if reverse
                          else f"Your agent's hard ceiling is {kes(agent['constraints']['budget_ceiling'])}.") + " Manual bids still pass the guardrails.").classes("text-xs opacity-70")
        if not mine and a["status"] in ("SCHEDULED", "ACTIVE", "EXTENDING"):
            with ui.expansion("Report this listing", icon="flag").classes("w-full border rounded"):
                from ..moderation import REASONS
                f = {"reason": "FRAUD", "note": ""}
                with ui.column().classes("p-2 gap-2 w-full"):
                    ui.select(REASONS, value=f["reason"], label="Why?", on_change=lambda e: f.update(reason=e.value)).classes("w-72")
                    ui.input("Details (optional)", on_change=lambda e: f.update(note=e.value)).classes("w-full")

                    @guard
                    def send_report():
                        core().moderation.report(user["id"], auction_id, f["reason"], f["note"])
                        ui.notify("Thanks — moderators will review it", type="positive")
                    ui.button("Send report", icon="flag", on_click=send_report).props("outline no-caps color=negative")
        if mine and a["status"] in ("SETTLED", "CANCELLED"):
            with ui.row().classes("items-center gap-2"):
                pct = ui.number("Price change (%)", value=0, min=-90, max=500, precision=0).classes("w-40")

                @guard
                def again():
                    v2 = core().engine.repost(auction_id=auction_id, agent_id=agent["agent_id"], price_change_pct=float(pct.value or 0))
                    ui.notify("Posted again", type="positive")
                    ui.navigate.to(f"/auction/{v2['auction_id']}")
                ui.button("Run it again", icon="replay", on_click=again).props("outline color=primary no-caps")
        if mine and not a["bids"] and a["status"] in ("ACTIVE", "SCHEDULED"):
            @guard
            def withdraw():
                core().engine.withdraw_listing(listing_id=auction_id, seller_agent_id=agent["agent_id"], reason="withdrawn by poster")
                ui.notify("Listing withdrawn")
                ui.navigate.to("/")
            ui.button("Withdraw request" if reverse else "Withdraw listing", icon="delete", on_click=withdraw).props("outline color=negative")


# ----------------------------------------------------------------------------- inbox

def inbox_page():
    user = require_user()
    if not user:
        return
    with frame(user, "/inbox"):
        agent = active_agent(user)
        if not agent:
            empty("Create an agent first.")
            return
        agent_picker(user, lambda: ui.navigate.reload())

        @ui.refreshable
        def body():
            pending = sorted((a for a in core().store.approvals.values() if a["agent_id"] == agent["agent_id"] and a["status"] == "PENDING"), key=lambda a: -a["created_at"])
            ui.label("Needs your decision").classes("text-lg font-medium")
            if not pending:
                empty("Nothing waiting on you.")
            for ap in pending:
                with ui.card().classes("w-full"):
                    title = f"Bid of {kes(ap['proposal']['amount'])}" if ap["kind"] == "BID" else "Join an auction"
                    a = core().store.auctions.get(ap["auction_id"])
                    ui.label(f"{title} on \"{a['product_spec']['title']}\"" if a else title).classes("font-medium")
                    ui.label(ap["reason"]).classes("opacity-70 text-sm")

                    async def decide(approve: bool, i=ap["id"]):
                        await guard(core().orchestrator.resolve_approval)(i, approve)
                        body.refresh()
                    with ui.row():
                        ui.button("Approve", icon="check", on_click=lambda i=ap["id"]: decide(True, i)).props("unelevated color=primary")
                        ui.button("Reject", icon="close", on_click=lambda i=ap["id"]: decide(False, i)).props("outline color=negative")
            ui.label("Agent messages").classes("text-lg font-medium mt-2")
            notes = core().router.notifications_for(agent["agent_id"], 50)
            if not notes:
                empty("No messages yet.")
            else:
                with ui.card().classes("w-full"):
                    for n in notes:
                        with ui.row().classes("w-full items-baseline gap-2 no-wrap"):
                            ui.label(fmt_time(n["at"])).classes("text-xs opacity-60 w-16")
                            badge(n["kind"])
                            ui.label(n["message"]).classes("text-sm")
        body()
        ui.timer(2.0, body.refresh)


# ----------------------------------------------------------------------------- matches

STEPS = ["PROPOSED", "SELLER_CONFIRMED", "BUYER_CONFIRMED", "CONTACT_REVEALED"]


def matches_page():
    user = require_user()
    if not user:
        return
    with frame(user, "/matches"):
        agent = active_agent(user)
        if not agent:
            empty("Create an agent first.")
            return
        agent_picker(user, lambda: ui.navigate.reload())

        @ui.refreshable
        def body():
            ms = core().matches.matches_for(agent["agent_id"])
            if not ms:
                empty("No matches yet. When an auction closes with a winner, the match appears here.")
            for m in ms:
                side = "seller" if m["seller_agent_id"] == agent["agent_id"] else "buyer"
                reached = 3 if m["status"] in ("CONTACT_REVEALED", "COMPLETED", "FELL_THROUGH", "NO_RESPONSE", "DISPUTED") else STEPS.index(m["status"])
                with ui.card().classes("w-full"):
                    with ui.row().classes("w-full items-center"):
                        ui.label(f"{m['agreed_terms']['title']} — {kes(m['agreed_terms']['price'])}").classes("font-medium")
                        ui.space()
                        badge(pretty(m["status"]), {"COMPLETED": "positive", "FELL_THROUGH": "negative", "NO_RESPONSE": "negative", "DISPUTED": "warning"}.get(m["status"], "grey"))
                    with ui.row().classes("gap-1"):
                        for i, s in enumerate(["Proposed", "Seller confirmed", "Buyer confirmed", "Contact revealed"]):
                            ui.badge(s).props(f"{'color=primary' if i <= reached else 'color=grey-5'}")
                    ui.label("Delivery and payment happen directly between you and the counterparty — KenyaBidder never holds funds.").classes("text-xs opacity-70")
                    confirmed = m["confirmations"][f"{side}_at"]
                    if not confirmed and m["status"] in ("PROPOSED", "SELLER_CONFIRMED", "BUYER_CONFIRMED"):
                        @guard
                        def confirm(i=m["match_id"]):
                            core().matches.confirm_match(i, agent["agent_id"])
                            body.refresh()
                        ui.button("Confirm match", icon="check", on_click=confirm).props("unelevated color=primary")
                    cr = m["contact_reveal"]
                    if cr:
                        for k, label in (("seller_contact", "Seller"), ("buyer_contact", "Buyer")):
                            c = cr[k]
                            ui.label(f"{label}: {c['name']}" + (f" ✓ verified business ({c['verified_business']})" if c.get("verified_business") else "")
                                     + f" · {c['phone'] or 'no phone'} · {c['email'] or 'no email'}")
                    if m["status"] == "DISPUTED" and not core().trust.dispute_for_match(m["match_id"]):
                        ui.label("You and the other party reported different outcomes. Nobody's reputation is affected unless you open a dispute for our team to review.").classes("text-sm text-amber-700")
                    dispute_section(m, agent, body.refresh)
                    mine_reported = any(r["agent_id"] == agent["agent_id"] for r in m["outcome_reports"])
                    if m["status"] == "CONTACT_REVEALED" and mine_reported:
                        ui.label("Thanks — waiting for the other party to report their side. Nothing is decided until they do (or 72 hours pass).").classes("text-sm opacity-70")
                    elif m["status"] == "CONTACT_REVEALED" and m["outcome_reports"]:
                        ui.label("The other party has already reported. Please give your side below — you have 72 hours from their report.").classes("text-sm text-amber-700")
                    if m["status"] == "CONTACT_REVEALED" and not mine_reported:
                        with ui.row().classes("items-center"):
                            ui.label("How did it go?").classes("opacity-70")
                            for o in ("COMPLETED", "FELL_THROUGH", "NO_RESPONSE"):
                                @guard
                                def report(o=o, i=m["match_id"]):
                                    core().matches.report_outcome(i, agent["agent_id"], o)
                                    body.refresh()
                                ui.button(pretty(o), on_click=report).props("outline no-caps")
        body()
        ui.timer(2.0, body.refresh)


def dispute_section(m: dict, agent: dict, refresh) -> None:
    """Open / answer / follow a dispute for one match."""
    from ..trust import CATEGORIES, RULINGS
    t = core().trust
    d = t.dispute_for_match(m["match_id"])
    if not d or d["status"] == "WITHDRAWN":
        if d:
            ui.label("A dispute on this deal was withdrawn.").classes("text-xs opacity-70")
        if m.get("contact_reveal") and m["status"] in ("CONTACT_REVEALED", "COMPLETED", "FELL_THROUGH", "DISPUTED"):
            with ui.expansion("Something went wrong? Open a dispute", icon="gavel").classes("w-full border rounded"):
                f = {"cat": "NOT_DELIVERED", "text": ""}
                with ui.column().classes("w-full gap-2 p-2"):
                    ui.select(CATEGORIES, value=f["cat"], label="What happened?", on_change=lambda e: f.update(cat=e.value)).classes("w-full")
                    ui.textarea("Describe it (10-1000 characters). The other party will see this.", on_change=lambda e: f.update(text=e.value)).classes("w-full").props("rows=3")

                    @guard
                    def open_it(i=m["match_id"]):
                        t.open_dispute(agent["agent_id"], i, f["cat"], f["text"])
                        ui.notify("Dispute opened — the other party has been asked to respond", type="positive")
                        refresh()
                    ui.button("Open dispute", icon="gavel", on_click=open_it).props("unelevated color=negative no-caps")
        return
    mine_opened = d["opened_by"] == agent["agent_id"]
    with ui.card().classes("w-full bg-amber-50 dark:bg-transparent border"):
        with ui.row().classes("items-center gap-2"):
            ui.label("Dispute").classes("font-medium")
            badge(d["status"].lower(), {"RULED": "positive", "WITHDRAWN": "grey"}.get(d["status"], "warning"))
            ui.label(CATEGORIES[d["category"]]).classes("text-xs opacity-70")
        for s in d["statements"]:
            who = "You" if s["agent_id"] == agent["agent_id"] else "Other party"
            ui.label(f"{who} ({fmt_time(s['at'])}): {s['text']}").classes("text-sm")
        if d["ruling"]:
            ui.label(f"Ruling: {RULINGS[d['ruling']['outcome']]} — {d['ruling']['note']}").classes("text-sm font-medium text-primary")
        elif d["status"] in ("OPEN", "RESPONDED"):
            if not mine_opened and d["status"] == "OPEN":
                ui.label(f"Please answer by {fmt_time(d['respond_by'])}. An administrator will review both sides.").classes("text-sm text-amber-700")
            mine_n = sum(1 for s in d["statements"] if s["agent_id"] == agent["agent_id"])
            if mine_n < 3:
                box = ui.input("Add your statement").classes("w-full")

                @guard
                def add(i=d["id"]):
                    t.add_statement(agent["agent_id"], i, box.value)
                    refresh()
                ui.button("Send", on_click=add).props("outline no-caps dense")
            if mine_opened:
                @guard
                def withdraw(i=d["id"]):
                    t.withdraw(agent["agent_id"], i)
                    refresh()
                ui.button("Withdraw dispute", on_click=withdraw).props("flat no-caps dense color=negative")


# ----------------------------------------------------------------------------- activity

def activity_page():
    user = require_user()
    if not user:
        return
    with frame(user, "/activity"):
        agent = active_agent(user)
        if not agent:
            empty("Create an agent first.")
            return
        agent_picker(user, lambda: ui.navigate.reload())

        opened: set[str] = set()  # the list re-renders on a timer; remember which plans the user expanded

        def remember(e, tid):
            opened.add(tid) if e.value else opened.discard(tid)

        @ui.refreshable
        def body():
            ui.label("Standing plans").classes("text-lg font-medium")
            trig = sorted(core().execution.triggers_for(agent["agent_id"]), key=lambda t: -t["created_at"])
            if not trig:
                empty("No plans yet.")
            for t in trig:
                a = core().store.auctions.get(t["auction_id"])
                with ui.expansion(f"{a['product_spec']['title'] if a else t['auction_id'][:6]} · {pretty(t['kind'])} · {pretty(t['status'])}"
                                  + (f" · {t['outcome'].lower()}" if t.get("outcome") else ""),
                                  value=t["id"] in opened, on_value_change=lambda e, tid=t["id"]: remember(e, tid)).classes("w-full border rounded"):
                    ui.label(f"Algorithm: {t.get('strategy', '—')} · params {json.dumps(t['params'])}").classes("text-sm")
                    ui.label(t["reasoning"]).classes("text-sm opacity-80")
                    if t.get("trace"):
                        ui.label("LLM steps: " + " → ".join(f"[{', '.join(s['calls']) or 'answer'}]" for s in t["trace"])).classes("text-xs opacity-60")
            ui.label("Audit log").classes("text-lg font-medium mt-2")
            rows = core().audit.for_agent(agent["agent_id"], 100)
            if not rows:
                empty("No agent actions yet.")
            else:
                ui.table(columns=[{"name": k, "label": l, "field": k, "align": "left"} for k, l in (("time", "Time"), ("proposed", "Proposed"), ("guardrail", "Guardrail"), ("result", "Result"))],
                         rows=[{"id": e["entry_id"], "time": fmt_time(e["timestamp"]),
                                "proposed": f"{e['proposed_action']['action']} {kes(e['proposed_action'].get('amount'))}",
                                "guardrail": e["guardrail_decision"].lower() + (f" — {e['rejection_reason']}" if e["rejection_reason"] else ""),
                                "result": (f"{e['execution_result']['code']}" + (f" · {e['execution_result']['latency_ms']}ms" if e["execution_result"].get("latency_ms") is not None else "")) if e["execution_result"] else "—"}
                               for e in rows], row_key="id").classes("w-full").props("dense flat")
        body()
        ui.timer(3.0, body.refresh)


# ----------------------------------------------------------------------------- platform

def platform_page():
    user = require_user()
    if not user:
        return
    with frame(user, "/platform"):
        ui.label("Platform health").classes("text-lg font-medium")

        @ui.refreshable
        def tiles():
            k = core().kpis()
            pct = lambda v: "—" if v is None else f"{round(v * 100)}%"
            data = [("Auctions settled", k["agent_performance"]["auctions_settled"]), ("Sell-through", pct(k["agent_performance"]["sell_through_rate"])),
                    ("Bid latency p95", "—" if k["agent_performance"]["bid_latency_ms"]["p95"] is None else f"{k['agent_performance']['bid_latency_ms']['p95']} ms"),
                    ("Guardrail intercept rate", pct(k["system_reliability"]["guardrail_intercept_rate"])), ("Matches", k["matching"]["matches"]),
                    ("Match confirmation", pct(k["matching"]["match_confirmation_rate"]))]
            with ui.grid().classes("w-full gap-3 grid-cols-2 lg:grid-cols-3"):
                for label, val in data:
                    with ui.card():
                        ui.label(label).classes("text-xs opacity-70")
                        ui.label(str(val)).classes("text-2xl font-bold")
        tiles()
        ui.timer(3.0, tiles.refresh)
