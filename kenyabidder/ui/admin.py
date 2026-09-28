"""Administrator console: LLM registry, MCP servers, knowledge bases, users, endpoint info."""
from __future__ import annotations

import shlex

from nicegui import ui

from ..llm.registry import PROVIDERS
from .common import badge, core, empty, frame, guard, pretty, require_admin, rt

ROLE_OPTS = {"BIDDER": "buyer agents", "SELLER": "seller agents"}


def register() -> None:
    ui.page("/admin")(admin_page)


def admin_page():
    user = require_admin()
    if not user:
        return
    with frame(user, "/admin"):
        ui.label("Administration").classes("text-lg font-medium")
        with ui.tabs().classes("w-full") as tabs:
            t_llm, t_mcp, t_kb, t_users, t_ep = ui.tab("LLMs"), ui.tab("MCP servers"), ui.tab("Knowledge bases"), ui.tab("Users"), ui.tab("Our MCP endpoint")
        with ui.tab_panels(tabs, value=t_llm).classes("w-full"):
            with ui.tab_panel(t_llm):
                llm_panel()
            with ui.tab_panel(t_mcp):
                mcp_panel()
            with ui.tab_panel(t_kb):
                kb_panel()
            with ui.tab_panel(t_users):
                users_panel(user)
            with ui.tab_panel(t_ep):
                endpoint_panel()


def _used_by(kind: str, ident: str) -> str:
    n = len(core().agents.references(kind, ident))
    return f"assigned to {n} agent{'s' if n != 1 else ''}" if n else "not assigned"


# ============================================================================= LLMs

def llm_panel():
    ui.label("Register the models your agents can think with. Keys are stored server-side and never shown again — or reference an environment variable instead.").classes("text-sm opacity-70")

    @ui.refreshable
    def listing():
        rows = core().llms.list()
        if not rows:
            empty("No LLMs registered yet.")
        for e in rows:
            with ui.card().classes("w-full"):
                with ui.row().classes("w-full items-center gap-2"):
                    ui.label(e["name"]).classes("font-medium")
                    badge(PROVIDERS[e["provider"]]["label"].split(" (")[0])
                    badge(e["model"])
                    badge("enabled" if e["enabled"] else "disabled", "positive" if e["enabled"] else "warning")
                    ui.space()
                    ui.label(_used_by("llm", e["id"])).classes("text-xs opacity-60")
                key = "key stored" if e.get("api_key") else (f"key from ${e['api_key_env']}" if e.get("api_key_env") else "no key set")
                ui.label(f"{e['base_url'] or 'default endpoint'} · {key} · roles: {', '.join(pretty(r) for r in e['roles'])} · max {e['max_tokens']} tokens · timeout {e['timeout_s']}s").classes("text-xs opacity-70")
                out = ui.label().classes("text-sm")

                @guard
                async def test(i=e["id"], out=out):
                    out.set_text("Testing…")
                    r = await core().llms.test(i)
                    out.set_text(f"✓ reachable — replied {r['reply']!r}" if r["ok"] else f"✗ {r['error']}")
                with ui.row():
                    ui.button("Test", icon="wifi_tethering", on_click=test).props("outline no-caps dense")
                    ui.button("Edit", icon="edit", on_click=lambda i=e["id"]: llm_dialog(i, listing.refresh)).props("outline no-caps dense")

                    @guard
                    def flip(i=e["id"], en=e["enabled"]):
                        core().llms.update(i, enabled=not en)
                        listing.refresh()

                    @guard
                    def delete(i=e["id"]):
                        core().llms.remove(i)
                        listing.refresh()
                    ui.button("Disable" if e["enabled"] else "Enable", on_click=flip).props("outline no-caps dense")
                    ui.button("Delete", icon="delete", on_click=delete).props("outline no-caps dense color=negative")
    ui.button("Add LLM", icon="add", on_click=lambda: llm_dialog(None, listing.refresh)).props("unelevated color=primary")
    listing()


