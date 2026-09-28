import { randomUUID } from 'node:crypto';
import { effectiveEnd, isOpen } from '../mcp/auctionEngine.js';
import { BaselineStrategy, HeuristicStrategy, LlmStrategy } from '../agents/strategy.js';

/**
 * Session Manager / LLM Orchestrator (spec §7.3, §11.3): reacts to auction
 * events, runs the deterministic pre-filter, asks the strategy layer for a
 * *proposal*, and hands it to the Execution Engine as a conditional trigger.
 */
export class Orchestrator {
  constructor({ store, clock, engine, intel, execution, notify = () => {}, strategies = null }) {
    this.store = store;
    this.clock = clock;
    this.engine = engine;
    this.intel = intel;
    this.execution = execution;
    this.notify = notify;
    const heuristic = new HeuristicStrategy();
    this.strategies = strategies ?? {
      baseline: new BaselineStrategy(),
      heuristic,
      llm: new LlmStrategy({ fallback: heuristic, getUser: (agent) => this.store.users.get(agent.principal_user_id) ?? { name: 'the user' } }),
    };
    this.pending = new Set();
    engine.on('auction.created', ({ auctionId }) => this._track(this.onAuctionCreated(auctionId)));
  }

  _track(p) {
    this.pending.add(p);
    p.catch((e) => console.error('[orchestrator]', e)).finally(() => this.pending.delete(p));
  }

  /** Await all in-flight strategy calls (tests / graceful shutdown). */
  async idle() {
    while (this.pending.size) await Promise.allSettled([...this.pending]);
  }

  async onAuctionCreated(auctionId) {
    for (const agent of this.store.agents.values()) {
      if (agent.agent_type !== 'BIDDER' || agent.status !== 'ACTIVE') continue;
      const mem = agent.durable_memory;
      if (!mem.autoBid || !mem.watch) continue;
      await this.consider(agent.agent_id, auctionId);
    }
  }

  /** Deterministic pre-filter (§11.3). Returns null if eligible, else a human-readable reason. */
  prefilter(agent, a, { spec = true } = {}) {
    const w = spec ? agent.durable_memory.watch : null;
    const now = this.clock.now();
    if (agent.status !== 'ACTIVE') return 'agent not active';
    if (a.status === 'SETTLED' || a.status === 'CANCELLED') return `auction is ${a.status}`;
    if (!isOpen(a) && a.status !== 'SCHEDULED') return `auction is ${a.status}`;
    const seller = this.store.agents.get(a.sellerAgentId);
    if (seller?.principal_user_id === agent.principal_user_id) return 'own listing';
    if (w) {
      if (w.category && w.category.toLowerCase() !== a.productSpec.category.toLowerCase()) return 'category mismatch';
      if (w.minQuantity && a.productSpec.quantity < w.minQuantity) return 'quantity below minimum';
      if (w.deadlineAt && effectiveEnd(a) > w.deadlineAt) return 'auction closes after the delivery deadline';
      if (w.keywords?.length) {
        const text = `${a.productSpec.title} ${a.productSpec.description ?? ''}`.toLowerCase();
        if (!w.keywords.some((k) => text.includes(k.toLowerCase()))) return 'keywords do not match';
      }
    }
    const openingPrice = a.auctionType === 'DUTCH' ? a.dutch.floorPrice : this.engine.minNextBid(a, now);
    if (openingPrice > agent.constraints.budget_ceiling) return 'cheapest possible price is above the budget ceiling';
    return null;
  }

