"""Composition root. Layering (spec §6): channels → harness (guardrail, execution, orchestrator) → MCP services.

The LLM strategy lives behind the orchestrator and can only ever *propose*.
"""
from __future__ import annotations

import os
import secrets
import uuid
from types import SimpleNamespace

from .agents import AgentService
from .audit import AuditLog
from .billing import BillingService
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
from .payments import MpesaClient, MpesaConfig
from .reputation import recompute_reputation
from .seller import SellerService
from .store import Store
from .wallet import TokenMeter, WalletDB
from .strategy import BaselineStrategy, HeuristicStrategy, LlmStrategy, SellerAdvisor


def create_app(*, store=None, clock=None, guardrail_config=None, transport=None, whatsapp=None, match_ttl_ms=None,
               llm_provider_factory=None, allow_stdio=None, report_grace_ms=None, wallet_path=":memory:", listing_limits=None, mpesa_client=None, dev_payments=None) -> SimpleNamespace:
    store = store or Store()
    clock = clock or SystemClock()
    events = Events()
    router = ChannelRouter(store, clock, whatsapp or WhatsAppAdapter(store, clock))
    notify = router.notify

    engine = AuctionEngine(store, clock, events, listing_limits)
    intel = MarketIntel(store, clock, engine)
    audit = AuditLog(store, clock)
    guardrail = GuardrailInterceptor(store, clock, engine, intel, guardrail_config)
    execution = ExecutionEngine(store, clock, engine, guardrail, audit, notify, transport)
    matches = MatchService(store, clock, engine, notify, **({"match_ttl_ms": match_ttl_ms} if match_ttl_ms else {}), **({"report_grace_ms": report_grace_ms} if report_grace_ms else {}))
    sellers = SellerService(store, clock, engine, notify)

    llms = LlmRegistry(store, clock, llm_provider_factory)
    wallet = WalletDB(wallet_path, clock)
    meter = TokenMeter(store, clock, wallet, llms, notify)
    kb = KnowledgeBaseService(store, clock)
    app = SimpleNamespace(store=store, clock=clock, events=events, engine=engine, intel=intel, audit=audit,
                          guardrail=guardrail, execution=execution, matches=matches, sellers=sellers, llms=llms, kb=kb,
                          router=router, wallet=wallet, meter=meter)
    app.mcps = McpManager(store, clock, builtin_factory=lambda: build_server(app, internal=False), allow_stdio=allow_stdio)
    app.mcps.ensure_builtin()

    strategies = {"baseline": BaselineStrategy(), "heuristic": HeuristicStrategy()}
    strategies["llm"] = LlmStrategy(llms, app.mcps, kb, store, fallback=strategies["heuristic"], meter=meter)
    app.orchestrator = Orchestrator(store, clock, engine, intel, execution, strategies, notify)
    app.advisor = SellerAdvisor(intel, llms, app.mcps, kb, store, meter)
    app.agents = AgentService(store, clock, execution, llms, app.mcps, kb)

    # ---- selling tokens (platform revenue; separate from auction settlement) ----
    if mpesa_client is None:
        cfg = MpesaConfig.from_env(fallback_secret=store.settings.setdefault("mpesa_callback_secret", secrets.token_urlsafe(24)))
        mpesa_client = MpesaClient(cfg, clock=None) if cfg else None
    if dev_payments is None:
        dev_payments = os.environ.get("KENYABIDDER_DEV_PAYMENTS") == "1"

    def on_credit(user_id: str, llm_id: str) -> None:
        """Tokens arrived: let the user's blocked agents pick their open auctions back up."""
        app.orchestrator.spawn(app.orchestrator.reconsider_user(user_id))

    app.billing = BillingService(store, clock, wallet, llms, meter, notify, mpesa_client, dev_payments, on_credit)
    app.agents.on_identity_change = app.billing.grant_signup_tokens

    def llm_deletion_blockers(llm_id: str) -> list[str]:
        out = []
        held = wallet.outstanding_liability().get(llm_id, 0)
        if held:
            out.append(f"customers still hold {held:,} tokens for it")
        if any(p["llm_id"] == llm_id for p in store.packs.values()):
            out.append("token packs are defined for it")
        if any(o["llm_id"] == llm_id for o in app.billing.list_orders(limit=100_000) if o["status"] in ("PENDING", "AWAITING_REVIEW")):
            out.append("there are unfinished payment orders for it")
        return out
    llms.deletion_blockers.append(llm_deletion_blockers)

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
        seller_summary=sellers.summary, status=status,
        owner_suspended=lambda agent: bool(store.users.get(agent["principal_user_id"], {}).get("suspended"))))

    def tick() -> None:
        """Periodic maintenance: advance clocks, evaluate time-based triggers, expire stale matches."""
        import logging
        for name, step in (("engine", engine.tick), ("triggers", execution.evaluate_all), ("matches", matches.expire_stale)):
            try:
                step()
            except Exception:  # noqa: BLE001  a failure in one step must never stop bids being evaluated
                logging.getLogger("kenyabidder").exception("tick step %s failed", name)

    def manual_bid(agent_id: str, auction_id: str, amount: int) -> dict:
        """Human override: an explicit user decision still passes the Guardrail Interceptor and is audited."""
        d = guardrail.evaluate({"agent_id": agent_id, "auction_id": auction_id, "action": "place_bid", "amount": amount, "source": "user"})
        result = None
        key = f"manual:{uuid.uuid4()}"  # unique per click — a timestamp would collide and silently replay the previous bid
        if d["decision"] == "APPROVED":
            result = execution._submit_with_retry(auction_id=auction_id, agent_id=agent_id, amount=amount, idempotency_key=key)
        audit.record(agent_id=agent_id, auction_id=auction_id, proposed_action={"action": "manual_bid", "amount": amount},
                     guardrail_decision=d["decision"], rejection_reason=None if d["decision"] == "APPROVED" else f"{d['code']}: {d['reason']}",
                     executed_action={"amount": amount, "idempotency_key": key} if result else None,
                     execution_result={"ok": result["ok"], "code": result.get("code", "OK")} if result else None)
        if d["decision"] != "APPROVED":
            return {"ok": False, "code": d["code"], "message": d["reason"]}
        return result

    app.manual_bid = manual_bid
    app.tick = tick
    app.kpis = lambda: compute_kpis(store)
    app.recompute_reputation = lambda agent_id: recompute_reputation(store, agent_id)
    return app
