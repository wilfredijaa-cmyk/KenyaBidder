import { randomUUID } from 'node:crypto';
import { performance } from 'node:perf_hooks';
import { badRequest, forbidden, notFound } from '../errors.js';
import { effectiveEnd, isOpen } from '../mcp/auctionEngine.js';

const KIND_FOR_TYPE = {
  ENGLISH: 'ENGLISH_INCREMENTAL',
  DUTCH: 'DUTCH_ACCEPT',
  FIRST_PRICE_SEALED: 'SEALED_BID',
  SECOND_PRICE_SEALED: 'SEALED_BID',
};

/** Direct in-process transport. Swap for a network client (or a flaky test double). */
export class DirectTransport {
  constructor(engine) {
    this.engine = engine;
  }
  submitBid(args) {
    return this.engine.submitBid(args);
  }
}

export class TransientError extends Error {
  constructor(msg = 'transient network failure') {
    super(msg);
    this.transient = true;
  }
}

/**
 * Deterministic Execution Engine (spec §7.2). Holds pre-approved conditional
 * actions ("triggers") and fires them the instant conditions are met. No LLM
 * call is ever made from this module — that is the point.
 *
 * Trigger kinds:
 *   ENGLISH_INCREMENTAL {maxBid, incrementPct, snipeWindowMs}
 *   DUTCH_ACCEPT        {threshold}          accept when price <= threshold
 *   SEALED_BID          {amount}
 */
export class ExecutionEngine {
  constructor({ store, clock, engine, guardrail, audit, notify = () => {}, transport = null, maxRetries = 3 }) {
    this.store = store;
    this.clock = clock;
    this.engine = engine;
    this.guardrail = guardrail;
    this.audit = audit;
    this.notify = notify;
    this.transport = transport ?? new DirectTransport(engine);
    this.maxRetries = maxRetries;
    this._evaluating = new Set();

    // Event-driven: re-evaluate an auction's triggers whenever it changes.
    for (const ev of ['auction.bid', 'auction.price', 'auction.extended', 'auction.started', 'auction.settled', 'auction.cancelled']) {
      engine.on(ev, ({ auctionId }) => this.evaluateAuction(auctionId));
    }
  }

  // ---------- trigger management ----------

  registerTrigger({ agentId, auctionId, kind, params, reasoning = '', source = 'strategy' }) {
    const agent = this.store.agents.get(agentId);
    if (!agent || agent.agent_type !== 'BIDDER') throw badRequest('INVALID_AGENT', 'bidder agent required');
    if (agent.status !== 'ACTIVE') throw forbidden('AGENT_NOT_ACTIVE', 'agent is not active');
    const a = this.store.auctions.get(auctionId);
    if (!a) throw notFound('AUCTION_NOT_FOUND', 'auction not found');
    if (KIND_FOR_TYPE[a.auctionType] !== kind) throw badRequest('KIND_MISMATCH', `${kind} cannot be used on a ${a.auctionType} auction`);
    validateParams(kind, params);

    for (const t of this.store.triggers.values()) {
      if (t.agentId === agentId && t.auctionId === auctionId && ['ACTIVE', 'AWAITING_APPROVAL'].includes(t.status)) {
        t.status = 'CANCELLED';
      }
    }
    const t = {
      id: randomUUID(),
      agentId,
      auctionId,
      kind,
      params: { ...params },
      status: 'ACTIVE',
      reasoning,
      source,
      firedCount: 0,
      approvalId: null,
      approvedUpTo: 0,
      lastRejection: null,
      lastError: null,
      createdAt: this.clock.now(),
    };
    this.store.triggers.set(t.id, t);
    this.evaluateAuction(auctionId);
    return t;
  }

  cancelAgentTriggers(agentId, reason = 'authority revoked') {
    let n = 0;
    for (const t of this.store.triggers.values()) {
      if (t.agentId === agentId && ['ACTIVE', 'AWAITING_APPROVAL'].includes(t.status)) {
        t.status = 'CANCELLED';
        t.lastError = reason;
        n++;
      }
    }
    return n;
  }

  triggersFor(agentId) {
    return [...this.store.triggers.values()].filter((t) => t.agentId === agentId);
  }

  // ---------- evaluation loop ----------

  evaluateAll() {
    const ids = new Set();
    for (const t of this.store.triggers.values()) if (t.status === 'ACTIVE') ids.add(t.auctionId);
    for (const id of ids) this.evaluateAuction(id);
  }

