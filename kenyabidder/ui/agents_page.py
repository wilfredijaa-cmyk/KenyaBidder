"""Agent list + the per-agent configuration page: rules, brain (LLM + algorithms), tools & knowledge, channels."""
from __future__ import annotations

from nicegui import ui

from ..agents import ADVISORS, STRATEGIES
from ..engine import AUCTION_TYPES, FORWARD_TYPES, REVERSE_TYPES
from .common import badge, core, empty, flash, frame, guard, kes, pretty, require_user, set_active_agent


def register() -> None:
    ui.page("/agents")(agents_page)
    ui.page("/agent/{agent_id}")(agent_page)


def agents_page():
    user = require_user()
    if not user:
        return
    with frame(user, "/agents"):
        ui.label("My agents").classes("text-lg font-medium")
        ui.label("Each agent is a persistent identity: its constraints, memory, brain (LLM), tools and knowledge follow you across every channel.").classes("text-sm opacity-70")

        def create(kind: str):
            @guard
            def go():
                c = {"budget_ceiling": 100_000} if kind == "BIDDER" else {}
                a = core().agents.create_agent(user_id=user["id"], type=kind, constraints=c)
                set_active_agent(a["agent_id"])
                ui.navigate.to(f"/agent/{a['agent_id']}")
            return go

        with ui.row():
            ui.button("New buyer agent", icon="shopping_cart", on_click=create("BIDDER")).props("unelevated color=primary")
            ui.button("New seller agent", icon="storefront", on_click=create("SELLER")).props("outline color=primary")

            @guard
            def stop_all():
                n = core().agents.pause_all(user["id"])
                ui.notify(f"Paused {n} agent(s); their live plans were cancelled", type="warning")
                ui.navigate.reload()
            ui.button("Pause ALL my agents", icon="pan_tool", on_click=stop_all).props("outline color=negative no-caps")
        agents = core().agents.agents_for(user["id"])
        if not agents:
            empty("No agents yet.")
        with ui.grid().classes("w-full gap-3 grid-cols-1 sm:grid-cols-2"):
            for a in agents:
                c = a["config"]
                with ui.card().classes("cursor-pointer").on("click", lambda _, i=a["agent_id"]: (set_active_agent(i), ui.navigate.to(f"/agent/{i}"))):
                    with ui.row().classes("w-full items-center"):
                        ui.label(f"{'Buyer' if a['agent_type'] == 'BIDDER' else 'Seller'} agent · {a['agent_id'][:6]}").classes("font-medium")
                        ui.space()
                        badge(a["status"].lower(), "positive" if a["status"] == "ACTIVE" else "warning")
                    if a["agent_type"] == "BIDDER":
                        ui.label(f"Ceiling {kes(a['constraints']['budget_ceiling'])}").classes("text-sm")
                    llm = core().store.llms.get(c["llm_id"])
                    ui.label(f"Brain: {llm['name'] if llm else 'none'} · {len(c['tools'])} tool(s) · {len(c['kb_ids'])} knowledge base(s)").classes("text-xs opacity-70")
                    ui.label(f"Reputation: {a['reputation']['tier'].lower()} (score {a['reputation']['score']})").classes("text-xs opacity-70")


def agent_page(agent_id: str):
    user = require_user()
    if not user:
        return
    with frame(user, "/agents"):
        try:
            agent = core().agents.owned_agent(agent_id, user["id"])
        except Exception:  # noqa: BLE001
            empty("Agent not found.")
            return
        role = agent["agent_type"]
        set_active_agent(agent_id)
        ui.button("All agents", icon="arrow_back", on_click=lambda: ui.navigate.to("/agents")).props("flat no-caps")
        with ui.row().classes("w-full items-center"):
            ui.label(f"{'Buyer' if role == 'BIDDER' else 'Seller'} agent · {agent_id[:6]}").classes("text-xl font-medium")
            badge(f"{agent['reputation']['tier'].lower()} · score {agent['reputation']['score']}")
            ui.space()

            @guard
            def toggle():
                core().agents.set_status(agent_id, "PAUSED" if agent["status"] == "ACTIVE" else "ACTIVE")
                flash("Paused — pending bids cancelled" if agent["status"] == "PAUSED" else "Resumed", "warning" if agent["status"] == "PAUSED" else "positive")
                ui.navigate.reload()
            ui.button("Pause agent" if agent["status"] == "ACTIVE" else "Resume agent", icon="pause" if agent["status"] == "ACTIVE" else "play_arrow", on_click=toggle).props(
                f"{'outline color=negative' if agent['status'] == 'ACTIVE' else 'unelevated color=primary'}").tooltip("Revoking authority cancels every pending bid trigger immediately")

        with ui.tabs().classes("w-full") as tabs:
            t_rules, t_brain, t_chan, t_talk = ui.tab("Rules"), ui.tab("Brain, tools & knowledge"), ui.tab("Channels"), ui.tab("Talk to agent")
        with ui.tab_panels(tabs, value=t_rules).classes("w-full"):
            with ui.tab_panel(t_rules):
                rules_tab(agent)
            with ui.tab_panel(t_brain):
                brain_tab(agent)
            with ui.tab_panel(t_chan):
                channels_tab(agent)
            with ui.tab_panel(t_talk):
                talk_tab(agent)


