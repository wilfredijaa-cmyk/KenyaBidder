"""Composition root. Layering (spec §6): channels → harness (guardrail, execution, orchestrator) → MCP services.

The LLM strategy lives behind the orchestrator and can only ever *propose*.
"""
from __future__ import annotations

from types import SimpleNamespace

from .agents import AgentService
from .audit import AuditLog
from .channels import ChannelRouter, WhatsAppAdapter
from .clock import SystemClock
from .engine import AuctionEngine
from .events import Events
from .execution import ExecutionEngine
from .guardrail import GuardrailInterceptor
from .intel import MarketIntel
from .kb import KnowledgeBaseService
from .kpis import compute_kpis
from .llm.registry import LlmRegistry
from .matches import MatchService
from .mcpx.manager import McpManager
from .mcpx.server import build_server
from .orchestrator import Orchestrator
from .reputation import recompute_reputation
from .seller import SellerService
from .store import Store
from .strategy import BaselineStrategy, HeuristicStrategy, LlmStrategy, SellerAdvisor


def create_app(*, store=None, clock=None, guardrail_config=None, transport=None, whatsapp=None, match_ttl_ms=None,
               llm_provider_factory=None, allow_stdio=None) -> SimpleNamespace:
    store = store or Store()
    clock = clock or SystemClock()
    events = Events()
    router = ChannelRouter(store, clock, whatsapp or WhatsAppAdapter(store, clock))
    notify = router.notify

    engine = AuctionEngine(store, clock, events)
    intel = MarketIntel(store, clock, engine)
    audit = AuditLog(store, clock)
    guardrail = GuardrailInterceptor(store, clock, engine, intel, guardrail_config)
    execution = ExecutionEngine(store, clock, engine, guardrail, audit, notify, transport)
    matches = MatchService(store, clock, engine, notify, **({"match_ttl_ms": match_ttl_ms} if match_ttl_ms else {}))
    sellers = SellerService(store, clock, engine, notify)

    llms = LlmRegistry(store, clock, llm_provider_factory)
    kb = KnowledgeBaseService(store, clock)
    app = SimpleNamespace(store=store, clock=clock, events=events, engine=engine, intel=intel, audit=audit,
                          guardrail=guardrail, execution=execution, matches=matches, sellers=sellers, llms=llms, kb=kb,
                          router=router)
    app.mcps = McpManager(store, clock, builtin_factory=lambda: build_server(app, internal=False), allow_stdio=allow_stdio)
    app.mcps.ensure_builtin()

    strategies = {"baseline": BaselineStrategy(), "heuristic": HeuristicStrategy()}
    strategies["llm"] = LlmStrategy(llms, app.mcps, kb, store, fallback=strategies["heuristic"])
    app.orchestrator = Orchestrator(store, clock, engine, intel, execution, strategies, notify)
    app.advisor = SellerAdvisor(intel, llms, app.mcps, kb, store)
    app.agents = AgentService(store, clock, execution, llms, app.mcps, kb)

    def status(agent: dict) -> str:
        live = [t for t in execution.triggers_for(agent["agent_id"]) if t["status"] in ("ACTIVE", "AWAITING_APPROVAL")]
        pending = [a for a in store.approvals.values() if a["agent_id"] == agent["agent_id"] and a["status"] == "PENDING"]
        c = agent["constraints"]
        lines = [f"{agent['agent_type']} agent is {agent['status']}."]
        if agent["agent_type"] == "BIDDER":
            lines.append(f"Ceiling {c['budget_ceiling']} KES, {len(live)} active plan(s), {len(pending)} approval(s) pending.")
        else:
            lines.append(sellers.summary(agent["agent_id"]))
        r = agent["reputation"]
        lines.append(f"Reputation: {r['tier']} (score {r['score']}).")
        return " ".join(lines)

    app.status = status
    router.bind(SimpleNamespace(
        find_by_channel=app.agents.find_by_channel, set_status=app.agents.set_status,
        update=lambda agent_id, constraints=None: app.agents.update(agent_id, constraints=constraints),
        find_approval=app.orchestrator.find_approval, resolve_approval=app.orchestrator.resolve_approval,
        seller_summary=sellers.summary, status=status))

    def tick() -> None:
        """Periodic maintenance: advance clocks, evaluate time-based triggers, expire stale matches."""
        engine.tick()
        execution.evaluate_all()
        matches.expire_stale()

    app.tick = tick
    app.kpis = lambda: compute_kpis(store)
    app.recompute_reputation = lambda agent_id: recompute_reputation(store, agent_id)
    return app
