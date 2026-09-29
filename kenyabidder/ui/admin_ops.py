"""Administrator panels for the operational side: messaging, trust & safety, plans, privacy requests, health & backups."""
from __future__ import annotations

from nicegui import ui

from .common import badge, core, empty, fmt_datetime, guard_admin


def messaging_panel() -> None:
    c = core()
    m = c.messenger
    ui.label("Codes for phone verification and password reset, and SMS/email alerts, go through these gateways.").classes("text-sm opacity-70")
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center gap-2"):
            ui.label("SMS gateway").classes("font-medium")
            badge(m.sms.name, "positive" if m.sms_real else "warning")
            ui.label("" if m.sms_real else "Development mode — messages are not sent. Set AT_USERNAME and AT_API_KEY (Africa's Talking) to go live.").classes("text-xs opacity-70")
        with ui.row().classes("items-center gap-2"):
            ui.label("Email").classes("font-medium")
            badge(m.email.name, "positive" if m.email_real else "warning")
            ui.label("" if m.email_real else "Development mode. Set SMTP_HOST / SMTP_USER / SMTP_PASSWORD / SMTP_FROM to go live.").classes("text-xs opacity-70")
        opts = {"auto": "Automatic (required once a real SMS gateway is configured)", "yes": "Always required", "no": "Optional"}
        cur = c.store.settings.get("require_verified_phone")
        sel = ui.select(opts, value="auto" if cur is None else ("yes" if cur else "no"), label="Verified phone required for the free trial and for contact exchange").classes("w-full max-w-xl")
        budget = ui.number("Daily SMS budget (platform-wide)", value=c.store.settings.get("sms_daily_budget", m.DEFAULT_SMS_BUDGET), min=0, precision=0).classes("w-64")
        used = (c.store.settings.get("sms_day") or {}).get("n", 0)
        ui.label(f"{used} SMS sent today (EAT). The budget stops SMS-pumping fraud from running up your bill.").classes("text-xs opacity-70")

        @guard_admin
        def save():
            c.verification.set_phone_required({"auto": None, "yes": True, "no": False}[sel.value])
            c.store.settings["sms_daily_budget"] = int(budget.value or 0)
            ui.notify("Saved", type="positive")
        ui.button("Save", icon="save", on_click=save).props("unelevated color=primary")
    with ui.card().classes("w-full"):
        ui.label("Recent messages (destinations masked, contents never stored)").classes("font-medium")
        rows = list(reversed(c.store.message_log[-50:]))
        if not rows:
            empty("Nothing sent yet.")
        else:
            ui.table(columns=[{"name": k, "label": l, "field": k, "align": "left"} for k, l in (("at", "Time (EAT)"), ("channel", "Channel"), ("to", "To"), ("kind", "Kind"), ("status", "Status"))],
                     rows=[{"id": i, "at": fmt_datetime(r["at"]), "channel": r["channel"], "to": r["to"], "kind": r["kind"], "status": "sent" if r["ok"] else f"failed: {r.get('error') or ''}"}
                           for i, r in enumerate(rows)], row_key="id").classes("w-full").props("dense flat")