# ----------------------------------------------------------------------------- rules

def rules_tab(agent: dict) -> None:
    c, m, role = agent["constraints"], agent["durable_memory"], agent["agent_type"]
    f = {"ceiling": c["budget_ceiling"], "floor": c["reserve_floor"], "esc": c["escalation_threshold_pct"], "types": list(c["authorized_auction_types"]),
         "cat": (m.get("watch") or {}).get("category") or "", "kw": ", ".join((m.get("watch") or {}).get("keywords", [])),
         "minq": (m.get("watch") or {}).get("min_quantity", 1), "auto": m.get("auto_bid", True),
         "summary": m.get("daily_summary", True), "relist": bool(m.get("auto_relist")), "maxr": (m.get("auto_relist") or {}).get("max_relists", 3), "disc": (m.get("auto_relist") or {}).get("discount_pct", 10)}
    with ui.card().classes("w-full"):
        ui.label("Hard limits (enforced by the deterministic guardrail — the LLM cannot override them)").classes("font-medium")
        if role == "BIDDER":
            ui.number("Budget ceiling per bid (KES)", value=f["ceiling"], min=1, precision=0, on_change=lambda e: f.update(ceiling=int(e.value or 0))).classes("w-64")
            ui.number("Ask me before bidding above this % of the ceiling", value=f["esc"], min=1, max=100, on_change=lambda e: f.update(esc=e.value)).classes("w-96")
        else:
            ui.number("Price floor (KES) — never list or quote below this", value=f["floor"], min=0, precision=0, on_change=lambda e: f.update(floor=int(e.value or 0))).classes("w-96")
        type_opts = {t: pretty(t) for t in (FORWARD_TYPES if role == "BIDDER" else AUCTION_TYPES)}
        ui.select(type_opts, value=[t for t in f["types"] if t in type_opts], multiple=True,
                  label="Auction types this agent may " + ("join without asking" if role == "BIDDER" else "list or quote on"),
                  on_change=lambda e: f.update(types=e.value)).props("use-chips").classes("w-full")
        if role == "SELLER":
            ui.label("Which buyer requests (RFQs) should it quote on?").classes("font-medium mt-2")
            ui.switch("Quote automatically on new matching RFQs", value=f["auto"], on_change=lambda e: f.update(auto=e.value))
            with ui.row().classes("w-full"):
                ui.input("Category", value=f["cat"], placeholder="electronics", on_change=lambda e: f.update(cat=e.value)).classes("w-56")
                ui.input("Keywords (comma separated)", value=f["kw"], on_change=lambda e: f.update(kw=e.value)).classes("grow")
                ui.number("Min quantity", value=f["minq"], min=1, precision=0, on_change=lambda e: f.update(minq=int(e.value or 1))).classes("w-36")
        if role == "BIDDER":
            ui.label("What should it hunt for?").classes("font-medium mt-2")
            ui.switch("Bid automatically on new matching listings", value=f["auto"], on_change=lambda e: f.update(auto=e.value))
            with ui.row().classes("w-full"):
                ui.input("Category", value=f["cat"], placeholder="electronics", on_change=lambda e: f.update(cat=e.value)).classes("w-56")
                ui.input("Keywords (comma separated)", value=f["kw"], on_change=lambda e: f.update(kw=e.value)).classes("grow")
                ui.number("Min quantity", value=f["minq"], min=1, precision=0, on_change=lambda e: f.update(minq=int(e.value or 1))).classes("w-36")
        else:
            ui.label("Unsold lots").classes("font-medium mt-2")
            ui.switch("Send me an evening summary (18:00 EAT) when there is activity", value=m.get("daily_summary", True), on_change=lambda e: f.update(summary=e.value))
            ui.switch("Auto-relist unsold lots at a lower reserve", value=f["relist"], on_change=lambda e: f.update(relist=e.value))
            with ui.row():
                ui.number("Max relists", value=f["maxr"], min=0, max=20, precision=0, on_change=lambda e: f.update(maxr=int(e.value or 0))).classes("w-36")
                ui.number("Reserve discount per relist (%)", value=f["disc"], min=0, max=99, on_change=lambda e: f.update(disc=e.value)).classes("w-64")

        @guard
        def save():
            if role == "BIDDER":
                constraints = {"budget_ceiling": f["ceiling"], "escalation_threshold_pct": f["esc"], "authorized_auction_types": f["types"]}
                watch = {"category": f["cat"], "keywords": [k.strip() for k in f["kw"].split(",") if k.strip()], "min_quantity": f["minq"]} if f["cat"].strip() or f["kw"].strip() else None
                core().agents.update(agent["agent_id"], constraints=constraints, memory={"auto_bid": f["auto"], "watch": watch})
            else:
                watch = {"category": f["cat"], "keywords": [k.strip() for k in f["kw"].split(",") if k.strip()], "min_quantity": f["minq"]} if f["cat"].strip() or f["kw"].strip() else None
                core().agents.update(agent["agent_id"], constraints={"reserve_floor": f["floor"], "authorized_auction_types": f["types"]},
                                     memory={"auto_bid": f["auto"], "watch": watch, "auto_relist": {"max_relists": f["maxr"], "discount_pct": f["disc"]} if f["relist"] else None, "daily_summary": f["summary"]})
            ui.notify("Saved", type="positive")
        ui.button("Save rules", icon="save", on_click=save).props("unelevated color=primary").classes("mt-2")