def llm_dialog(llm_id: str | None, done) -> None:
    cur = core().llms.get(llm_id) if llm_id else None
    f = {"name": cur["name"] if cur else "", "provider": cur["provider"] if cur else "anthropic", "model": cur["model"] if cur else PROVIDERS["anthropic"]["default_model"],
         "base_url": cur["base_url"] if cur else "", "api_key": "", "api_key_env": cur["api_key_env"] if cur else "", "max_tokens": cur["max_tokens"] if cur else 1024,
         "temperature": cur["temperature"] if cur else None, "timeout_s": cur["timeout_s"] if cur else 30, "roles": cur["roles"] if cur else ["BIDDER", "SELLER"], "notes": cur["notes"] if cur else ""}
    with ui.dialog() as d, ui.card().classes("w-[36rem] max-w-full"):
        ui.label("Edit LLM" if cur else "Add LLM").classes("text-lg font-medium")
        ui.input("Display name", value=f["name"], on_change=lambda e: f.update(name=e.value)).classes("w-full")

        def on_provider(e):
            f["provider"] = e.value
            model.set_value(PROVIDERS[e.value]["default_model"])
            base.set_value(PROVIDERS[e.value]["base_url"])
        ui.select({k: v["label"] for k, v in PROVIDERS.items()}, value=f["provider"], label="Provider", on_change=on_provider).classes("w-full")
        model = ui.input("Model", value=f["model"], on_change=lambda e: f.update(model=e.value)).classes("w-full")
        base = ui.input("Base URL (required for OpenAI-compatible, e.g. http://localhost:11434/v1 for Ollama)", value=f["base_url"], on_change=lambda e: f.update(base_url=e.value)).classes("w-full")
        ui.input("API key" + (" (leave blank to keep the stored key)" if cur else ""), password=True, password_toggle_button=True, on_change=lambda e: f.update(api_key=e.value)).classes("w-full")
        ui.input("…or environment variable name holding the key", value=f["api_key_env"], placeholder="ANTHROPIC_API_KEY", on_change=lambda e: f.update(api_key_env=e.value)).classes("w-full")
        with ui.row().classes("w-full"):
            ui.number("Max tokens", value=f["max_tokens"], min=16, precision=0, on_change=lambda e: f.update(max_tokens=int(e.value or 0))).classes("w-36")
            ui.number("Temperature", value=f["temperature"], min=0, max=2, step=0.1, on_change=lambda e: f.update(temperature=e.value)).classes("w-36")
            ui.number("Timeout (s)", value=f["timeout_s"], min=1, on_change=lambda e: f.update(timeout_s=e.value)).classes("w-36")
        ui.select(ROLE_OPTS, value=f["roles"], multiple=True, label="May be used by", on_change=lambda e: f.update(roles=e.value)).props("use-chips").classes("w-full")
        ui.textarea("Notes", value=f["notes"], on_change=lambda e: f.update(notes=e.value)).classes("w-full")

        @guard
        def save():
            if cur:
                core().llms.update(llm_id, **f)
            else:
                core().llms.add(**f)
            d.close()
            done()
            ui.notify("Saved", type="positive")
        with ui.row().classes("justify-end w-full"):
            ui.button("Cancel", on_click=d.close).props("flat")
            ui.button("Save", on_click=save).props("unelevated color=primary")
    d.open()


# ============================================================================= MCP

