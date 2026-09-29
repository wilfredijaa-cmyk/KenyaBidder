"""Administrator panels for the operational side: messaging, trust & safety, plans, privacy requests, health & backups."""
from __future__ import annotations

from nicegui import ui

from .common import admin_user, badge, core, empty, fmt_datetime, guard_admin


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


def _ask(title: str, label: str, on_ok, ok_label: str = "Confirm", options: dict | None = None, required: bool = True) -> None:
    """Small modal: an optional choice plus a note."""
    with ui.dialog() as d, ui.card().classes("w-96 max-w-full"):
        ui.label(title).classes("font-medium")
        pick = ui.select(options, value=next(iter(options)), label="Decision").classes("w-full") if options else None
        note = ui.textarea(label).classes("w-full").props("rows=3")

        @guard_admin
        def go():
            if required and not (note.value or "").strip():
                ui.notify("A note is required", type="negative")
                return
            on_ok(pick.value if pick else None, note.value or "")
            d.close()
        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=d.close).props("flat no-caps")
            ui.button(ok_label, on_click=go).props("unelevated color=primary no-caps")
    d.open()


def trust_panel() -> None:
    from ..trust import CATEGORIES, KINDS, RULINGS
    c = core()

    @ui.refreshable
    def body():
        with ui.card().classes("w-full"):
            ui.label("Verification policy").classes("font-medium")
            thr = ui.number("Require a verified business for any bid or quote above (KES) — 0 = never", value=c.store.settings.get("verification_required_above", 0), min=0, precision=0).classes("w-full max-w-xl")

            @guard_admin
            def save():
                c.store.settings["verification_required_above"] = int(thr.value or 0)
                ui.notify("Saved", type="positive")
            ui.button("Save", icon="save", on_click=save).props("unelevated color=primary")

        ui.label(f"Applications waiting ({len(c.trust.pending_applications())})").classes("text-lg font-medium mt-2")
        pend = c.trust.pending_applications()
        if not pend:
            empty("No applications waiting.")
        for b in pend:
            u = c.store.users.get(b["user_id"], {"name": "?"})
            with ui.card().classes("w-full"):
                ui.label(f"{b['business_name']} — applicant {u['name']}").classes("font-medium")
                ui.label(f"{KINDS[b['kind']]}: {b['registration_no']} · submitted {fmt_datetime(b['submitted_at'])}").classes("text-sm")
                if b["notes"]:
                    ui.label(f"Applicant notes: {b['notes']}").classes("text-xs opacity-70")
                ui.label("Check the number against iTax / the Business Registration Service before approving — the platform cannot do that for you.").classes("text-xs opacity-60")

                def decide(kind, note, b=b):
                    c.trust.review(admin_user(), b["id"], kind == "approve", note)
                    c.billing.log_admin(admin_user(), "verification_" + kind, user=u["name"], business=b["business_name"])
                    body.refresh()
                with ui.row():
                    ui.button("Approve", icon="verified", on_click=lambda b=b: _ask("Approve verification", "Note (optional)", lambda k, n, b=b: decide("approve", n, b), "Approve", required=False)).props("unelevated dense no-caps color=primary")
                    ui.button("Reject", icon="close", on_click=lambda b=b: _ask("Reject verification", "Reason (the applicant sees it)", lambda k, n, b=b: decide("reject", n, b), "Reject")).props("outline dense no-caps color=negative")

        ui.label("Verified businesses").classes("text-lg font-medium mt-2")
        vs = [u for u in c.store.users.values() if u.get("verified_business")]
        if not vs:
            empty("None yet.")
        for u in vs:
            with ui.row().classes("w-full items-center gap-2"):
                badge("✓ verified", "positive")
                ui.label(f"{u['name']} — {u['verified_business']['name']} ({KINDS[u['verified_business']['kind']]})").classes("grow")

                def revoke(k, note, u=u):
                    c.trust.revoke(admin_user(), u["id"], note)
                    c.billing.log_admin(admin_user(), "verification_revoke", user=u["name"], reason=note)
                    body.refresh()
                ui.button("Revoke", on_click=lambda u=u: _ask("Revoke badge", "Reason", lambda k, n, u=u: revoke(k, n, u), "Revoke")).props("outline dense no-caps color=negative")

        ui.label(f"Open disputes ({len(c.trust.open_disputes())})").classes("text-lg font-medium mt-2")
        ds = c.trust.open_disputes()
        if not ds:
            empty("No open disputes.")
        for d in ds:
            m = c.store.matches.get(d["match_id"], {})
            name = lambda aid: c.store.users.get((c.store.agents.get(aid) or {}).get("principal_user_id"), {}).get("name", "?")  # noqa: E731
            with ui.card().classes("w-full"):
                ui.label(f"{m.get('agreed_terms', {}).get('title', '?')} — {CATEGORIES[d['category']]}").classes("font-medium")
                ui.label(f"Opened by {name(d['opened_by'])} against {name(d['against'])} · {fmt_datetime(d['opened_at'])} · {d['status'].lower()}").classes("text-xs opacity-70")
                for s in d["statements"]:
                    ui.label(f"{name(s['agent_id'])}: {s['text']}").classes("text-sm")

                def rule(outcome, note, d=d):
                    c.trust.rule(admin_user(), d["id"], outcome, note)
                    c.billing.log_admin(admin_user(), "dispute_ruling", dispute=d["id"][:8], outcome=outcome)
                    body.refresh()
                ui.button("Rule on this dispute", icon="gavel", on_click=lambda d=d: _ask("Rule on dispute", "Explanation (both parties see it)", lambda k, n, d=d: rule(k, n, d), "Issue ruling", options=RULINGS)).props("unelevated dense no-caps color=primary")
    body()



