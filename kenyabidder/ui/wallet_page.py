"""Wallet (balances, buy tokens, orders, usage), Profile, and the public Terms page."""
from __future__ import annotations

from nicegui import ui

from ..terms import get_terms
from .common import (active_agent, badge, core, empty, fmt_datetime, frame, guard, kes, my_agents, pretty, require_user, theme)

STATUS_COLOR = {"PAID": "positive", "PENDING": "warning", "AWAITING_REVIEW": "warning", "FAILED": "negative", "REJECTED": "negative",
                "CANCELLED": "grey", "EXPIRED": "grey", "REFUNDED": "grey"}


def register() -> None:
    ui.page("/wallet")(wallet_page)
    ui.page("/profile")(profile_page)
    ui.page("/terms")(terms_page)


def terms_page():
    theme()
    with ui.column().classes("max-w-3xl mx-auto p-6 gap-2"):
        ui.label("Terms & Privacy Notice").classes("text-2xl font-semibold")
        for para in get_terms(core().store).split("\n\n"):
            ui.label(para).classes("whitespace-pre-line")
        ui.link("← Back", "/login")


# ----------------------------------------------------------------------------- wallet

def wallet_page():
    user = require_user()
    if not user:
        return
    c = core()
    with frame(user, "/wallet"):
        ui.label("Wallet").classes("text-lg font-medium")
        ui.label("Your agents think with LLMs. Each decision uses tokens of the model it runs on — buy them here. "
                 "Agents that only use the baseline/heuristic algorithms never need tokens.").classes("text-sm opacity-70")

        grants = c.billing.signup_grants()
        if grants and not user.get("phone"):
            with ui.card().classes("w-full border border-primary"):
                ui.label("Claim your free trial tokens").classes("font-medium")
                ui.label("Add your phone number (one free trial per number) — it is also where M-Pesa prompts go.").classes("text-sm opacity-70")
                ph = ui.input("Phone", placeholder="0712 345 678").classes("w-64")

                @guard
                def claim():
                    c.agents.set_phone(user["id"], ph.value)
                    ui.notify("Phone saved — free tokens added if you qualify", type="positive")
                    ui.navigate.reload()
                ui.button("Claim", icon="redeem", on_click=claim).props("unelevated color=primary")

        # ---- balances
        @ui.refreshable
        def balances():
            metered = [e for e in c.llms.list(enabled_only=True) if c.meter.is_metered(e)]
            if not metered:
                empty("No metered LLMs are configured — nothing to buy right now.")
                return
            bal = c.wallet.balances(user["id"])
            with ui.grid().classes("w-full gap-3 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3"):
                for e in metered:
                    b, need = bal.get(e["id"], 0), c.meter.min_balance(e)
                    users = [a for a in my_agents(user) if e["id"] in c.meter.llm_ids_for(a)]
                    with ui.card():
                        ui.label(e["name"]).classes("font-medium")
                        ui.label(f"{b:,}").classes("text-3xl font-bold " + ("text-negative" if b < need and users else ""))
                        ui.label("tokens").classes("text-xs opacity-60 -mt-2")
                        ui.label(f"An agent needs at least {need:,} to make a decision.").classes("text-xs opacity-70")
                        if users:
                            ui.label("Used by: " + ", ".join(f"{a['agent_type'].title()} {a['agent_id'][:6]}" for a in users)).classes("text-xs opacity-70")
                        if b < need and users:
                            badge("your agents are waiting for tokens", "negative")
        balances()

        # ---- buy
        ui.label("Buy tokens").classes("text-lg font-medium mt-2")
        methods = c.billing.methods()
        packs = c.billing.list_packs(enabled_only=True)
        if not packs:
            empty("No token packs are on sale yet.")
        elif not methods:
            ui.label("Online payment is not set up yet — please contact the administrator.").classes("text-amber-700")
        with ui.grid().classes("w-full gap-3 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3"):
            for p in packs:
                llm = c.store.llms[p["llm_id"]]
                with ui.card():
                    ui.label(p["name"]).classes("font-medium")
                    ui.label(f"{p['tokens']:,} tokens").classes("text-xl font-bold")
                    ui.label(f"for {llm['name']} · KES {p['price_kes']:,} · ≈ KES {p['price_kes'] / p['tokens'] * 1000:.2f} per 1k").classes("text-xs opacity-70")
                    if p["description"]:
                        ui.label(p["description"]).classes("text-sm")
                    if methods:
                        ui.button("Buy", icon="shopping_cart", on_click=lambda pk=p: buy_dialog(user, pk, methods, orders.refresh)).props("unelevated color=primary")

        # ---- orders
        ui.label("My orders").classes("text-lg font-medium mt-2")

        @ui.refreshable
        def orders():
            rows = c.billing.list_orders(user_id=user["id"], limit=20)
            if not rows:
                empty("No orders yet.")
            for o in rows:
                with ui.card().classes("w-full"):
                    with ui.row().classes("w-full items-center gap-2"):
                        ui.label(f"{o['reference']} · {o['pack_name']} · {o['tokens']:,} tokens · {kes(o['amount_kes'])}").classes("font-medium")
                        badge(pretty(o["status"]), STATUS_COLOR.get(o["status"], "grey"))
                        ui.space()
                        ui.label(fmt_datetime(o["created_at"])).classes("text-xs opacity-60")
                    if o["note"]:
                        ui.label(o["note"]).classes("text-xs opacity-70")
                    order_actions(user, o, orders.refresh, balances.refresh)
        orders()

        def tick():
            balances.refresh()
            orders.refresh()
        ui.timer(4.0, tick)

        # ---- usage & history
        ui.label("Where your tokens go (last 30 days)").classes("text-lg font-medium mt-2")
        usage = c.wallet.usage_by_agent(user["id"], c.clock.now() - 30 * 24 * 3600_000)
        if not usage:
            empty("No usage yet.")
        else:
            ui.table(columns=[{"name": k, "label": l, "field": k, "align": "left"} for k, l in (("agent", "Agent"), ("llm", "LLM"), ("calls", "LLM calls"), ("used", "Tokens used"))],
                     rows=[{"id": i, "agent": (f"{c.store.agents[u['agent_id']]['agent_type'].title()} {u['agent_id'][:6]}" if u["agent_id"] in c.store.agents else "(removed)"),
                            "llm": c.store.llms.get(u["llm_id"], {}).get("name", "?"), "calls": u["calls"], "used": f"{u['used']:,}"} for i, u in enumerate(usage)],
                     row_key="id").classes("w-full").props("dense flat")

        ui.label("Ledger").classes("text-lg font-medium mt-2")
        led = c.wallet.ledger(user_id=user["id"], limit=50)
        if not led:
            empty("Nothing yet.")
        else:
            ui.table(columns=[{"name": k, "label": l, "field": k, "align": "left"} for k, l in (("at", "Time (EAT)"), ("kind", "Type"), ("llm", "LLM"), ("tokens", "Tokens"), ("bal", "Balance"), ("note", "Note"))],
                     rows=[{"id": e["entry_id"], "at": fmt_datetime(e["at"]), "kind": e["kind"].lower(), "llm": c.store.llms.get(e["llm_id"], {}).get("name", "?"),
                            "tokens": f"{e['tokens']:+,}" if e["tokens"] else f"used {e['used']:,} (free)", "bal": f"{e['balance_after']:,}",
                            "note": e["meta"].get("reason") or e["meta"].get("purpose") or e["meta"].get("order") or ""} for e in led], row_key="id").classes("w-full").props("dense flat")
        with ui.row():
            ui.button("Download ledger (CSV)", icon="download", on_click=lambda: ui.download.content(c.billing.ledger_csv(user_id=user["id"]), "kenyabidder-ledger.csv", "text/csv")).props("outline no-caps")
            ui.button("Download orders (CSV)", icon="download", on_click=lambda: ui.download.content(c.billing.orders_csv(user_id=user["id"]), "kenyabidder-orders.csv", "text/csv")).props("outline no-caps")