def mcp_panel():
    ui.label("Register MCP servers and choose which of their tools each agent may use. The built-in KenyaBidder server is always present.").classes("text-sm opacity-70")

    @ui.refreshable
    def listing():
        for m in core().mcps.list():
            with ui.card().classes("w-full"):
                with ui.row().classes("w-full items-center gap-2"):
                    ui.label(m["name"]).classes("font-medium")
                    badge(m["transport"])
                    if m["builtin"]:
                        badge("built-in", "primary")
                    badge("enabled" if m["enabled"] else "disabled", "positive" if m["enabled"] else "warning")
                    ui.space()
                    ui.label(_used_by("mcp", m["id"])).classes("text-xs opacity-60")
                where = m["url"] or m["command"] or "in-process FastMCP server"
                ui.label(f"{where} · roles: {', '.join(pretty(r) for r in m['roles'])} · {len(m['tools'])} tool(s) discovered").classes("text-xs opacity-70")
                if m["last_error"]:
                    ui.label(f"⚠ {m['last_error']}").classes("text-sm text-negative")

                @guard
                async def refresh(i=m["id"]):
                    tools = await core().mcps.refresh_tools(i)
                    ui.notify(f"Discovered {len(tools)} tool(s)", type="positive")
                    listing.refresh()

                @guard
                def flip(i=m["id"], en=m["enabled"]):
                    core().mcps.update(i, enabled=not en)
                    listing.refresh()

                @guard
                def delete(i=m["id"]):
                    core().mcps.remove(i)
                    listing.refresh()

                @guard
                def set_roles(e, i=m["id"]):
                    if e.value:
                        core().mcps.update(i, roles=e.value)
                with ui.row().classes("items-center"):
                    ui.button("Discover tools", icon="travel_explore", on_click=refresh).props("outline no-caps dense")
                    ui.button("Disable" if m["enabled"] else "Enable", on_click=flip).props("outline no-caps dense")
                    if not m["builtin"]:
                        ui.button("Delete", icon="delete", on_click=delete).props("outline no-caps dense color=negative")
                    ui.select(ROLE_OPTS, value=m["roles"], multiple=True, label="Available to", on_change=set_roles).props("dense use-chips").classes("w-72")
                if m["tools"]:
                    with ui.expansion(f"Tools ({len(m['tools'])}) — untick to hide a tool from every agent").classes("w-full border rounded"):
                        for t in m["tools"]:
                            def toggle(e, i=m["id"], name=t["name"], srv=m):
                                dis = set(srv["disabled_tools"])
                                dis.discard(name) if e.value else dis.add(name)
                                core().mcps.update(i, disabled_tools=sorted(dis))
                            with ui.column().classes("gap-0 px-2 pb-1"):
                                ui.checkbox(t["name"], value=t["name"] not in m["disabled_tools"], on_change=toggle).classes("font-mono text-sm")
                                ui.label(t["description"][:220]).classes("text-xs opacity-70 ml-9 -mt-2")
    ui.button("Add MCP server", icon="add", on_click=lambda: mcp_dialog(listing.refresh)).props("unelevated color=primary")
    listing()

    @guard
    async def first_discovery():
        b = core().mcps.ensure_builtin()
        if not b["tools"]:
            await core().mcps.refresh_tools("builtin")
            listing.refresh()
    ui.timer(0.2, first_discovery, once=True)


def mcp_dialog(done) -> None:
    f = {"name": "", "transport": "http", "url": "", "bearer_token": "", "bearer_env": "", "command": "", "args": "", "env": "", "roles": ["BIDDER", "SELLER"]}
    transports = {"http": "HTTP (streamable)"}
    if core().mcps.allow_stdio:
        transports["stdio"] = "stdio (runs a local command)"
    with ui.dialog() as d, ui.card().classes("w-[36rem] max-w-full"):
        ui.label("Add MCP server").classes("text-lg font-medium")
        ui.input("Name", on_change=lambda e: f.update(name=e.value)).classes("w-full")
        ui.select(transports, value="http", label="Transport", on_change=lambda e: (f.update(transport=e.value), form.refresh())).classes("w-full")
        if not core().mcps.allow_stdio:
            ui.label("stdio servers execute commands on this host and are disabled. Start the app with KENYABIDDER_ALLOW_STDIO=1 to enable them.").classes("text-xs opacity-60")

        @ui.refreshable
        def form():
            if f["transport"] == "http":
                ui.input("Server URL", placeholder="https://mcp.example.com/mcp", on_change=lambda e: f.update(url=e.value)).classes("w-full")
                ui.input("Bearer token (optional)", password=True, password_toggle_button=True, on_change=lambda e: f.update(bearer_token=e.value)).classes("w-full")
                ui.input("…or env var holding the token", on_change=lambda e: f.update(bearer_env=e.value)).classes("w-full")
            else:
                ui.input("Command", placeholder="npx", on_change=lambda e: f.update(command=e.value)).classes("w-full")
                ui.input("Arguments (space separated)", placeholder="-y @modelcontextprotocol/server-filesystem /data", on_change=lambda e: f.update(args=e.value)).classes("w-full")
                ui.textarea("Environment (KEY=VALUE per line)", on_change=lambda e: f.update(env=e.value)).classes("w-full")
        form()
        ui.select(ROLE_OPTS, value=f["roles"], multiple=True, label="Available to", on_change=lambda e: f.update(roles=e.value)).props("use-chips").classes("w-full")

        @guard
        async def save():
            env = {}
            for line in f["env"].splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
            m = core().mcps.add(name=f["name"], transport=f["transport"], url=f["url"], bearer_token=f["bearer_token"], bearer_env=f["bearer_env"],
                                command=f["command"], args=shlex.split(f["args"]), env=env, roles=f["roles"])
            d.close()
            try:
                tools = await core().mcps.refresh_tools(m["id"])
                ui.notify(f"Registered {m['name']} — discovered {len(tools)} tool(s)", type="positive")
            except Exception as e:  # noqa: BLE001
                ui.notify(f"Registered {m['name']}, but discovery failed: {getattr(e, 'message', e)}", type="warning", multi_line=True)
            done()
        with ui.row().classes("justify-end w-full"):
            ui.button("Cancel", on_click=d.close).props("flat")
            ui.button("Register & discover tools", on_click=save).props("unelevated color=primary")
    d.open()