def ops_panel() -> None:
    """Health, database and backups."""
    import asyncio
    from .. import metrics
    from .common import rt
    r = rt()
    c = core()

    @ui.refreshable
    def body():
        ok, checks = metrics.readiness(r)
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center gap-2"):
                ui.label("Health").classes("font-medium")
                badge("ready" if ok else "NOT READY", "positive" if ok else "negative")
            for k, v in checks.items():
                ui.label(f"{'✓' if v else '✗'} {k.replace('_', ' ')}").classes("text-sm " + ("" if v else "text-negative"))
            problems = c.wallet.verify_integrity()
            ui.label("✓ token ledger matches balances" if not problems else "✗ LEDGER PROBLEMS: " + "; ".join(problems[:3])).classes("text-sm " + ("" if not problems else "text-negative"))
            ui.label(f"Database: {r.db.dialect} · {'durable state on' if r.persistence else 'in-memory (data is lost on restart)'} · "
                     "scrape /metrics (set KENYABIDDER_METRICS_TOKEN) and alert on /readyz.").classes("text-xs opacity-70")
        with ui.card().classes("w-full"):
            ui.label("Backups").classes("font-medium")
            if not r.backups:
                ui.label("Automatic backups need a durable DuckDB database (default when running with a data directory). "
                         "For PostgreSQL use your provider's snapshots or pg_dump.").classes("text-sm opacity-70")
                return
            b = r.backups
            ui.label(f"Every {b.every_ms // 3_600_000}h, keeping the newest {b.keep}. Stored in {b.dir}. Copy them off this machine!").classes("text-sm opacity-70")
            if b.last_error:
                ui.label(f"Last attempt failed: {b.last_error}").classes("text-sm text-negative")

            @guard_admin
            async def run():
                path = await asyncio.to_thread(b.run)
                ui.notify(f"Backup written: {path.name}", type="positive")
                body.refresh()
            ui.button("Back up now", icon="backup", on_click=run).props("unelevated color=primary")
            files = b.list()
            if not files:
                empty("No backups yet.")
            else:
                ui.table(columns=[{"name": k, "label": l, "field": k, "align": "left"} for k, l in (("name", "File"), ("when", "Taken"), ("mb", "Size"))],
                         rows=[{"id": f["name"], "name": f["name"], "when": f["when"], "mb": f"{f['bytes'] / 1_048_576:.2f} MB"} for f in files], row_key="id").classes("w-full").props("dense flat")
            ui.label("Restore (with the app stopped): python -m kenyabidder restore <backup file> — the current database is kept as .before-restore.").classes("text-xs opacity-60")
    body()