  /**
   * Evaluate to a fixed point. Events raised by our own bids are absorbed by
   * the loop (guarded by _evaluating) instead of recursing.
   */
  evaluateAuction(auctionId) {
    if (this._evaluating.has(auctionId)) return;
    this._evaluating.add(auctionId);
    try {
      for (let i = 0; i < 10_000; i++) {
        let fired = false;
        for (const t of this.store.triggers.values()) {
          if (t.auctionId === auctionId && t.status === 'ACTIVE') fired = this._evaluate(t) || fired;
        }
        if (!fired) break;
      }
    } finally {
      this._evaluating.delete(auctionId);
    }
  }

  _evaluate(t) {
    const a = this.store.auctions.get(t.auctionId);
    if (!a) {
      t.status = 'CANCELLED';
      return false;
    }
    this.engine._advance(a);
    if (a.status === 'SCHEDULED') return false;
    if (!isOpen(a)) {
      this._finish(t, a);
      return false;
    }
    const agent = this.store.agents.get(t.agentId);
    if (!agent || agent.status !== 'ACTIVE') {
      t.status = 'CANCELLED';
      t.lastError = 'agent not active';
      return false;
    }
    const now = this.clock.now();
    const p = t.params;

    if (t.kind === 'ENGLISH_INCREMENTAL') {
      if (a.bids.at(-1)?.agentId === t.agentId) return false; // already winning
      const needed = this.engine.minNextBid(a, now);
      if (needed > p.maxBid) {
        t.status = 'EXHAUSTED';
        this.notify(t.agentId, 'outbid', `Outbid on "${a.productSpec.title}": the next valid bid (${needed}) is above your agent's limit (${p.maxBid}).`, { auctionId: a.auctionId });
        return false;
      }
      if (p.snipeWindowMs > 0 && effectiveEnd(a) - now > p.snipeWindowMs) return false;
      const last = a.bids.at(-1);
      const stepped = last ? Math.ceil(last.amount * (1 + (p.incrementPct ?? 0) / 100)) : needed;
      const amount = Math.min(p.maxBid, Math.max(needed, stepped));
      return this._fire(t, a, amount);
    }

    if (t.kind === 'DUTCH_ACCEPT') {
      const price = this.engine.currentPrice(a, now);
      if (price > p.threshold) return false;
      return this._fire(t, a, price);
    }

    if (t.kind === 'SEALED_BID') return this._fire(t, a, p.amount);
    return false;
  }

  _finish(t, a) {
    t.status = 'DONE';
    if (a.status === 'SETTLED') {
      const won = a.result?.winnerAgentId === t.agentId;
      t.outcome = won ? 'WON' : 'LOST';
    }
  }

  // ---------- firing ----------

  _fire(t, a, amount) {
    const started = performance.now();
    const proposal = {
      agentId: t.agentId,
      auctionId: a.auctionId,
      action: a.auctionType.includes('SEALED') ? 'place_sealed_bid' : 'place_bid',
      amount,
      source: t.source,
      approvedUpTo: t.approvedUpTo,
    };
    const decision = this.guardrail.evaluate(proposal);

    if (decision.decision === 'REJECTED') {
      if (t.lastRejection !== decision.code) {
        t.lastRejection = decision.code;
        this.audit.record({ agentId: t.agentId, auctionId: a.auctionId, proposedAction: proposal, guardrailDecision: 'REJECTED', rejectionReason: `${decision.code}: ${decision.reason}` });
        if (decision.notify) this.notify(t.agentId, 'anomaly', `Autonomous bidding halted on "${a.productSpec.title}": ${decision.reason}. Please review and decide manually.`, { auctionId: a.auctionId });
      }
      if (decision.permanent) {
        t.status = 'BLOCKED';
        t.lastError = decision.code;
      }
      return false;
    }

    if (decision.decision === 'ESCALATED') {
      const approval = this._createApproval(t, a, proposal, decision);
      t.status = 'AWAITING_APPROVAL';
      t.approvalId = approval.id;
      this.audit.record({ agentId: t.agentId, auctionId: a.auctionId, proposedAction: proposal, guardrailDecision: 'ESCALATED', rejectionReason: `${decision.code}: ${decision.reason}` });
      this.notify(t.agentId, 'approval_needed', `Approval needed: bid ${amount} on "${a.productSpec.title}" (${decision.reason}). Reply "approve ${approval.id.slice(0, 8)}" or "reject ${approval.id.slice(0, 8)}".`, { approvalId: approval.id, auctionId: a.auctionId });
      return false;
    }

    t.lastRejection = null;
    const key = `${t.id}:${t.firedCount}`;
    const res = this._submitWithRetry({ auctionId: a.auctionId, agentId: t.agentId, amount, idempotencyKey: key });
    const latencyMs = Math.round((performance.now() - started) * 1000) / 1000;
    this.audit.record({
      agentId: t.agentId,
      auctionId: a.auctionId,
      proposedAction: proposal,
      guardrailDecision: 'APPROVED',
      executedAction: { action: proposal.action, amount, idempotencyKey: key },
      executionResult: { ok: res.ok, code: res.code ?? 'OK', reconciled: !!res.reconciled, latencyMs },
    });

    if (res.ok) {
      t.firedCount++;
      if (t.kind !== 'ENGLISH_INCREMENTAL') t.status = 'DONE';
      return t.kind === 'ENGLISH_INCREMENTAL';
    }
    if (res.code === 'AUCTION_NOT_OPEN') {
      t.status = 'DONE';
    } else if (!['BID_TOO_LOW', 'ALREADY_HIGHEST'].includes(res.code)) {
      t.status = 'BLOCKED';
      t.lastError = res.code;
    }
    return false;
  }

