import { isOpen } from './auctionEngine.js';

const pct = (sorted, p) => {
  if (!sorted.length) return null;
  const i = (sorted.length - 1) * p;
  const lo = Math.floor(i);
  const hi = Math.ceil(i);
  return Math.round(sorted[lo] + (sorted[hi] - sorted[lo]) * (i - lo));
};

/** Market Intelligence MCP backing service (spec §8.3). */
export class MarketIntel {
  constructor({ store, clock, engine }) {
    this.store = store;
    this.clock = clock;
    this.engine = engine;
  }

  getHistoricalClearingPrices({ category, auctionType = null, sinceMs = null } = {}) {
    const cat = String(category ?? '').toLowerCase();
    const since = sinceMs != null ? this.clock.now() - sinceMs : -Infinity;
    const rows = this.store.marketHistory.filter(
      (r) => r.category === cat && (!auctionType || r.auctionType === auctionType) && r.at >= since,
    );
    const prices = rows.map((r) => r.price).sort((a, b) => a - b);
    return {
      category: cat,
      count: prices.length,
      min: prices[0] ?? null,
      p25: pct(prices, 0.25),
      median: pct(prices, 0.5),
      p75: pct(prices, 0.75),
      max: prices.at(-1) ?? null,
    };
  }

  getDemandSignal({ category }) {
    const cat = String(category ?? '').toLowerCase();
    let activeAuctions = 0;
    let bids = 0;
    for (const a of this.store.auctions.values()) {
      if (a.productSpec.category.toLowerCase() !== cat || !isOpen(a)) continue;
      activeAuctions++;
      bids += a.bids.length;
    }
    let watchers = 0;
    for (const ag of this.store.agents.values()) {
      if (ag.agent_type === 'BIDDER' && ag.status === 'ACTIVE' && ag.durable_memory?.watch?.category?.toLowerCase() === cat) watchers++;
    }
    return { category: cat, activeAuctions, bidsOnActive: bids, bidderAgentsWatching: watchers };
  }

  getComparableActiveAuctions({ category, title = '', excludeAuctionId = null }) {
    const words = new Set(String(title).toLowerCase().split(/\W+/).filter((w) => w.length > 2));
    return this.engine
      .listActiveAuctions({ category })
      .filter((a) => a.auctionId !== excludeAuctionId)
      .map((a) => ({
        auctionId: a.auctionId,
        title: a.productSpec.title,
        auctionType: a.auctionType,
        currentPrice: a.currentPrice,
        overlap: a.productSpec.title.toLowerCase().split(/\W+/).filter((w) => words.has(w)).length,
      }))
      .sort((x, y) => y.overlap - x.overlap);
  }

  /** Seller-side: recommend an auction type with reasoning (spec §1, §14.2). */
  recommendListing({ category, quantity = 1, urgency = 'normal', scarce = false }) {
    const stats = this.getHistoricalClearingPrices({ category });
    const demand = this.getDemandSignal({ category });
    let auctionType;
    let reasoning;
    if (scarce) {
      auctionType = 'ENGLISH';
      reasoning = 'Scarce or collectible stock: an ascending English auction lets competing bidders reveal their true willingness to pay.';
    } else if (urgency === 'high' || quantity >= 20) {
      auctionType = 'DUTCH';
      reasoning = 'Fast-moving or bulk stock: a descending Dutch auction clears quickly — the first bidder to accept the current price wins.';
    } else if (demand.bidderAgentsWatching >= 3) {
      auctionType = 'SECOND_PRICE_SEALED';
      reasoning = 'Several bidder agents are watching this category: a Vickrey auction encourages truthful bids and extracts real value.';
    } else {
      auctionType = 'ENGLISH';
      reasoning = 'Default: English auction is the most transparent format for thin markets.';
    }
    const suggestedReserve = stats.p25 ?? null;
    return {
      auctionType,
      reasoning,
      suggestedReserve,
      suggestedStartPrice: stats.p25 != null ? Math.round(stats.p25 * 0.8) : null,
      marketStats: stats,
      demand,
    };
  }
}