# ============================================================================= knowledge

def kb_panel():
    ui.label("Add reference material (pricing guides, policies, product notes). Agents assigned a knowledge base can search it while deciding. Content is treated as data, never as instructions.").classes("text-sm opacity-70")

    @ui.refreshable
    def listing():
        kbs = core().kb.list()
        if not kbs:
            empty("No knowledge bases yet.")
        for kb in kbs:
            with ui.card().classes("w-full"):
                with ui.row().classes("w-full items-center gap-2"):
                    ui.label(kb["name"]).classes("font-medium")
                    badge(f"{len(kb['docs'])} doc(s)")
                    badge("enabled" if kb["enabled"] else "disabled", "positive" if kb["enabled"] else "warning")
                    ui.space()
                    ui.label(_used_by("kb", kb["id"])).classes("text-xs opacity-60")
                if kb["description"]:
                    ui.label(kb["description"]).classes("text-sm opacity-70")

                @guard
                def flip(i=kb["id"], en=kb["enabled"]):
                    core().kb.update(i, enabled=not en)
                    listing.refresh()

                @guard
                def delete(i=kb["id"]):
                    core().kb.remove(i)
                    listing.refresh()

                @guard
                def set_roles(e, i=kb["id"]):
                    if e.value:
                        core().kb.update(i, roles=e.value)
                with ui.row().classes("items-center"):
                    ui.button("Add document", icon="note_add", on_click=lambda i=kb["id"]: doc_dialog(i, listing.refresh)).props("outline no-caps dense")
                    ui.button("Disable" if kb["enabled"] else "Enable", on_click=flip).props("outline no-caps dense")
                    ui.button("Delete", icon="delete", on_click=delete).props("outline no-caps dense color=negative")
                    ui.select(ROLE_OPTS, value=kb["roles"], multiple=True, label="Available to", on_change=set_roles).props("dense use-chips").classes("w-72")
                for doc in kb["docs"].values():
                    with ui.row().classes("w-full items-center gap-2 px-2"):
                        ui.icon("description").classes("opacity-60")
                        ui.label(doc["title"]).classes("text-sm")
                        ui.label(f"{doc['chars']:,} chars · {len(doc['chunks'])} chunks · {doc['source'][:60]}").classes("text-xs opacity-60")
                        ui.space()

                        @guard
                        def rm(k=kb["id"], dd=doc["id"]):
                            core().kb.remove_doc(k, dd)
                            listing.refresh()
                        ui.button(icon="close", on_click=rm).props("flat round dense size=sm")
    ui.button("New knowledge base", icon="add", on_click=lambda: kb_dialog(listing.refresh)).props("unelevated color=primary")
    listing()

    with ui.card().classes("w-full"):
        ui.label("Test retrieval").classes("font-medium")
        q = ui.input("Search all enabled knowledge bases").classes("w-full")
        res = ui.column().classes("w-full gap-1")

        @guard
        def run():
            res.clear()
            hits = core().kb.search(q.value, None, 5)
            with res:
                if not hits:
                    ui.label("No matches.").classes("opacity-70")
                for h in hits:
                    ui.label(f"{h['title']} ({h['kb']}) · score {h['score']}").classes("text-sm font-medium")
                    ui.label(h["text"][:300]).classes("text-xs opacity-70")
        q.on("keydown.enter", run)
        ui.button("Search", icon="search", on_click=run).props("outline")


def kb_dialog(done) -> None:
    f = {"name": "", "description": "", "roles": ["BIDDER", "SELLER"]}
    with ui.dialog() as d, ui.card().classes("w-96 max-w-full"):
        ui.label("New knowledge base").classes("text-lg font-medium")
        ui.input("Name", on_change=lambda e: f.update(name=e.value)).classes("w-full")
        ui.textarea("Description", on_change=lambda e: f.update(description=e.value)).classes("w-full")
        ui.select(ROLE_OPTS, value=f["roles"], multiple=True, label="Available to", on_change=lambda e: f.update(roles=e.value)).props("use-chips").classes("w-full")

        @guard
        def save():
            core().kb.create(f["name"], f["description"], f["roles"])
            d.close()
            done()
        with ui.row().classes("justify-end w-full"):
            ui.button("Cancel", on_click=d.close).props("flat")
            ui.button("Create", on_click=save).props("unelevated color=primary")
    d.open()


