import { randomUUID } from 'node:crypto';
import { badRequest, forbidden, notFound } from '../errors.js';
import { recomputeReputation } from '../reputation.js';

export const OUTCOMES = ['COMPLETED', 'FELL_THROUGH', 'NO_RESPONSE'];
const PRE_REVEAL = ['PROPOSED', 'SELLER_CONFIRMED', 'BUYER_CONFIRMED'];

/** Match / Introduction MCP backing service (spec §8.2, §11.2). No funds ever move here. */
export class MatchService {
  constructor({ store, clock, engine, notify = () => {}, matchTtlMs = 24 * 3600_000 }) {
    this.store = store;
    this.clock = clock;
    this.notify = notify;
    this.matchTtlMs = matchTtlMs;
    // create_match is triggered by the deterministic layer when an auction closes with a winner.
    engine.on('auction.settled', ({ auctionId, result }) => {
      if (result.outcome === 'SOLD') this.createMatch({ auctionId });
    });
  }

  createMatch({ auctionId }) {
    const a = this.store.auctions.get(auctionId);
    if (!a || a.result?.outcome !== 'SOLD') throw badRequest('NOT_SOLD', 'auction has no winner');
    for (const m of this.store.matches.values()) if (m.auction_id === auctionId) return m; // idempotent
    const now = this.clock.now();
    const m = {
      match_id: randomUUID(),
      auction_id: auctionId,
      seller_agent_id: a.sellerAgentId,
      buyer_agent_id: a.result.winnerAgentId,
      agreed_terms: { price: a.result.price, quantity: a.productSpec.quantity, title: a.productSpec.title, delivery_terms: null },
      status: 'PROPOSED',
      confirmations: { sellerAt: null, buyerAt: null },
      contact_reveal: null,
      outcome_reports: [],
      fault_agent_ids: [],
      created_at: now,
      updated_at: now,
    };
    this.store.matches.set(m.match_id, m);
    const msg = (who) => `Match created for "${a.productSpec.title}" at ${a.result.price} KES. Please confirm to exchange contact details — ${who}.`;
    this.notify(m.seller_agent_id, 'match', msg('you are the seller'), { matchId: m.match_id });
    this.notify(m.buyer_agent_id, 'match', msg('you won this auction'), { matchId: m.match_id });
    return m;
  }

  _party(m, agentId) {
    if (agentId === m.seller_agent_id) return 'seller';
    if (agentId === m.buyer_agent_id) return 'buyer';
    throw forbidden('NOT_A_PARTY', 'agent is not a party to this match');
  }

  getMatch(matchId, agentId = null) {
    const m = this.store.matches.get(matchId);
    if (!m) throw notFound('MATCH_NOT_FOUND', 'match not found');
    if (agentId) this._party(m, agentId);
    return m;
  }

  matchesFor(agentId) {
    return [...this.store.matches.values()].filter((m) => m.seller_agent_id === agentId || m.buyer_agent_id === agentId).sort((a, b) => b.created_at - a.created_at);
  }

  confirmMatch(matchId, agentId) {
    const m = this.getMatch(matchId, agentId);
    if (!PRE_REVEAL.includes(m.status)) throw badRequest('INVALID_STATE', `match is ${m.status}`);
    const side = this._party(m, agentId);
    const now = this.clock.now();
    if (!m.confirmations[`${side}At`]) m.confirmations[`${side}At`] = now;
    m.updated_at = now;
    if (m.confirmations.sellerAt && m.confirmations.buyerAt) {
      this.revealContact(matchId);
    } else {
      m.status = side === 'seller' ? 'SELLER_CONFIRMED' : 'BUYER_CONFIRMED';
      const other = side === 'seller' ? m.buyer_agent_id : m.seller_agent_id;
      this.notify(other, 'match', `The ${side} confirmed the match. Confirm to reveal contact details.`, { matchId });
    }
    return m;
  }

