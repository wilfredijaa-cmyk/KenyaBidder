"""Shared UI plumbing: auth, page frame, notification push, formatting."""
from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from nicegui import app as ng_app
from nicegui import context, ui

from .. import i18n
from ..errors import AppError
from ..i18n import LANGS
from ..timeutil import EAT

_rt = None


def bind(runtime) -> None:
    global _rt
    _rt = runtime


def rt():
    return _rt


def core():
    return _rt.app


def lang() -> str:
    return ng_app.storage.user.get("lang", "en")


def set_lang(code: str) -> None:
    ng_app.storage.user["lang"] = code if code in LANGS else "en"
    u = core().store.users.get(ng_app.storage.user.get("uid") or "")
    if u:
        u["lang"] = ng_app.storage.user["lang"]  # remembered for SMS/email too
    ui.navigate.reload()


def lang_toggle() -> None:
    ui.toggle({"en": "EN", "sw": "SW"}, value=lang(), on_change=lambda e: set_lang(e.value)).props("dense no-caps unelevated").tooltip("Language / Lugha")


def theme() -> None:
    """Brand colours + follow the OS light/dark preference. Called by every page (login included)."""
    i18n.install(ui, lang)
    ui.colors(primary="#0b7a4b", positive="#067647", negative="#b42318", warning="#b54708")
    ui.dark_mode(None)


# ---------- formatting ----------

def kes(n) -> str:
    return "—" if n is None else f"KES {int(n):,}"


def fmt_time(ms: int | None) -> str:
    """Wall-clock time in East Africa Time regardless of the server's timezone."""
    return "" if not ms else datetime.fromtimestamp(ms / 1000, EAT).strftime("%H:%M:%S")


def fmt_datetime(ms: int | None) -> str:
    return "" if not ms else datetime.fromtimestamp(ms / 1000, EAT).strftime("%d %b %Y %H:%M")


def left(end_ms: int) -> str:
    ms = end_ms - int(time.time() * 1000)
    if ms <= 0:
        return "closing…"
    if ms < 90_000:
        return f"{-(-ms // 1000)}s left"
    if ms < 5_400_000:
        return f"{round(ms / 60000)}m left"
    return f"{round(ms / 3_600_000)}h left"


def pretty(s: str) -> str:
    return s.replace("_", " ").lower()


# ---------- auth ----------

def current_user() -> dict | None:
    uid = ng_app.storage.user.get("uid")
    u = core().store.users.get(uid) if uid else None
    if u and ng_app.storage.user.get("sv", 0) != u.get("session_version", 0):
        return None  # the password was changed/reset since this browser signed in: every other session ends
    return None if (u and u.get("suspended")) else u  # a suspended account is signed out on its next page load


def login_user(user: dict) -> None:
    ng_app.storage.user["uid"] = user["id"]
    ng_app.storage.user["sv"] = user.get("session_version", 0)


def logout() -> None:
    ng_app.storage.user.clear()
    ui.navigate.to("/login")


def require_user() -> dict | None:
    u = current_user()
    if not u:
        ui.navigate.to("/login")
    return u


def guard_admin(fn):
    """Like guard, but re-checks the *current* role at click time (a demoted admin's open page must stop working)."""
    import functools

    @functools.wraps(fn)
    async def wrapper(*a, **kw):
        u = current_user()
        if not u or u["role"] != "admin":
            ui.notify("Administrators only", type="negative")
            return None
        return await guard(fn)(*a, **kw)
    return wrapper


def admin_user() -> dict:
    return current_user()


def token_chip_state(user: dict) -> tuple[str, str, str]:
    """(label, color, tooltip) for the header chip; empty label when no metered LLM exists."""
    c = core()
    metered = [e for e in c.llms.list(enabled_only=True) if c.meter.is_metered(e)]
    if not metered:
        return "", "grey", ""
    bal = c.wallet.balances(user["id"])
    total = sum(bal.get(e["id"], 0) for e in metered)
    agent = active_agent(user)
    ok = c.meter.status_for_agent(agent)["ok"] if agent else True
    txt = f"{total / 1000:.1f}k" if total >= 1000 else str(total)
    tip = " · ".join(f"{e['name']}: {bal.get(e['id'], 0):,}" for e in metered)
    return f"{txt} tokens", ("primary" if ok else "negative"), tip + ("" if ok else " — your agent is waiting for tokens")


def flash(message: str, kind: str = "info") -> None:
    """Queue a toast that survives the next navigation/reload (a plain ui.notify is lost when the page reloads)."""
    ng_app.storage.user["flash"] = {"message": message, "type": kind}


