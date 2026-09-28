import { randomUUID, randomBytes } from 'node:crypto';
import { AUCTION_TYPES } from '../mcp/auctionEngine.js';
import { badRequest, conflict, forbidden, isNonNegInt, notFound } from '../errors.js';

/** Users, agents and channel identity map (spec §7.1, §10). */
export class AgentService {
  constructor({ store, clock, execution = null }) {
    this.store = store;
    this.clock = clock;
    this.execution = execution;
  }

  createUser({ name, phone = null, email = null }) {
    if (typeof name !== 'string' || !name.trim()) throw badRequest('INVALID_USER', 'name is required');
    const user = { id: randomUUID(), name: name.trim(), contact: { phone, email }, createdAt: this.clock.now() };
    const token = randomBytes(24).toString('hex');
    this.store.users.set(user.id, user);
    this.store.tokens.set(token, user.id);
    return { user, token };
  }

  userForToken(token) {
    const id = this.store.tokens.get(token);
    return id ? this.store.users.get(id) : null;
  }

  createAgent({ userId, type, constraints = {}, memory = {} }) {
    if (!this.store.users.has(userId)) throw notFound('USER_NOT_FOUND', 'user not found');
    if (!['SELLER', 'BIDDER'].includes(type)) throw badRequest('INVALID_AGENT_TYPE', 'type must be SELLER or BIDDER');
    const c = this._constraints(type, constraints);
    const agent = {
      agent_id: randomUUID(),
      agent_type: type,
      principal_user_id: userId,
      channel_identity_map: [],
      constraints: c,
      reputation: { completed_matches: 0, fell_through_count: 0, avg_response_time_seconds: 0, tier: 'NEW', score: 100 },
      durable_memory: {
        conversation: [],
        strategy: 'heuristic',
        autoBid: true,
        watch: null,
        autoRelist: null,
        preferredChannel: 'WEB',
        lastChannel: 'WEB',
        oneOffAuthorizations: [],
        ...memory,
      },
      status: 'ACTIVE',
      createdAt: this.clock.now(),
    };
    if (type === 'BIDDER' && agent.durable_memory.watch) agent.durable_memory.watch = this._watch(agent.durable_memory.watch);
    this.store.agents.set(agent.agent_id, agent);
    return agent;
  }

  _constraints(type, input) {
    const c = {
      budget_ceiling: input.budget_ceiling ?? 0,
      reserve_floor: input.reserve_floor ?? 0,
      authorized_auction_types: input.authorized_auction_types ?? [...AUCTION_TYPES],
      escalation_threshold_pct: input.escalation_threshold_pct ?? 100,
    };
    if (!isNonNegInt(c.budget_ceiling)) throw badRequest('INVALID_CONSTRAINT', 'budget_ceiling must be a non-negative integer');
    if (type === 'BIDDER' && c.budget_ceiling <= 0) throw badRequest('INVALID_CONSTRAINT', 'budget_ceiling is required for bidder agents');
    if (!isNonNegInt(c.reserve_floor)) throw badRequest('INVALID_CONSTRAINT', 'reserve_floor must be a non-negative integer');
    if (!Array.isArray(c.authorized_auction_types) || c.authorized_auction_types.some((t) => !AUCTION_TYPES.includes(t))) {
      throw badRequest('INVALID_CONSTRAINT', `authorized_auction_types must be a subset of ${AUCTION_TYPES.join(', ')}`);
    }
    if (!(c.escalation_threshold_pct > 0 && c.escalation_threshold_pct <= 100)) throw badRequest('INVALID_CONSTRAINT', 'escalation_threshold_pct must be in (0, 100]');
    return c;
  }