  /**
   * Network-drop safe submission (spec §12): never blindly resubmit. After a
   * transport failure, query the authoritative state; only retry if the bid
   * did not land. The idempotency key makes the retry itself safe.
   */
  _submitWithRetry(args) {
    for (let attempt = 0; ; attempt++) {
      try {
        return this.transport.submitBid(args);
      } catch (e) {
        if (!e.transient) return { ok: false, code: 'EXECUTION_ERROR', message: e.message };
        const a = this.store.auctions.get(args.auctionId);
        if (a?.bids.some((b) => b.idempotencyKey === args.idempotencyKey)) {
          return { ok: true, reconciled: true };
        }
        if (attempt >= this.maxRetries) return { ok: false, code: 'TRANSPORT_FAILURE', message: e.message };
      }
    }
  }

  // ---------- approvals (request_user_approval) ----------

  _createApproval(t, a, proposal, decision) {
    const approval = {
      id: randomUUID(),
      kind: 'BID',
      agentId: t.agentId,
      auctionId: a.auctionId,
      triggerId: t.id,
      proposal,
      code: decision.code,
      reason: decision.reason,
      status: 'PENDING',
      createdAt: this.clock.now(),
    };
    this.store.approvals.set(approval.id, approval);
    return approval;
  }

  resolveApproval(approvalId, approve) {
    const ap = this.store.approvals.get(approvalId);
    if (!ap) throw notFound('APPROVAL_NOT_FOUND', 'approval not found');
    if (ap.status !== 'PENDING') throw badRequest('APPROVAL_RESOLVED', `approval already ${ap.status}`);
    ap.status = approve ? 'APPROVED' : 'REJECTED';
    ap.resolvedAt = this.clock.now();
    const t = this.store.triggers.get(ap.triggerId);
    if (t && t.status === 'AWAITING_APPROVAL') {
      if (approve) {
        if (ap.code === 'TYPE_NOT_AUTHORIZED') {
          const agent = this.store.agents.get(ap.agentId);
          const mem = (agent.durable_memory.oneOffAuthorizations ??= []);
          if (!mem.includes(ap.auctionId)) mem.push(ap.auctionId);
        }
        t.approvedUpTo = Math.max(t.approvedUpTo, ap.proposal.amount);
        t.status = 'ACTIVE';
        t.lastRejection = null;
      } else {
        t.status = 'CANCELLED';
        t.lastError = 'rejected by user';
      }
    }
    if (approve) this.evaluateAuction(ap.auctionId);
    return ap;
  }
}

function validateParams(kind, p) {
  if (!p || typeof p !== 'object') throw badRequest('INVALID_PARAMS', 'params required');
  const posInt = (n) => Number.isInteger(n) && n > 0;
  if (kind === 'ENGLISH_INCREMENTAL') {
    if (!posInt(p.maxBid)) throw badRequest('INVALID_PARAMS', 'maxBid must be a positive integer');
    if (p.incrementPct != null && !(p.incrementPct >= 0 && p.incrementPct <= 100)) throw badRequest('INVALID_PARAMS', 'incrementPct must be 0-100');
    if (p.snipeWindowMs != null && !(Number.isInteger(p.snipeWindowMs) && p.snipeWindowMs >= 0)) throw badRequest('INVALID_PARAMS', 'snipeWindowMs must be >= 0');
  } else if (kind === 'DUTCH_ACCEPT') {
    if (!posInt(p.threshold)) throw badRequest('INVALID_PARAMS', 'threshold must be a positive integer');
  } else if (kind === 'SEALED_BID') {
    if (!posInt(p.amount)) throw badRequest('INVALID_PARAMS', 'amount must be a positive integer');
  } else {
    throw badRequest('INVALID_KIND', `unknown trigger kind ${kind}`);
  }
}
