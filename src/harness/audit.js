import { randomUUID } from 'node:crypto';

/** Append-only audit log (spec §7.3, §10 AuditEntry). Entries are frozen once written. */
export class AuditLog {
  constructor({ store, clock }) {
    this.store = store;
    this.clock = clock;
  }

  record({ agentId, auctionId = null, proposedAction, guardrailDecision, rejectionReason = null, executedAction = null, executionResult = null }) {
    const entry = Object.freeze({
      entry_id: randomUUID(),
      agent_id: agentId,
      auction_id: auctionId,
      proposed_action: proposedAction,
      guardrail_decision: guardrailDecision,
      rejection_reason: rejectionReason,
      executed_action: executedAction,
      execution_result: executionResult,
      timestamp: this.clock.now(),
    });
    this.store.audit.push(entry);
    return entry;
  }

  forAgent(agentId, limit = 100) {
    const out = [];
    for (let i = this.store.audit.length - 1; i >= 0 && out.length < limit; i--) {
      if (this.store.audit[i].agent_id === agentId) out.push(this.store.audit[i]);
    }
    return out;
  }
}