def order_actions(user: dict, o: dict, refresh_orders, refresh_balances) -> None:
    c = core()

    @guard
    def cancel(i=o["id"]):
        c.billing.cancel_order(user, i)
        refresh_orders()

    @guard
    async def check(i=o["id"]):
        r = await c.billing.check_order(user, i)
        ui.notify("Payment received!" if r["status"] == "PAID" else "Still waiting for your M-Pesa confirmation…", type="positive" if r["status"] == "PAID" else "info")
        refresh_orders()
        refresh_balances()

    if o["status"] == "PENDING" and o["provider"] == "manual":
        ui.label(c.billing.manual_instructions()).classes("text-sm font-medium whitespace-pre-line")
        ui.label(f"Use {o['reference']} as the account/reference, then enter the M-Pesa confirmation code from your SMS:").classes("text-xs opacity-70")
        code = ui.input("M-Pesa code", placeholder="SGH7X2K9LP").props("dense").classes("w-56")

        @guard
        def submit(i=o["id"]):
            c.billing.submit_receipt(user, i, code.value)
            ui.notify("Thanks — we will verify your payment shortly", type="positive")
            refresh_orders()
        with ui.row():
            ui.button("Submit code", on_click=submit).props("unelevated color=primary dense")
            ui.button("Cancel", on_click=cancel).props("outline dense color=negative")
    elif o["status"] == "PENDING" and o["provider"] == "mpesa":
        ui.label("Check your phone and enter your M-Pesa PIN to approve the payment.").classes("text-sm")
        with ui.row():
            ui.button("I have paid — check", icon="refresh", on_click=check).props("unelevated color=primary dense")
            ui.button("Cancel", on_click=cancel).props("outline dense color=negative")
    elif o["status"] == "AWAITING_REVIEW":
        ui.label(f"Code {o['receipt']} submitted — waiting for verification.").classes("text-sm")


