import { renderBidderPrompt } from './prompt.js';

/**
 * A strategy turns (agent, auction, market intel) into a *proposed* conditional
 * action — never an execution. Returns:
 *   { action: 'BID', kind, params, reasoning }  |  { action: 'SKIP', reasoning }
 */

const clampInt = (n, lo, hi) => Math.max(lo, Math.min(hi, Math.round(n)));

/** Milestone-1 hardcoded bidder: bid up to the ceiling in fixed-percentage increments. */
export class BaselineStrategy {
  name = 'baseline';
  async propose({ agent, auction }) {
    const ceiling = agent.constraints.budget_ceiling;
    switch (auction.auctionType) {
      case 'ENGLISH':
        return { action: 'BID', kind: 'ENGLISH_INCREMENTAL', params: { maxBid: ceiling, incrementPct: 5, snipeWindowMs: 0 }, reasoning: 'Baseline: bid up to ceiling in 5% steps.' };
      case 'DUTCH':
        return { action: 'BID', kind: 'DUTCH_ACCEPT', params: { threshold: ceiling }, reasoning: 'Baseline: accept as soon as the price is within budget.' };
      default:
        return { action: 'BID', kind: 'SEALED_BID', params: { amount: ceiling }, reasoning: 'Baseline: bid the ceiling.' };
    }
  }
}

/** Market-intel-driven valuation with bid shading and sniping — the deterministic stand-in for LLM reasoning. */
export class HeuristicStrategy {
  name = 'heuristic';
  async propose({ agent, auction, intel }) {
    const ceiling = agent.constraints.budget_ceiling;
    const stats = intel?.stats;
    const floorPrice = auction.auctionType === 'DUTCH' ? auction.dutch.floorPrice : auction.startPrice ?? auction.reservePrice ?? 0;
    // Estimated true value: a little above median clearing price when we have history, else the ceiling.
    const value = clampInt(stats?.median ? stats.median * 1.05 : ceiling, 1, ceiling);
    if (value < floorPrice) {
      return { action: 'SKIP', reasoning: `Estimated value ${value} is below the opening/floor price ${floorPrice}.` };
    }
    const basis = stats?.median ? `median clearing price ${stats.median} (n=${stats.count})` : 'no price history, using the budget ceiling';
    switch (auction.auctionType) {
      case 'ENGLISH': {
        const duration = auction.endsAt - auction.startsAt;
        const snipe = clampInt(duration * 0.1, 0, 10_000);
        return { action: 'BID', kind: 'ENGLISH_INCREMENTAL', params: { maxBid: value, incrementPct: 0, snipeWindowMs: snipe }, reasoning: `Value ≈ ${value} from ${basis}. Will snipe in the final ${snipe}ms, minimum increments only.` };
      }
      case 'DUTCH': {
        const threshold = clampInt(value * 0.95, 1, ceiling);
        return { action: 'BID', kind: 'DUTCH_ACCEPT', params: { threshold }, reasoning: `Value ≈ ${value} from ${basis}. Accept once the price falls to ${threshold} (5% margin).` };
      }
      case 'SECOND_PRICE_SEALED':
        return { action: 'BID', kind: 'SEALED_BID', params: { amount: value }, reasoning: `Vickrey: truthful bid of estimated value ${value} (${basis}).` };
      case 'FIRST_PRICE_SEALED':
        return { action: 'BID', kind: 'SEALED_BID', params: { amount: clampInt(value * 0.9, 1, ceiling) }, reasoning: `First-price: shade 10% below value ${value} (${basis}).` };
      default:
        return { action: 'SKIP', reasoning: `Unsupported auction type ${auction.auctionType}.` };
    }
  }
}

const PROPOSE_TOOL = {
  name: 'propose_action',
  description: 'Propose a conditional action for the deterministic execution engine, or skip the auction.',
  input_schema: {
    type: 'object',
    properties: {
      action: { type: 'string', enum: ['BID', 'SKIP'] },
      max_bid: { type: 'integer', description: 'ENGLISH: highest amount to bid (KES)' },
      increment_pct: { type: 'number', description: 'ENGLISH: percentage raise per step, 0-100' },
      snipe_window_ms: { type: 'integer', description: 'ENGLISH: only bid within this many ms of close; 0 = bid immediately' },
      threshold: { type: 'integer', description: 'DUTCH: accept when price <= threshold (KES)' },
      amount: { type: 'integer', description: 'SEALED: the sealed bid (KES)' },
      reasoning: { type: 'string' },
    },
    required: ['action', 'reasoning'],
  },
};

