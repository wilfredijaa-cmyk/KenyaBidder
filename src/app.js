import { Store } from './store.js';
import { SystemClock } from './clock.js';
import { AuditLog } from './harness/audit.js';
import { AuctionEngine } from './mcp/auctionEngine.js';
import { MarketIntel } from './mcp/marketIntel.js';
import { MatchService } from './mcp/matchService.js';
import { buildMcpServers } from './mcp/registry.js';
import { GuardrailInterceptor } from './harness/guardrail.js';
import { ExecutionEngine } from './harness/execution.js';
import { Orchestrator } from './harness/orchestrator.js';
import { ChannelRouter, WhatsAppAdapter } from './harness/channels.js';
import { AgentService } from './agents/agentService.js';
import { SellerService } from './agents/sellerService.js';
import { computeKpis } from './kpis.js';
import { recomputeReputation } from './reputation.js';

/**
 * Composition root. Layering (spec §6): channels → harness (guardrail,
 * execution, orchestrator) → MCP services. The LLM strategy lives behind the
 * orchestrator and can only ever *propose*.
 */
export function createApp({ store = new Store(), clock = new SystemClock(), guardrailConfig = {}, strategies = null, transport = null, whatsapp = null, matchTtlMs } = {}) {
  const router = new ChannelRouter({ store, clock, whatsapp: whatsapp ?? new WhatsAppAdapter({ store, clock }) });
  const notify = (...args) => router.notify(...args);

  const engine = new AuctionEngine({ store, clock });
  const intel = new MarketIntel({ store, clock, engine });
  const audit = new AuditLog({ store, clock });
  const guardrail = new GuardrailInterceptor({ store, clock, engine, intel, config: guardrailConfig });
  const execution = new ExecutionEngine({ store, clock, engine, guardrail, audit, notify, transport });
  const matches = new MatchService({ store, clock, engine, notify, matchTtlMs });
  const sellers = new SellerService({ store, clock, engine, notify });
  const orchestrator = new Orchestrator({ store, clock, engine, intel, execution, notify, strategies });
  const agents = new AgentService({ store, clock, execution });
  const mcp = buildMcpServers({ engine, matches, intel });

  const status = (agent) => {
    const triggers = execution.triggersFor(agent.agent_id).filter((t) => ['ACTIVE', 'AWAITING_APPROVAL'].includes(t.status));
    const pending = [...store.approvals.values()].filter((a) => a.agentId === agent.agent_id && a.status === 'PENDING');
    const c = agent.constraints;
    const lines = [`${agent.agent_type} agent is ${agent.status}.`];
    if (agent.agent_type === 'BIDDER') lines.push(`Ceiling ${c.budget_ceiling} KES, ${triggers.length} active plan(s), ${pending.length} approval(s) pending.`);
    else lines.push(sellers.summary(agent.agent_id));
    lines.push(`Reputation: ${agent.reputation.tier} (score ${agent.reputation.score}).`);
    return lines.join(' ');
  };

  router.bind({
    findByChannel: (ch, id) => agents.findByChannel(ch, id),
    setStatus: (id, s) => agents.setStatus(id, s),
    update: (id, patch) => agents.update(id, patch),
    findApproval: (id, prefix) => orchestrator.findApproval(id, prefix),
    resolveApproval: (id, approve) => orchestrator.resolveApproval(id, approve),
    sellerSummary: (id) => sellers.summary(id),
    status,
  });

  /** Periodic maintenance: advance clocks, evaluate time-based triggers, expire stale matches. */
  function tick() {
    engine.tick();
    execution.evaluateAll();
    matches.expireStale();
  }

  return {
    store, clock, engine, intel, audit, guardrail, execution, matches, sellers, orchestrator, agents, router, mcp,
    tick, status, kpis: () => computeKpis(store), recomputeReputation: (id) => recomputeReputation(store, id),
  };
}