def buy_dialog(user: dict, pack: dict, methods: list[dict], done) -> None:
    c = core()
    st = {"method": methods[0]["id"], "phone": user.get("phone") or ""}
    llm = c.store.llms[pack["llm_id"]]
    with ui.dialog() as d, ui.card().classes("w-96 max-w-full"):
        ui.label(f"Buy {pack['tokens']:,} {llm['name']} tokens").classes("text-lg font-medium")
        ui.label(f"Price: {kes(pack['price_kes'])}").classes("text-sm")
        ui.radio({m["id"]: m["label"] for m in methods}, value=st["method"], on_change=lambda e: (st.update(method=e.value), phone_box.refresh()))

        @ui.refreshable
        def phone_box():
            if st["method"] == "mpesa":
                ui.input("M-Pesa phone number", value=st["phone"], placeholder="0712 345 678", on_change=lambda e: st.update(phone=e.value)).classes("w-full")
                ui.label("You will get a prompt on this phone to enter your M-Pesa PIN.").classes("text-xs opacity-70")
            elif st["method"] == "dev":
                ui.label("Development mode: this credits the tokens instantly without any payment.").classes("text-xs text-amber-700")
        phone_box()

        @guard
        async def go():
            o = await c.billing.checkout(user, pack["id"], st["method"], st["phone"])
            d.close()
            ui.notify({"PAID": "Tokens added to your wallet!", "PENDING": "Order created — follow the steps under My orders"}.get(o["status"], "Order created"), type="positive")
            done()
        with ui.row().classes("justify-end w-full"):
            ui.button("Cancel", on_click=d.close).props("flat")
            ui.button("Continue", icon="arrow_forward", on_click=go).props("unelevated color=primary")
    d.open()


# ----------------------------------------------------------------------------- profile

def profile_page():
    user = require_user()
    if not user:
        return
    c = core()
    with frame(user, "/profile"):
        ui.label("My profile").classes("text-lg font-medium")
        with ui.card().classes("w-full max-w-xl"):
            ui.label("Contact details").classes("font-medium")
            ui.label("Shared with a counterparty only after BOTH of you confirm a match.").classes("text-xs opacity-70")
            ph = ui.input("Phone", value=user.get("phone") or "", placeholder="0712 345 678").classes("w-full")
            em = ui.input("Email", value=user.get("email") or "").classes("w-full")

            @guard
            def save():
                c.agents.set_phone(user["id"], ph.value)
                c.agents.set_email(user["id"], em.value)
                ui.notify("Saved", type="positive")
            ui.button("Save", icon="save", on_click=save).props("unelevated color=primary")
        with ui.card().classes("w-full max-w-xl"):
            ui.label("Change password").classes("font-medium")
            old = ui.input("Current password", password=True, password_toggle_button=True).classes("w-full")
            new = ui.input("New password (8+ characters)", password=True, password_toggle_button=True).classes("w-full")

            @guard
            def change():
                c.agents.change_password(user["id"], old.value, new.value)
                old.set_value("")
                new.set_value("")
                ui.notify("Password changed", type="positive")
            ui.button("Change password", on_click=change).props("outline")
        ui.label(f"Accepted the Terms & Privacy Notice (version {user.get('terms_version', '-')}). ").classes("text-xs opacity-60")
        ui.link("Read the terms", "/terms", new_tab=True).classes("text-xs")