def doc_dialog(kb_id: str, done) -> None:
    f = {"title": "", "text": "", "url": ""}
    with ui.dialog() as d, ui.card().classes("w-[40rem] max-w-full"):
        ui.label("Add document").classes("text-lg font-medium")
        with ui.tabs().classes("w-full") as tabs:
            t1, t2, t3 = ui.tab("Paste text"), ui.tab("Upload file"), ui.tab("From URL")
        with ui.tab_panels(tabs, value=t1).classes("w-full"):
            with ui.tab_panel(t1):
                ui.input("Title", on_change=lambda e: f.update(title=e.value)).classes("w-full")
                ui.textarea("Text", on_change=lambda e: f.update(text=e.value)).props("rows=8").classes("w-full")

                @guard
                def add_text():
                    core().kb.add_text(kb_id, f["title"], f["text"])
                    d.close()
                    done()
                    ui.notify("Document added", type="positive")
                ui.button("Add", on_click=add_text).props("unelevated color=primary")
            with ui.tab_panel(t2):
                ui.label("Plain text, markdown, CSV, JSON or HTML (PDF if pypdf is installed). Max ~500k characters.").classes("text-xs opacity-70")

                @guard
                async def on_upload(e):
                    data = await e.file.read()
                    core().kb.add_file(kb_id, e.file.name, data)
                    ui.notify(f"Added {e.file.name}", type="positive")
                    done()
                ui.upload(on_upload=on_upload, auto_upload=True, max_file_size=5_000_000).props("accept=.txt,.md,.csv,.json,.html,.htm,.pdf").classes("w-full")
            with ui.tab_panel(t3):
                ui.input("Title (optional)", on_change=lambda e: f.update(title=e.value)).classes("w-full")
                ui.input("URL", placeholder="https://…", on_change=lambda e: f.update(url=e.value)).classes("w-full")
                ui.label("Only public http(s) addresses are fetched; internal/private hosts are blocked.").classes("text-xs opacity-70")

                @guard
                async def add_url():
                    await core().kb.add_url(kb_id, f["url"], f["title"])
                    d.close()
                    done()
                    ui.notify("Page added", type="positive")
                ui.button("Fetch & add", on_click=add_url).props("unelevated color=primary")
        ui.button("Close", on_click=d.close).props("flat")
    d.open()


# ============================================================================= users + endpoint

def users_panel(me: dict):
    @ui.refreshable
    def listing():
        for u in sorted(core().store.users.values(), key=lambda x: x["created_at"]):
            with ui.card().classes("w-full"):
                with ui.row().classes("w-full items-center gap-2"):
                    ui.label(u["name"]).classes("font-medium")
                    badge(u["role"], "primary" if u["role"] == "admin" else "grey")
                    ui.label(f"{len(core().agents.agents_for(u['id']))} agent(s)").classes("text-xs opacity-60")
                    ui.space()

                    @guard
                    def flip(i=u["id"], r=u["role"]):
                        core().agents.set_role(i, "user" if r == "admin" else "admin")
                        listing.refresh()
                    ui.button("Make user" if u["role"] == "admin" else "Make admin", on_click=flip).props("outline no-caps dense")
    listing()


def endpoint_panel():
    r = rt()
    with ui.card().classes("w-full"):
        ui.label("KenyaBidder as an MCP server (FastMCP)").classes("font-medium")
        ui.label("Expose the platform's read-only auction, market and knowledge tools to any MCP client (Claude Desktop, your own agents…). State-changing tools stay internal to the execution harness and are never served here.").classes("text-sm opacity-70")
        if r.mcp_url:
            ui.label("Endpoint").classes("text-xs opacity-60 mt-2")
            ui.label(r.mcp_url).classes("font-mono text-sm")
            ui.label("Bearer key").classes("text-xs opacity-60 mt-2")
            shown = ui.label("••••••••••••••••").classes("font-mono text-sm")

            def reveal():
                shown.set_text(r.mcp_key)
            ui.button("Reveal key", icon="visibility", on_click=reveal).props("outline no-caps dense")
            ui.label("Bound to 127.0.0.1 by default; put it behind a TLS reverse proxy before exposing it.").classes("text-xs opacity-60")
        else:
            ui.label("The HTTP endpoint is disabled. Start with --mcp-port 8765 (or KENYABIDDER_MCP_PORT) to enable it.").classes("text-sm")