  /**
   * Run the agent's strategy for an auction and register the resulting trigger.
   * `manual` skips the watch-spec filter (user explicitly asked) but never the hard checks.
   */
  async consider(agentId, auctionId, { manual = false } = {}) {
    const agent = this.store.agents.get(agentId);
    const a = this.store.auctions.get(auctionId);
    if (!agent || !a) return { status: 'ERROR', reason: 'unknown agent or auction' };
    const key = `${agentId}:${auctionId}`;
    if (!manual && this.store.considered.has(key)) return { status: 'SKIPPED', reason: 'already considered' };

    const why = this.prefilter(agent, a, { spec: !manual });
    if (why) return { status: 'FILTERED', reason: why };
    this.store.considered.add(key);
    // An explicit user request counts as authorization for this one auction.
    if (manual && !(agent.durable_memory.oneOffAuthorizations ??= []).includes(auctionId)) agent.durable_memory.oneOffAuthorizations.push(auctionId);

    // Auction types outside the pre-authorized list need explicit approval (hard constraint #2).
    const oneOff = agent.durable_memory.oneOffAuthorizations ?? [];
    if (!agent.constraints.authorized_auction_types.includes(a.auctionType) && !oneOff.includes(a.auctionId) && !manual) {
      return this._requestParticipation(agent, a);
    }

    const stats = this.intel.getHistoricalClearingPrices({ category: a.productSpec.category });
    const strat = this.strategies[agent.durable_memory.strategy] ?? this.strategies.heuristic;
    const proposal = await strat.propose({ agent, auction: a, intel: { stats } });

    // Re-check after the (possibly slow) strategy call — the world may have moved on.
    const fresh = this.store.agents.get(agentId);
    if (fresh.status !== 'ACTIVE') return { status: 'CANCELLED', reason: 'agent no longer active' };
    if (!isOpen(this.engine.getAuction(auctionId)) && this.engine.getAuction(auctionId).status !== 'SCHEDULED') return { status: 'FILTERED', reason: 'auction closed while deciding' };

    if (proposal.action === 'SKIP') {
      this.notify(agentId, 'skip', `Skipping "${a.productSpec.title}": ${proposal.reasoning}`, { auctionId });
      return { status: 'SKIPPED', reason: proposal.reasoning };
    }
    const trigger = this.execution.registerTrigger({ agentId, auctionId, kind: proposal.kind, params: proposal.params, reasoning: proposal.reasoning, source: 'strategy' });
    this.notify(agentId, 'plan', `Plan for "${a.productSpec.title}" (${a.auctionType}): ${proposal.reasoning}`, { auctionId, triggerId: trigger.id });
    return { status: 'PLANNED', trigger, reasoning: proposal.reasoning };
  }

  _requestParticipation(agent, a) {
    const approval = {
      id: randomUUID(),
      kind: 'PARTICIPATE',
      agentId: agent.agent_id,
      auctionId: a.auctionId,
      triggerId: null,
      proposal: null,
      code: 'TYPE_NOT_AUTHORIZED',
      reason: `${a.auctionType} auctions are not pre-authorized for autonomous bidding`,
      status: 'PENDING',
      createdAt: this.clock.now(),
    };
    this.store.approvals.set(approval.id, approval);
    this.notify(agent.agent_id, 'approval_needed', `"${a.productSpec.title}" is a ${a.auctionType} auction, which you have not pre-authorized. Reply "approve ${approval.id.slice(0, 8)}" to let the agent participate, or "reject ${approval.id.slice(0, 8)}".`, { approvalId: approval.id, auctionId: a.auctionId });
    return { status: 'APPROVAL_REQUESTED', approvalId: approval.id };
  }

  /** Resolve any approval kind. BID approvals go to the executor; PARTICIPATE approvals start the strategy. */
  async resolveApproval(approvalId, approve) {
    const ap = this.store.approvals.get(approvalId);
    if (ap?.kind !== 'PARTICIPATE') return this.execution.resolveApproval(approvalId, approve);
    if (ap.status !== 'PENDING') return ap;
    ap.status = approve ? 'APPROVED' : 'REJECTED';
    ap.resolvedAt = this.clock.now();
    if (approve) {
      await this.consider(ap.agentId, ap.auctionId, { manual: true });
    }
    return ap;
  }

  /** Match a partial (8-char) approval id as used in chat channels. */
  findApproval(agentId, idOrPrefix) {
    const pending = [...this.store.approvals.values()].filter((a) => a.agentId === agentId && a.status === 'PENDING');
    return pending.find((a) => a.id === idOrPrefix || a.id.startsWith(idOrPrefix)) ?? null;
  }
}