  _watch(w) {
    if (!w || typeof w !== 'object') throw badRequest('INVALID_WATCH', 'watch must be an object');
    if (w.category != null && (typeof w.category !== 'string' || !w.category.trim())) throw badRequest('INVALID_WATCH', 'watch.category must be a string');
    if (w.minQuantity != null && !(Number.isInteger(w.minQuantity) && w.minQuantity > 0)) throw badRequest('INVALID_WATCH', 'watch.minQuantity must be a positive integer');
    if (w.deadlineAt != null && !Number.isInteger(w.deadlineAt)) throw badRequest('INVALID_WATCH', 'watch.deadlineAt must be epoch ms');
    return {
      category: w.category?.trim() ?? null,
      keywords: Array.isArray(w.keywords) ? w.keywords.map(String) : [],
      minQuantity: w.minQuantity ?? 1,
      deadlineAt: w.deadlineAt ?? null,
    };
  }

  getAgent(agentId) {
    const a = this.store.agents.get(agentId);
    if (!a) throw notFound('AGENT_NOT_FOUND', 'agent not found');
    return a;
  }

  /** Ownership check used by every user-facing route. */
  ownedAgent(agentId, userId) {
    const a = this.getAgent(agentId);
    if (a.principal_user_id !== userId) throw forbidden('NOT_YOUR_AGENT', 'agent belongs to another user');
    return a;
  }

  agentsFor(userId) {
    return [...this.store.agents.values()].filter((a) => a.principal_user_id === userId);
  }

  update(agentId, patch) {
    const agent = this.getAgent(agentId);
    if (patch.constraints) {
      agent.constraints = this._constraints(agent.agent_type, { ...agent.constraints, ...patch.constraints });
    }
    if (patch.memory) {
      const m = { ...patch.memory };
      if (m.watch !== undefined) m.watch = m.watch === null ? null : this._watch(m.watch);
      if (m.strategy !== undefined && !['baseline', 'heuristic', 'llm'].includes(m.strategy)) throw badRequest('INVALID_MEMORY', 'strategy must be baseline|heuristic|llm');
      if (m.preferredChannel !== undefined && !['WEB', 'WHATSAPP'].includes(m.preferredChannel)) throw badRequest('INVALID_MEMORY', 'preferredChannel must be WEB or WHATSAPP');
      delete m.conversation;
      delete m.oneOffAuthorizations;
      Object.assign(agent.durable_memory, m);
    }
    if (patch.status !== undefined) this.setStatus(agentId, patch.status);
    return agent;
  }

  setStatus(agentId, status) {
    if (!['ACTIVE', 'PAUSED', 'SUSPENDED'].includes(status)) throw badRequest('INVALID_STATUS', 'status must be ACTIVE|PAUSED|SUSPENDED');
    const agent = this.getAgent(agentId);
    agent.status = status;
    // Revocation is immediate: pending triggers are cancelled (spec §12).
    if (status !== 'ACTIVE') this.execution?.cancelAgentTriggers(agentId);
    return agent;
  }

  linkChannel(agentId, channel, externalId) {
    if (!['WEB', 'WHATSAPP'].includes(channel)) throw badRequest('INVALID_CHANNEL', 'channel must be WEB or WHATSAPP');
    if (typeof externalId !== 'string' || !externalId.trim()) throw badRequest('INVALID_CHANNEL', 'externalId required');
    const agent = this.getAgent(agentId);
    for (const other of this.store.agents.values()) {
      if (other.agent_id !== agentId && other.channel_identity_map.some((c) => c.channel === channel && c.external_id === externalId)) {
        throw conflict('CHANNEL_TAKEN', 'that channel identity is already linked to another agent');
      }
    }
    if (!agent.channel_identity_map.some((c) => c.channel === channel && c.external_id === externalId)) {
      agent.channel_identity_map.push({ channel, external_id: externalId });
    }
    return agent;
  }

  findByChannel(channel, externalId) {
    for (const a of this.store.agents.values()) {
      if (a.channel_identity_map.some((c) => c.channel === channel && c.external_id === externalId)) return a;
    }
    return null;
  }
}
