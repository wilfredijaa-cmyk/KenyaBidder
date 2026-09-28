
/**
 * MCP-style tool servers (spec §8). Each server exposes `tools/list` and
 * `tools/call` over JSON-RPC 2.0. Tools flagged `internal` (e.g. submit_bid)
 * are callable only with the harness's internal key and are never listed to
 * LLM-facing clients — the LLM proposes, the Execution Engine executes.
 */
const obj = (properties, required = []) => ({ type: 'object', properties, required });
const str = { type: 'string' };
const int = { type: 'integer' };

export function buildMcpServers({ engine, matches, intel }) {
  const auction = [
    { name: 'list_active_auctions', description: 'Deterministic pre-filtered list of open auctions.', inputSchema: obj({ category: str, maxPrice: int, minQuantity: int, endsBefore: int, q: str, auctionTypes: { type: 'array', items: str } }), run: (a) => engine.listActiveAuctions(a ?? {}) },
    { name: 'get_auction_detail', description: 'Full public state, bids and time remaining.', inputSchema: obj({ auction_id: str, viewer_agent_id: str }, ['auction_id']), run: (a) => engine.getAuctionDetail(a.auction_id, a.viewer_agent_id ?? null) },
    { name: 'create_listing', description: 'Create an auction listing.', inputSchema: obj({ sellerAgentId: str, productSpec: { type: 'object' }, auctionType: str, reservePrice: int, startPrice: int, minIncrement: int, durationMs: int, dutch: { type: 'object' } }, ['sellerAgentId', 'productSpec', 'auctionType', 'durationMs']), run: (a) => engine.createListing(a) },
    { name: 'withdraw_listing', description: 'Withdraw a listing that has no bids.', inputSchema: obj({ listing_id: str, seller_agent_id: str, reason: str }, ['listing_id', 'seller_agent_id']), run: (a) => engine.withdrawListing({ listingId: a.listing_id, sellerAgentId: a.seller_agent_id, reason: a.reason }) },
    { name: 'submit_bid', description: 'Place a bid. Execution Engine only.', internal: true, inputSchema: obj({ auction_id: str, agent_id: str, amount: int, bid_type: str, idempotency_key: str }, ['auction_id', 'agent_id', 'amount']), run: (a) => engine.submitBid({ auctionId: a.auction_id, agentId: a.agent_id, amount: a.amount, bidType: a.bid_type, idempotencyKey: a.idempotency_key }) },
    { name: 'submit_sealed_bid', description: 'Place a sealed bid. Execution Engine only.', internal: true, inputSchema: obj({ auction_id: str, agent_id: str, sealed_amount: int, idempotency_key: str }, ['auction_id', 'agent_id', 'sealed_amount']), run: (a) => engine.submitBid({ auctionId: a.auction_id, agentId: a.agent_id, amount: a.sealed_amount, bidType: 'SEALED', idempotencyKey: a.idempotency_key }) },
  ];
  const match = [
    { name: 'create_match', description: 'Create a match for a sold auction. Deterministic layer only.', internal: true, inputSchema: obj({ auction_id: str }, ['auction_id']), run: (a) => matches.createMatch({ auctionId: a.auction_id }) },
    { name: 'confirm_match', description: 'Confirm intent to proceed.', inputSchema: obj({ match_id: str, agent_id: str }, ['match_id', 'agent_id']), run: (a) => matches.confirmMatch(a.match_id, a.agent_id) },
    { name: 'reveal_contact', description: 'Reveal contacts once both sides confirmed.', internal: true, inputSchema: obj({ match_id: str }, ['match_id']), run: (a) => matches.revealContact(a.match_id) },
    { name: 'report_outcome', description: 'Report COMPLETED | FELL_THROUGH | NO_RESPONSE.', inputSchema: obj({ match_id: str, agent_id: str, outcome: str, notes: str }, ['match_id', 'agent_id', 'outcome']), run: (a) => matches.reportOutcome(a.match_id, a.agent_id, a.outcome, a.notes ?? '') },
    { name: 'get_match_status', description: 'Read-only match status.', inputSchema: obj({ match_id: str, agent_id: str }, ['match_id', 'agent_id']), run: (a) => matches.getMatch(a.match_id, a.agent_id) },
  ];
  const market = [
    { name: 'get_historical_clearing_prices', description: 'Clearing price statistics for a category.', inputSchema: obj({ category: str, auction_type: str, since_ms: int }, ['category']), run: (a) => intel.getHistoricalClearingPrices({ category: a.category, auctionType: a.auction_type, sinceMs: a.since_ms }) },
    { name: 'get_demand_signal', description: 'Demand signal for a category.', inputSchema: obj({ category: str }, ['category']), run: (a) => intel.getDemandSignal({ category: a.category }) },
    { name: 'get_comparable_active_auctions', description: 'Comparable open auctions.', inputSchema: obj({ category: str, title: str }, ['category']), run: (a) => intel.getComparableActiveAuctions({ category: a.category, title: a.title }) },
  ];
  return { auction, match, market };
}

/** Handle one JSON-RPC 2.0 request against a tool list. */
export function handleRpc(tools, body, { internal = false } = {}) {
  const id = body?.id ?? null;
  const ok = (result) => ({ jsonrpc: '2.0', id, result });
  const err = (code, message) => ({ jsonrpc: '2.0', id, error: { code, message } });
  if (!body || body.jsonrpc !== '2.0' || typeof body.method !== 'string') return err(-32600, 'invalid request');
  if (body.method === 'initialize') return ok({ protocolVersion: '2024-11-05', capabilities: { tools: {} }, serverInfo: { name: 'kenyabidder', version: '0.1.0' } });
  if (body.method === 'tools/list') {
    return ok({ tools: tools.filter((t) => internal || !t.internal).map(({ name, description, inputSchema }) => ({ name, description, inputSchema })) });
  }
  if (body.method === 'tools/call') {
    const tool = tools.find((t) => t.name === body.params?.name);
    if (!tool || (tool.internal && !internal)) return err(-32601, `unknown tool ${body.params?.name}`);
    try {
      const out = tool.run(body.params?.arguments ?? {});
      return ok({ content: [{ type: 'text', text: JSON.stringify(out) }], isError: out?.ok === false });
    } catch (e) {
      return ok({ content: [{ type: 'text', text: JSON.stringify({ error: e.code ?? 'ERROR', message: e.message }) }], isError: true });
    }
  }
  return err(-32601, `unknown method ${body.method}`);
}
