import { isOpen } from '../mcp/auctionEngine.js';

export const DEFAULT_GUARDRAIL_CONFIG = Object.freeze({
  maxProposalsPerMinute: 60,
  /** Max single-bid value for NEW-tier agents ("low-stakes pool", §11.1). */
  lowStakesCeiling: 1_000_000,
  anomalyMultiple: 3,
  anomalyMinSamples: 5,
  breakerCooldownMs: 10 * 60_000,
});

/**
 * Guardrail Interceptor (spec §7.3, §Security). A deterministic rules engine —
 * never an LLM. Every proposed action passes through here before any
 * state-changing call. Decisions: APPROVED | REJECTED | ESCALATED.
 */
export class GuardrailInterceptor {
  constructor({ store, clock, engine, intel, config = {} }) {
    this.store = store;
    this.clock = clock;
    this.engine = engine;
    this.intel = intel;
    this.config = { ...DEFAULT_GUARDRAIL_CONFIG, ...config };
  }

  /**
   * proposal: { agentId, auctionId, action, amount, source, approvedUpTo? }
   * source: 'strategy' (autonomous) | 'user' (explicit human decision) | 'approval'
   */
  evaluate(p) {
    const reject = (code, reason, extra = {}) => ({ decision: 'REJECTED', code, reason, ...extra });
    const escalate = (code, reason) => ({ decision: 'ESCALATED', code, reason });
    const now = this.clock.now();

    const agent = this.store.agents.get(p.agentId);
    if (!agent) return reject('AGENT_NOT_FOUND', 'unknown agent', { permanent: true });
    if (agent.status !== 'ACTIVE') return reject('AGENT_NOT_ACTIVE', `agent is ${agent.status}`, { permanent: true });
    if (agent.agent_type !== 'BIDDER') return reject('NOT_A_BIDDER', 'only bidder agents may bid', { permanent: true });
    if (!Number.isInteger(p.amount) || p.amount <= 0) return reject('INVALID_AMOUNT', 'amount must be a positive integer', { permanent: true });

    const c = agent.constraints;
    if (p.amount > c.budget_ceiling) return reject('CEILING_EXCEEDED', `amount ${p.amount} exceeds budget ceiling ${c.budget_ceiling}`, { permanent: true });

    const a = this.store.auctions.get(p.auctionId);
    if (!a) return reject('AUCTION_NOT_FOUND', 'unknown auction', { permanent: true });
    if (!isOpen(a) && a.status !== 'SCHEDULED') return reject('AUCTION_NOT_OPEN', `auction is ${a.status}`, { permanent: true });

    if (this._breakerActive(a.productSpec.category, now)) {
      return reject('MARKET_ANOMALY', 'autonomous bidding halted for this category (circuit breaker)', { permanent: true, notify: true });
    }
    if (this._isAnomalous(a, p.amount)) {
      this._trip(a.productSpec.category, `bid ${p.amount} is more than ${this.config.anomalyMultiple}x the historical median`, now);
      return reject('MARKET_ANOMALY', 'price is anomalous versus recent clearing prices; autonomous bidding halted', { permanent: true, notify: true });
    }

    const oneOff = agent.durable_memory?.oneOffAuthorizations ?? [];
    if (!c.authorized_auction_types.includes(a.auctionType) && !oneOff.includes(a.auctionId) && p.source === 'strategy') {
      return escalate('TYPE_NOT_AUTHORIZED', `${a.auctionType} auctions are not pre-authorized for autonomous bidding`);
    }

    const tier = agent.reputation?.tier ?? 'NEW';
    if (tier === 'NEW' && p.amount > this.config.lowStakesCeiling) {
      return reject('TIER_LIMIT', `NEW-tier agents are limited to ${this.config.lowStakesCeiling} per bid until they complete matches`, { permanent: true });
    }

    if (p.source !== 'user') {
      const threshold = (c.budget_ceiling * c.escalation_threshold_pct) / 100;
      if (p.amount > threshold && !(p.approvedUpTo >= p.amount)) {
        return escalate('ESCALATION_THRESHOLD', `amount ${p.amount} exceeds ${c.escalation_threshold_pct}% of the budget ceiling`);
      }
    }

    const window = (this.store.rateWindows.get(p.agentId) ?? []).filter((t) => now - t < 60_000);
    if (window.length >= this.config.maxProposalsPerMinute) {
      this.store.rateWindows.set(p.agentId, window);
      return reject('RATE_LIMIT', `more than ${this.config.maxProposalsPerMinute} actions in the last minute`);
    }
    window.push(now);
    this.store.rateWindows.set(p.agentId, window);

    return { decision: 'APPROVED', code: 'OK', reason: 'passed all guardrails' };
  }

  _isAnomalous(a, amount) {
    if (a.auctionType === 'DUTCH') return false; // Dutch price is set by the seller's descending schedule
    const s = this.intel.getHistoricalClearingPrices({ category: a.productSpec.category });
    return s.count >= this.config.anomalyMinSamples && s.median > 0 && amount > s.median * this.config.anomalyMultiple;
  }

  _breakerActive(category, now) {
    const b = this.store.breakers.get(category.toLowerCase());
    if (!b) return false;
    if (b.until <= now) {
      this.store.breakers.delete(category.toLowerCase());
      return false;
    }
    return true;
  }

  _trip(category, reason, now) {
    this.store.breakers.set(category.toLowerCase(), { reason, trippedAt: now, until: now + this.config.breakerCooldownMs });
  }

  resetBreaker(category) {
    this.store.breakers.delete(category.toLowerCase());
  }
}

