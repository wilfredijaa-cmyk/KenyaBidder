/** Listing Agent behaviours: auto-relist unsold lots and daily summaries (spec §1 Persona A). */
export class SellerService {
  constructor({ store, clock, engine, notify = () => {} }) {
    this.store = store;
    this.clock = clock;
    this.engine = engine;
    this.notify = notify;
    engine.on('auction.settled', ({ auctionId, result }) => {
      if (result.outcome === 'NO_SALE') this.maybeRelist(auctionId);
    });
  }

  maybeRelist(auctionId) {
    const a = this.store.auctions.get(auctionId);
    const seller = this.store.agents.get(a.sellerAgentId);
    const cfg = seller?.durable_memory?.autoRelist;
    if (!cfg || seller.status !== 'ACTIVE') return null;
    if (a.relistCount >= (cfg.maxRelists ?? 3)) {
      this.notify(seller.agent_id, 'relist', `"${a.productSpec.title}" stayed unsold after ${a.relistCount} relists; the agent stopped relisting.`, { auctionId });
      return null;
    }
    const factor = 1 - (cfg.discountPct ?? 10) / 100;
    const floor = seller.constraints.reserve_floor ?? 0;
    const reserve = Math.max(floor, Math.floor(a.reservePrice * factor));
    const duration = a.endsAt - a.startsAt;
    const input = {
      sellerAgentId: seller.agent_id,
      productSpec: a.productSpec,
      auctionType: a.auctionType,
      reservePrice: reserve,
      durationMs: duration,
      relistOf: a.auctionId,
      relistCount: a.relistCount + 1,
    };
    if (a.auctionType === 'ENGLISH') {
      input.startPrice = Math.min(reserve, Math.max(0, Math.floor(a.startPrice * factor)));
      input.minIncrement = a.minIncrement;
      input.antiSnipe = a.antiSnipe;
    } else if (a.auctionType === 'DUTCH') {
      const d = a.dutch;
      const floorPrice = Math.max(reserve, Math.floor(d.floorPrice * factor));
      input.dutch = { ...d, startPrice: Math.max(floorPrice + 1, Math.floor(d.startPrice * factor)), floorPrice };
    }
    try {
      const created = this.engine.createListing(input);
      this.notify(seller.agent_id, 'relist', `"${a.productSpec.title}" did not sell. Relisted with reserve ${reserve} (attempt ${a.relistCount + 1}).`, { auctionId: created.auctionId });
      return created;
    } catch (e) {
      this.notify(seller.agent_id, 'relist', `Auto-relist failed: ${e.message}`, { auctionId });
      return null;
    }
  }

  /** Evening summary text (spec Persona A). */
  summary(agentId) {
    const since = this.clock.now() - 24 * 3600_000;
    let sold = 0;
    let revenue = 0;
    let unsold = 0;
    let live = 0;
    for (const a of this.store.auctions.values()) {
      if (a.sellerAgentId !== agentId) continue;
      if (a.status === 'ACTIVE' || a.status === 'EXTENDING' || a.status === 'SCHEDULED') live++;
      if (a.status === 'SETTLED' && a.closedAt >= since) {
        if (a.result.outcome === 'SOLD') {
          sold++;
          revenue += a.result.price;
        } else unsold++;
      }
    }
    return `Daily summary: ${sold} sold (${revenue} KES), ${unsold} unsold, ${live} live listings.`;
  }
}