  /** Fires once both sides confirmed (default: immediate reveal, §11.2). Logged for bypass analytics. */
  revealContact(matchId) {
    const m = this.getMatch(matchId);
    if (!(m.confirmations.sellerAt && m.confirmations.buyerAt)) throw badRequest('NOT_CONFIRMED', 'both parties must confirm first');
    if (m.status === 'CONTACT_REVEALED' || m.contact_reveal) return m;
    const contactOf = (agentId) => {
      const agent = this.store.agents.get(agentId);
      const user = this.store.users.get(agent.principal_user_id);
      return { name: user.name, phone: user.contact.phone, email: user.contact.email };
    };
    m.contact_reveal = { seller_contact: contactOf(m.seller_agent_id), buyer_contact: contactOf(m.buyer_agent_id) };
    m.status = 'CONTACT_REVEALED';
    m.updated_at = this.clock.now();
    this.store.revealLog.push({ matchId, at: m.updated_at, sellerAgentId: m.seller_agent_id, buyerAgentId: m.buyer_agent_id });
    for (const id of [m.seller_agent_id, m.buyer_agent_id]) {
      this.notify(id, 'match', 'Both sides confirmed — contact details are now visible. Settle delivery and payment directly with the counterparty.', { matchId });
    }
    return m;
  }

  reportOutcome(matchId, agentId, outcome, notes = '') {
    const m = this.getMatch(matchId, agentId);
    if (!OUTCOMES.includes(outcome)) throw badRequest('INVALID_OUTCOME', `outcome must be one of ${OUTCOMES.join(', ')}`);
    if (m.status !== 'CONTACT_REVEALED') throw badRequest('INVALID_STATE', `outcomes can only be reported once contact is revealed (match is ${m.status})`);
    const now = this.clock.now();
    const existing = m.outcome_reports.find((r) => r.agent_id === agentId);
    if (existing) Object.assign(existing, { outcome, notes, reported_at: now });
    else m.outcome_reports.push({ agent_id: agentId, outcome, notes, reported_at: now });
    m.updated_at = now;
    this._finalize(m);
    return m;
  }

  /**
   * Fault assumption: a FELL_THROUGH / NO_RESPONSE report counts against the
   * counterparty of the reporter; if both report FELL_THROUGH, both are at fault.
   */
  _finalize(m) {
    const reports = m.outcome_reports;
    const other = (id) => (id === m.seller_agent_id ? m.buyer_agent_id : m.seller_agent_id);
    const bad = reports.filter((r) => r.outcome === 'FELL_THROUGH' || r.outcome === 'NO_RESPONSE');
    if (bad.length) {
      const kind = reports.some((r) => r.outcome === 'FELL_THROUGH') ? 'FELL_THROUGH' : 'NO_RESPONSE';
      m.status = kind;
      m.fault_agent_ids = [...new Set(bad.map((r) => other(r.agent_id)))];
    } else if (reports.length === 2) {
      m.status = 'COMPLETED';
    } else {
      return;
    }
    recomputeReputation(this.store, m.seller_agent_id);
    recomputeReputation(this.store, m.buyer_agent_id);
  }

  /** Unconfirmed matches lapse into NO_RESPONSE, attributed to whoever did not confirm. */
  expireStale() {
    const now = this.clock.now();
    for (const m of this.store.matches.values()) {
      if (!PRE_REVEAL.includes(m.status) || now - m.created_at < this.matchTtlMs) continue;
      m.status = 'NO_RESPONSE';
      m.fault_agent_ids = [];
      if (!m.confirmations.sellerAt) m.fault_agent_ids.push(m.seller_agent_id);
      if (!m.confirmations.buyerAt) m.fault_agent_ids.push(m.buyer_agent_id);
      m.updated_at = now;
      recomputeReputation(this.store, m.seller_agent_id);
      recomputeReputation(this.store, m.buyer_agent_id);
    }
  }
}