# ----------------------------------------------------------------------------- brain

def brain_tab(agent: dict) -> None:
    """LLM, per-algorithm strategy mapping, assigned MCP tools, assigned knowledge bases."""
    role, cfg = agent["agent_type"], agent["config"]
    llms = core().llms.list(enabled_only=True, role=role)
    llm_opts = {"": "— none —", **{l["id"]: f"{l['name']} ({l['model']})" for l in llms}}
    for lid in {cfg["llm_id"], (cfg.get("advisor") or {}).get("llm_id"), *(v.get("llm_id") for v in cfg["algorithms"].values())} - {None, ""}:
        if lid not in llm_opts and lid in core().store.llms:  # keep an assigned-but-now-disabled model visible instead of a blank select
            llm_opts[lid] = f"{core().store.llms[lid]['name']} (disabled)"
    st = {"cap": cfg.get("max_tokens_per_day"), "llm": cfg["llm_id"] or "", "algos": {t: dict(v) for t, v in cfg["algorithms"].items()}, "advisor": dict(cfg["advisor"] or {"strategy": "rules", "llm_id": None}),
          "tools": set(cfg["tools"]), "kbs": set(cfg["kb_ids"]), "steps": cfg["max_tool_steps"]}
    for t in (FORWARD_TYPES if role == "BIDDER" else REVERSE_TYPES):
        st["algos"].setdefault(t, {"strategy": "heuristic", "llm_id": None})  # agents created before RFQs existed

    with ui.card().classes("w-full"):
        ui.label("Brain").classes("font-medium")
        ui.label("The LLM only ever proposes — the guardrail and the deterministic execution engine decide and act. Pick which model this agent thinks with, and which algorithm runs for each auction type.").classes("text-sm opacity-70")
        if not llms:
            ui.label(f"No LLMs are enabled for {role.lower()} agents yet. An administrator can add one under Admin → LLMs. Deterministic algorithms (baseline, heuristic) work without one.").classes("text-sm text-amber-700")
        ui.select(llm_opts, value=st["llm"], label="Default LLM", on_change=lambda e: st.update(llm=e.value)).classes("w-96")
        with ui.column().classes("w-full gap-1"):
            ui.label("Algorithm per auction type" if role == "BIDDER" else "Algorithm per RFQ type (how this agent quotes)").classes("text-sm font-medium mt-2")
            for t in (FORWARD_TYPES if role == "BIDDER" else REVERSE_TYPES):
                with ui.row().classes("w-full items-center gap-3"):
                    ui.label(pretty(t).capitalize()).classes("w-44")
                    ui.select({s: {"baseline": "baseline — bid up to ceiling", "heuristic": "heuristic — market-price valuation", "llm": "llm — tool-using model"}[s] for s in STRATEGIES},
                              value=st["algos"][t]["strategy"], on_change=lambda e, t=t: st["algos"][t].update(strategy=e.value)).classes("w-80")
                    ui.select(llm_opts, value=st["algos"][t]["llm_id"] or "", label="LLM override",
                              on_change=lambda e, t=t: st["algos"][t].update(llm_id=e.value or None)).classes("w-72")
        if role == "SELLER":
            with ui.row().classes("items-center gap-3"):
                ui.select({s: {"rules": "rules — market statistics", "llm": "llm — tool-using model"}[s] for s in ADVISORS}, value=st["advisor"]["strategy"],
                          label="Listing advisor", on_change=lambda e: st["advisor"].update(strategy=e.value)).classes("w-80")
                ui.select(llm_opts, value=st["advisor"].get("llm_id") or "", label="Advisor LLM override",
                          on_change=lambda e: st["advisor"].update(llm_id=e.value or None)).classes("w-72")
        ui.number("Max tool-call rounds per decision", value=st["steps"], min=0, max=10, precision=0, on_change=lambda e: st.update(steps=int(e.value or 0))).classes("w-72")
        ui.number("Daily token cap for this agent (blank = no cap)", value=st["cap"], min=1000, precision=0, on_change=lambda e: st.update(cap=int(e.value) if e.value else None)).classes("w-96").tooltip(
            "Stops a runaway agent from spending your whole wallet: once it has used this many tokens in 24 hours it waits.")
        token_status(agent)

    with ui.card().classes("w-full"):
        ui.label("Tools (from registered MCP servers)").classes("font-medium")
        ui.label("Only the tools ticked here are visible to this agent's LLM. Read-only research tools only — state-changing tools (submit_bid, create_match…) are reserved for the harness and can never be assigned.").classes("text-sm opacity-70")
        servers = core().mcps.list(enabled_only=True, role=role)
        if not servers:
            ui.label("No MCP servers are enabled for this agent type.").classes("opacity-70")
        for m in servers:
            tools = [t for t in m["tools"] if t["name"] not in m["disabled_tools"]]
            with ui.expansion(f"{m['name']} — {len(tools)} tool(s)", icon="hub", value=any(r.startswith(m["id"] + ":") for r in st["tools"])).classes("w-full border rounded"):
                if not tools:
                    ui.label("No tools discovered yet — an administrator can refresh this server under Admin → MCP servers.").classes("text-sm opacity-70 p-2")
                for t in tools:
                    ref = f"{m['id']}:{t['name']}"

                    def toggle(e, ref=ref):
                        st["tools"].add(ref) if e.value else st["tools"].discard(ref)
                    with ui.column().classes("gap-0 px-2 pb-1"):
                        ui.checkbox(t["name"], value=ref in st["tools"], on_change=toggle).classes("font-mono text-sm")
                        ui.label(t["description"][:200]).classes("text-xs opacity-70 ml-9 -mt-2")

    with ui.card().classes("w-full"):
        ui.label("Knowledge bases").classes("font-medium")
        kbs = core().kb.list(role=role, enabled_only=True)
        if not kbs:
            ui.label("No knowledge bases available yet. An administrator can add documents under Admin → Knowledge bases.").classes("opacity-70")
        for kb in kbs:
            def togglekb(e, i=kb["id"]):
                st["kbs"].add(i) if e.value else st["kbs"].discard(i)
            ui.checkbox(f"{kb['name']} — {len(kb['docs'])} document(s)", value=kb["id"] in st["kbs"], on_change=togglekb)

    @guard
    def save():
        core().agents.update(agent["agent_id"], config={"llm_id": st["llm"] or None, "algorithms": st["algos"],
                                                        "advisor": st["advisor"] if role == "SELLER" else None, "tools": sorted(st["tools"]),
                                                        "kb_ids": sorted(st["kbs"]), "max_tool_steps": st["steps"], "max_tokens_per_day": st["cap"]})
        ui.notify("Brain configuration saved", type="positive")
    ui.button("Save brain, tools & knowledge", icon="save", on_click=save).props("unelevated color=primary")