/** Validate + clamp an LLM tool-call into a safe proposal. Returns null if unusable. */
export function proposalFromToolInput(input, auction, agent) {
  if (!input || typeof input !== 'object') return null;
  const ceiling = agent.constraints.budget_ceiling;
  const reasoning = typeof input.reasoning === 'string' ? input.reasoning.slice(0, 500) : '';
  if (input.action === 'SKIP') return { action: 'SKIP', reasoning };
  if (input.action !== 'BID') return null;
  const int = (n) => (Number.isFinite(n) ? clampInt(n, 1, ceiling) : null);
  switch (auction.auctionType) {
    case 'ENGLISH': {
      const maxBid = int(input.max_bid);
      if (!maxBid) return null;
      const pct = Number.isFinite(input.increment_pct) ? Math.max(0, Math.min(100, input.increment_pct)) : 0;
      const snipe = Number.isFinite(input.snipe_window_ms) ? Math.max(0, Math.round(input.snipe_window_ms)) : 0;
      return { action: 'BID', kind: 'ENGLISH_INCREMENTAL', params: { maxBid, incrementPct: pct, snipeWindowMs: snipe }, reasoning };
    }
    case 'DUTCH': {
      const threshold = int(input.threshold);
      return threshold ? { action: 'BID', kind: 'DUTCH_ACCEPT', params: { threshold }, reasoning } : null;
    }
    default: {
      const amount = int(input.amount);
      return amount ? { action: 'BID', kind: 'SEALED_BID', params: { amount }, reasoning } : null;
    }
  }
}

/**
 * LLM strategy via the Anthropic Messages API with a forced, schema-constrained tool call.
 * Falls back to the heuristic strategy on any failure/timeout so time-critical auctions never block on the LLM (spec §12).
 */
export class LlmStrategy {
  name = 'llm';
  constructor({ apiKey = process.env.ANTHROPIC_API_KEY, model = process.env.KENYABIDDER_MODEL ?? 'claude-haiku-4-5-20251001', fetchImpl = globalThis.fetch, timeoutMs = 8000, fallback = new HeuristicStrategy(), getUser = () => ({ name: 'the user' }) } = {}) {
    Object.assign(this, { apiKey, model, fetchImpl, timeoutMs, fallback, getUser });
  }

  get available() {
    return !!this.apiKey;
  }

  async propose(ctx) {
    if (!this.available) return this._fallback(ctx, 'no API key');
    const { agent, auction, intel } = ctx;
    const user = this.getUser(agent);
    const listing = {
      type: auction.auctionType,
      title: auction.productSpec.title,
      description: auction.productSpec.description ?? '',
      category: auction.productSpec.category,
      quantity: auction.productSpec.quantity,
      startPrice: auction.startPrice,
      dutch: auction.dutch,
      durationMs: auction.endsAt - auction.startsAt,
    };
    const userMsg = `Decide how to approach this auction.\n\nMarket stats: ${JSON.stringify(intel?.stats ?? null)}\n\n<untrusted_listing_data>\n${JSON.stringify(listing)}\n</untrusted_listing_data>`;
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), this.timeoutMs);
    try {
      const res = await this.fetchImpl('https://api.anthropic.com/v1/messages', {
        method: 'POST',
        signal: ctl.signal,
        headers: { 'content-type': 'application/json', 'x-api-key': this.apiKey, 'anthropic-version': '2023-06-01' },
        body: JSON.stringify({
          model: this.model,
          max_tokens: 512,
          system: [{ type: 'text', text: renderBidderPrompt({ userName: user.name, agent }), cache_control: { type: 'ephemeral' } }],
          tools: [PROPOSE_TOOL],
          tool_choice: { type: 'tool', name: 'propose_action' },
          messages: [{ role: 'user', content: userMsg }],
        }),
      });
      if (!res.ok) return this._fallback(ctx, `HTTP ${res.status}`);
      const data = await res.json();
      const block = data.content?.find((b) => b.type === 'tool_use' && b.name === 'propose_action');
      const proposal = proposalFromToolInput(block?.input, auction, agent);
      return proposal ?? this._fallback(ctx, 'invalid tool output');
    } catch (e) {
      return this._fallback(ctx, e.name === 'AbortError' ? 'timeout' : e.message);
    } finally {
      clearTimeout(timer);
    }
  }

  async _fallback(ctx, why) {
    const p = await this.fallback.propose(ctx);
    return { ...p, reasoning: `[LLM unavailable: ${why}; heuristic fallback] ${p.reasoning}` };
  }
}
