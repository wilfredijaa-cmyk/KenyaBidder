import { randomUUID } from 'node:crypto';
import { badRequest } from '../errors.js';

/** WhatsApp adapter: always records to the outbox; sends via the Cloud API when credentials are configured. */
export class WhatsAppAdapter {
  constructor({ store, clock, token = process.env.WHATSAPP_TOKEN, phoneNumberId = process.env.WHATSAPP_PHONE_NUMBER_ID, fetchImpl = globalThis.fetch }) {
    Object.assign(this, { store, clock, token, phoneNumberId, fetchImpl });
  }
  async send(to, text) {
    this.store.outbox.push({ channel: 'WHATSAPP', to, text, at: this.clock.now() });
    if (this.store.outbox.length > 1000) this.store.outbox.splice(0, this.store.outbox.length - 1000);
    if (!this.token || !this.phoneNumberId) return;
    try {
      await this.fetchImpl(`https://graph.facebook.com/v20.0/${this.phoneNumberId}/messages`, {
        method: 'POST',
        headers: { authorization: `Bearer ${this.token}`, 'content-type': 'application/json' },
        body: JSON.stringify({ messaging_product: 'whatsapp', to, type: 'text', text: { body: text } }),
      });
    } catch (e) {
      console.error('[whatsapp] send failed', e.message);
    }
  }
}

/**
 * Channel Router (spec §7.3). Normalizes inbound messages from every channel
 * into commands against the *agent* (never a channel session), and routes
 * outbound notifications to web subscribers and the preferred external channel.
 */
export class ChannelRouter {
  constructor({ store, clock, whatsapp }) {
    this.store = store;
    this.clock = clock;
    this.whatsapp = whatsapp;
    this.webListeners = new Set(); // fn(notification)
    this.handlers = null; // bound by app.js to avoid circular deps
  }

  bind(handlers) {
    this.handlers = handlers;
  }

  notify(agentId, kind, message, data = {}) {
    const n = { id: randomUUID(), agentId, kind, message, data, at: this.clock.now() };
    this.store.notifications.push(n);
    if (this.store.notifications.length > 5000) this.store.notifications.splice(0, 1000);
    for (const fn of this.webListeners) {
      try {
        fn(n);
      } catch {
        /* a broken web client must never break execution */
      }
    }
    const agent = this.store.agents.get(agentId);
    if (agent?.durable_memory?.preferredChannel === 'WHATSAPP') {
      const link = agent.channel_identity_map.find((c) => c.channel === 'WHATSAPP');
      if (link) this.whatsapp.send(link.external_id, message);
    }
    return n;
  }

  notificationsFor(agentId, limit = 50) {
    const out = [];
    for (let i = this.store.notifications.length - 1; i >= 0 && out.length < limit; i--) {
      if (this.store.notifications[i].agentId === agentId) out.push(this.store.notifications[i]);
    }
    return out;
  }

  /** Inbound from any channel. Identity resolves through the channel identity map to the canonical agent. */
  async handleInbound({ channel, externalId, text }) {
    if (!this.handlers) throw new Error('router not bound');
    const agent = this.handlers.findByChannel(channel, externalId);
    if (!agent) return { agentId: null, reply: 'This number is not linked to a KenyaBidder agent. Link it from the web app first.' };
    agent.durable_memory.lastChannel = channel;
    const conv = agent.durable_memory.conversation;
    conv.push({ role: 'user', channel, text: String(text).slice(0, 500), at: this.clock.now() });
    let reply;
    try {
      reply = await this.command(agent, String(text).trim());
    } catch (e) {
      reply = `Sorry, that failed: ${e.message}`;
    }
    conv.push({ role: 'agent', channel, text: reply, at: this.clock.now() });
    if (conv.length > 100) conv.splice(0, conv.length - 100);
    return { agentId: agent.agent_id, reply };
  }

  async command(agent, text) {
    const h = this.handlers;
    const [cmd, ...rest] = text.split(/\s+/);
    const arg = rest.join(' ');
    switch ((cmd ?? '').toLowerCase()) {
      case 'status':
        return h.status(agent);
      case 'pause':
        h.setStatus(agent.agent_id, 'PAUSED');
        return 'Agent paused. All pending bid triggers were cancelled.';
      case 'resume':
        h.setStatus(agent.agent_id, 'ACTIVE');
        return 'Agent resumed.';
      case 'ceiling': {
        const n = Number(arg.replace(/[, ]/g, ''));
        if (!Number.isInteger(n) || n <= 0) throw badRequest('INVALID_CEILING', 'usage: ceiling <positive whole amount>');
        h.update(agent.agent_id, { constraints: { budget_ceiling: n } });
        return `Budget ceiling is now ${n} KES.`;
      }
      case 'approve':
      case 'reject': {
        const ap = h.findApproval(agent.agent_id, arg);
        if (!ap) return 'No pending approval matches that id.';
        await h.resolveApproval(ap.id, cmd.toLowerCase() === 'approve');
        return `${cmd.toLowerCase() === 'approve' ? 'Approved' : 'Rejected'}.`;
      }
      case 'summary':
        return agent.agent_type === 'SELLER' ? h.sellerSummary(agent.agent_id) : h.status(agent);
      case 'help':
      default:
        return 'Commands: status, pause, resume, ceiling <amount>, approve <id>, reject <id>, summary, help';
    }
  }
}