def token_status(agent: dict) -> None:
    """Can this agent operate right now? Shown next to the LLM mapping so nobody is surprised by a silent agent."""
    c = core()

    @ui.refreshable
    def box():
        s = c.meter.status_for_agent(agent)
        if not s["llms"] and not s["cap"]:
            ui.label("This agent uses only deterministic algorithms, so it needs no tokens.").classes("text-xs opacity-70")
            return
        for r in s["llms"]:
            if not r["metered"]:
                ui.label(f"{r['name']}: free (platform-funded)").classes("text-sm text-primary")
            elif r["ok"]:
                ui.label(f"{r['name']}: {r['balance']:,} tokens — ready").classes("text-sm text-primary")
            else:
                with ui.row().classes("items-center gap-2"):
                    ui.label(f"{r['name']}: {r['balance']:,} tokens — needs at least {r['min_needed']:,}. The agent will {'wait' if c.meter.policy == 'block' else 'use the heuristic'} until you top up.").classes("text-sm text-negative")
                    ui.link("Buy tokens", "/wallet").classes("text-sm")
        if s["avg_decision_tokens"]:
            ui.label(f"Typical cost: about {s['avg_decision_tokens']:,} tokens per LLM call (last 20).").classes("text-xs opacity-70")
        if s["cap"]:
            ui.label(f"Daily cap: {s['used_24h']:,} of {s['cap']:,} tokens used in the last 24h" + (" — cap reached, waiting" if s["cap_reached"] else "")).classes("text-xs opacity-70")
    box()
    ui.timer(5.0, box.refresh)


