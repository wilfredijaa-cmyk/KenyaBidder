"""Shared UI plumbing: auth, page frame, notification push, formatting."""
from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from nicegui import app as ng_app
from nicegui import Client, context, ui

from .. import i18n
from ..errors import AppError
from ..i18n import LANGS, Verbatim
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

SESSION_MAX_MS = 12 * 3600_000          # absolute: sign in again after 12 hours
SESSION_IDLE_MS = 2 * 3600_000          # idle timeout for users…
ADMIN_IDLE_MS = 30 * 60_000             # …and a much shorter one for administrators


def client_ip() -> str:
    """The caller's real address (X-Forwarded-For honoured only from configured trusted proxies)."""
    try:
        return core().client_ip.key(core().client_ip.from_request(context.client.request))
    except Exception:  # noqa: BLE001  no request context (tests, background work)
        return "unknown"


def current_user() -> dict | None:
    st = ng_app.storage.user
    uid = st.get("uid")
    u = core().store.users.get(uid) if uid else None
    if u:
        now = core().clock.now()
        idle = ADMIN_IDLE_MS if u["role"] == "admin" else SESSION_IDLE_MS
        if "at" not in st or now - st["at"] > SESSION_MAX_MS or now - st.get("seen", now) > idle:  # (no stamp = older release: sign in again)
            st.clear()  # expired: a stolen cookie or an unattended screen stops working
            return None
        if now - st.get("seen", 0) > 60_000:
            st["seen"] = now
    if u and ng_app.storage.user.get("sv", 0) != u.get("session_version", 0):
        return None  # the password was changed/reset since this browser signed in: every other session ends
    return None if (u and u.get("suspended")) else u  # a suspended account is signed out on its next page load


def login_user(user: dict) -> None:
    lang = ng_app.storage.user.get("lang")
    ng_app.storage.user.clear()  # never carry state from an anonymous (or someone else's) session into the new one
    now = core().clock.now()
    ng_app.storage.user.update(uid=user["id"], sv=user.get("session_version", 0), at=now, seen=now)
    if lang:
        ng_app.storage.user["lang"] = lang


def refresh_session(user: dict) -> None:
    """Keep THIS browser signed in after a credential change (which ended every other session) WITHOUT extending its absolute lifetime."""
    st = ng_app.storage.user
    st["sv"], st["seen"] = user.get("session_version", 0), core().clock.now()


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
        if not u or u["role"] != "admin" or not core().agents.admin_2fa_ok(u):
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
    if u and not core().agents.admin_2fa_ok(u):
        flash("Set up two-factor authentication (Profile) to use the admin console", "warning")
        ui.navigate.to("/profile")
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


def public(fn):
    """Mark a UI action as usable without a session (sign-in, sign-up, password reset)."""
    fn._public = True
    return fn


def guard(fn):
    """Run a UI action: re-check that the session is still valid AT CLICK TIME (a page left open keeps its websocket after the cookie was
    revoked, expired or the account was suspended), show domain errors as toasts, and never show internals."""
    import functools
    import inspect
    import logging
    import uuid

    @functools.wraps(fn)
    async def wrapper(*a, **kw):
        if not getattr(fn, "_public", False) and not current_user():
            ui.notify("Your session has ended — please sign in again", type="warning")
            ui.navigate.to("/login")
            return None
        try:
            r = fn(*a, **kw)
            return await r if inspect.isawaitable(r) else r
        except AppError as e:
            ui.notify(e.message, type="negative", multi_line=True)
        except Exception:  # noqa: BLE001
            ref = uuid.uuid4().hex[:8]
            logging.getLogger("kenyabidder.ui").exception("unexpected error in a UI action (ref %s)", ref)
            ui.notify(f"Something went wrong on our side (reference {ref}). Please try again; if it keeps happening, quote the reference to support.", type="negative", multi_line=True)
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

    def alive(n: dict) -> None:
        if client.id not in Client.instances:  # a page that never connected (aborted tab, bot) is dropped instead of leaking forever
            cleanup()
            return
        on_notify(n)

    core().router.listeners.append(alive)
    pending = ng_app.storage.user.pop("flash", None)
    if pending:
        ui.notify(pending["message"], type=pending["type"])

    def cleanup() -> None:
        if alive in core().router.listeners:
            core().router.listeners.remove(alive)
    client.on_disconnect(cleanup)
    client.on_delete(cleanup)  # disconnect handlers never run for a client that never connected; delete handlers always do

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
        ui.button(Verbatim(user["name"]), icon="person", on_click=lambda: ui.navigate.to("/profile")).props("flat dense no-caps color=grey-7").classes("max-sm:hidden")
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