def require_admin() -> dict | None:
    u = require_user()
    if u and u["role"] != "admin":
        flash("Administrators only", "negative")
        ui.navigate.to("/")
        return None
    return u


def my_agents(user: dict) -> list[dict]:
    return core().agents.agents_for(user["id"])


def active_agent(user: dict) -> dict | None:
    agents = my_agents(user)
    want = ng_app.storage.user.get("agent_id")
    return next((a for a in agents if a["agent_id"] == want), agents[0] if agents else None)


def set_active_agent(agent_id: str) -> None:
    ng_app.storage.user["agent_id"] = agent_id


def guard(fn):
    """Run a UI action; show domain errors as toasts instead of crashing the handler."""
    import functools
    import inspect

    @functools.wraps(fn)
    async def wrapper(*a, **kw):
        try:
            r = fn(*a, **kw)
            return await r if inspect.isawaitable(r) else r
        except AppError as e:
            ui.notify(e.message, type="negative", multi_line=True)
        except Exception as e:  # noqa: BLE001
            ui.notify(f"Unexpected error: {e}", type="negative", multi_line=True)
    return wrapper


# ---------- frame ----------

NAV = [("/", "Auctions", "gavel"), ("/agents", "My agents", "smart_toy"), ("/wallet", "Wallet", "bolt"), ("/inbox", "Inbox", "inbox"),
       ("/matches", "Matches", "handshake"), ("/activity", "Activity", "receipt_long"), ("/platform", "Platform", "bar_chart")]


@contextmanager
def frame(user: dict, active: str):
    theme()
    client = context.client
    uid = user["id"]

    def on_notify(n: dict) -> None:
        agent = core().store.agents.get(n["agent_id"])
        if not agent or agent["principal_user_id"] != uid:
            return
        try:
            with client:
                ui.notify(n["message"], type="warning" if n["kind"] in ("anomaly", "approval_needed", "outbid") else "info", multi_line=True, close_button=True)
        except Exception:  # noqa: BLE001  client may be mid-disconnect
            pass

    core().router.listeners.append(on_notify)
    pending = ng_app.storage.user.pop("flash", None)
    if pending:
        ui.notify(pending["message"], type=pending["type"])

    def cleanup() -> None:
        if on_notify in core().router.listeners:
            core().router.listeners.remove(on_notify)
    client.on_disconnect(cleanup)

    with ui.header().classes("items-center gap-2 px-4 py-2 bg-white text-slate-900 dark:bg-slate-900 dark:text-slate-100 border-b"):
        ui.label("Kenya").classes("text-xl font-semibold")
        ui.label("Bidder").classes("text-xl font-semibold text-primary -ml-2")
        ui.space()
        with ui.row().classes("gap-0 max-sm:hidden"):
            for path, label, icon in NAV:
                ui.button(label, icon=icon, on_click=lambda p=path: ui.navigate.to(p)).props(
                    f"flat no-caps {'color=primary' if active == path else 'color=grey-7'}")
            if user["role"] == "admin":
                ui.button("Admin", icon="admin_panel_settings", on_click=lambda: ui.navigate.to("/admin")).props(
                    f"flat no-caps {'color=primary' if active == '/admin' else 'color=grey-7'}")
        with ui.button(icon="menu").props("flat round").classes("sm:hidden"):
            with ui.menu():
                for path, label, _ in NAV + ([("/admin", "Admin", "")] if user["role"] == "admin" else []):
                    ui.menu_item(label, on_click=lambda p=path: ui.navigate.to(p))
        lang_toggle()
        label, color, tip = token_chip_state(user)
        if label:
            ui.button(label, icon="bolt", on_click=lambda: ui.navigate.to("/wallet")).props(f"flat dense no-caps color={color}").tooltip(tip)
        ui.button(user["name"], icon="person", on_click=lambda: ui.navigate.to("/profile")).props("flat dense no-caps color=grey-7").classes("max-sm:hidden")
        ui.button(icon="logout", on_click=logout).props("flat round").tooltip("Sign out")
    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-3"):
        yield


def agent_picker(user: dict, on_change) -> None:
    agents = my_agents(user)
    if len(agents) <= 1:
        return
    cur = active_agent(user)
    opts = {a["agent_id"]: f"{a['agent_type'].title()} · {a['agent_id'][:6]} · {a['status'].lower()}" for a in agents}
    ui.select(opts, value=cur["agent_id"], label="Acting as", on_change=lambda e: (set_active_agent(e.value), on_change())).classes("w-64")


def empty(msg: str) -> None:
    ui.label(msg).classes("text-slate-500 w-full text-center py-6")


def badge(text: str, color: str = "grey") -> None:
    ui.badge(text, color=color).props("outline")