# ----------------------------------------------------------------------------- channels + chat

def channels_tab(agent: dict) -> None:
    m = agent["durable_memory"]
    with ui.card().classes("w-full"):
        ui.label("Channels").classes("font-medium")
        ui.label("One agent, one memory. Everything you do here shows up on WhatsApp and vice-versa.").classes("text-sm opacity-70")
        ui.select({"WEB": "Web only", "WHATSAPP": "WhatsApp", "SMS": "SMS (verified phone)", "EMAIL": "Email (verified address)"}, value=m["preferred_channel"], label="Send notifications to",
                  on_change=guard(lambda e: core().agents.update(agent["agent_id"], memory={"preferred_channel": e.value}))).classes("w-64")
        linked = ui.label(", ".join(f"{c['channel']} {c['external_id']}" for c in agent["channel_identity_map"]) or "Nothing linked yet").classes("text-sm")
        owner = core().store.users.get(agent["principal_user_id"], {})
        if owner.get("phone") and owner.get("phone_verified"):
            ui.label(f"WhatsApp number to link: {owner['phone']} (your verified phone)").classes("text-sm")
        else:
            ui.label("To link WhatsApp, first add and verify your phone number in your profile — only your own verified number can be linked.").classes("text-sm text-amber-700")
            ui.link("Open profile", "/profile")

        @guard
        def link():
            a = core().agents.link_channel(agent["agent_id"], "WHATSAPP", owner.get("phone") or "")
            linked.set_text(", ".join(f"{c['channel']} {c['external_id']}" for c in a["channel_identity_map"]))
            ui.notify("Linked", type="positive")
        ui.button("Link WhatsApp", icon="link", on_click=link).props("outline")


def talk_tab(agent: dict) -> None:
    with ui.card().classes("w-full"):
        ui.label("Talk to your agent").classes("font-medium")
        ui.label("Commands: status · pause · resume · ceiling 50000 · approve <id> · reject <id> · summary · help").classes("text-xs opacity-70")

        @ui.refreshable
        def log():
            with ui.column().classes("w-full max-h-72 overflow-auto gap-1 border rounded p-2"):
                for x in agent["durable_memory"]["conversation"][-15:]:
                    ui.label(f"{'You' if x['role'] == 'user' else 'Agent'} ({x['channel'].lower()}): {x['text']}").classes("text-sm " + ("text-primary" if x["role"] == "user" else ""))
        log()
        box = ui.input(placeholder="status").props("outlined dense").classes("w-full")

        @guard
        async def send():
            if not box.value.strip():
                return
            await core().router.converse(agent, "WEB", box.value)
            box.set_value("")
            log.refresh()
        box.on("keydown.enter", send)
        ui.button("Send", icon="send", on_click=send).props("unelevated color=primary")
