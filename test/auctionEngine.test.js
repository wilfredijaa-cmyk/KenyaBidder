import test from 'node:test';
import assert from 'node:assert/strict';
import { makeApp, seller, bidder, english } from './helpers.js';

test('english: bids must beat min increment, highest wins at own price', () => {
  const app = makeApp();
  const s = seller(app);
  const b1 = bidder(app, { name: 'b1' });
  const b2 = bidder(app, { name: 'b2' });
  const a = app.engine.createListing(english(s.agent.agent_id));
  const bid = (b, amount) => app.engine.submitBid({ auctionId: a.auctionId, agentId: b.agent.agent_id, amount });

  assert.equal(bid(b1, 900).code, 'BID_TOO_LOW');
  assert.equal(bid(b1, 1000).ok, true);
  assert.equal(bid(b2, 1050).code, 'BID_TOO_LOW'); // increment is 100
  assert.equal(bid(b1, 1200).code, 'ALREADY_HIGHEST');
  assert.equal(bid(b2, 1100).ok, true);

  app.clock.advance(60_000);
  const d = app.engine.getAuctionDetail(a.auctionId);
  assert.equal(d.status, 'SETTLED');
  assert.deepEqual(d.result, { outcome: 'SOLD', winnerAgentId: b2.agent.agent_id, price: 1100 });
});

test('english: reserve not met -> NO_SALE; reserve hidden from bidders', () => {
  const app = makeApp();
  const s = seller(app);
  const b = bidder(app);
  const a = app.engine.createListing(english(s.agent.agent_id, { reservePrice: 5000, startPrice: 1000 }));
  app.engine.submitBid({ auctionId: a.auctionId, agentId: b.agent.agent_id, amount: 1000 });
  assert.equal(app.engine.getAuctionDetail(a.auctionId, b.agent.agent_id).reservePrice, undefined);
  assert.equal(app.engine.getAuctionDetail(a.auctionId, s.agent.agent_id).reservePrice, 5000);
  app.clock.advance(60_000);
  assert.equal(app.engine.getAuctionDetail(a.auctionId).result.outcome, 'NO_SALE');
  assert.equal(app.store.matches.size, 0);
});

test('english: anti-sniping extends the close', () => {
  const app = makeApp();
  const s = seller(app);
  const b = bidder(app);
  const a = app.engine.createListing(english(s.agent.agent_id, { antiSnipe: { windowMs: 5000, extendMs: 10_000 } }));
  app.clock.advance(57_000);
  assert.equal(app.engine.submitBid({ auctionId: a.auctionId, agentId: b.agent.agent_id, amount: 1000 }).ok, true);
  const d = app.engine.getAuctionDetail(a.auctionId);
  assert.equal(d.status, 'EXTENDING');
  assert.equal(d.extendedUntil, a.endsAt + 10_000);
  app.clock.advance(5_000); // past original end, still inside extension
  assert.equal(app.engine.getAuctionDetail(a.auctionId).status, 'EXTENDING');
  app.clock.advance(8_000);
  assert.equal(app.engine.getAuctionDetail(a.auctionId).status, 'SETTLED');
});

test('dutch: price decays on schedule; first acceptance wins at the current price', () => {
  const app = makeApp();
  const s = seller(app);
  const b = bidder(app);
  const a = app.engine.createListing({
    sellerAgentId: s.agent.agent_id,
    productSpec: { category: 'electronics', title: 'Tablets', quantity: 5 },
    auctionType: 'DUTCH',
    reservePrice: 5000,
    durationMs: 100_000,
    dutch: { startPrice: 10_000, floorPrice: 5000, decrement: 500, intervalMs: 10_000 },
  });
  assert.equal(app.engine.currentPrice(app.engine.getAuction(a.auctionId)), 10_000);
  app.clock.advance(35_000);
  assert.equal(app.engine.getAuctionDetail(a.auctionId).currentPrice, 8500);
  const lowball = app.engine.submitBid({ auctionId: a.auctionId, agentId: b.agent.agent_id, amount: 8000 });
  assert.equal(lowball.code, 'BID_TOO_LOW');
  const win = app.engine.submitBid({ auctionId: a.auctionId, agentId: b.agent.agent_id, amount: 9000 });
  assert.equal(win.ok, true);
  const d = app.engine.getAuctionDetail(a.auctionId);
  assert.equal(d.status, 'SETTLED');
  assert.equal(d.result.price, 8500); // pays the current price, not the offered amount
});

test('dutch: never falls below floor and unsold at end', () => {
  const app = makeApp();
  const s = seller(app);
  const a = app.engine.createListing({
    sellerAgentId: s.agent.agent_id,
    productSpec: { category: 'electronics', title: 'Tablets', quantity: 5 },
    auctionType: 'DUTCH', reservePrice: 5000, durationMs: 100_000,
    dutch: { startPrice: 10_000, floorPrice: 5000, decrement: 5000, intervalMs: 1000 },
  });
  app.clock.advance(50_000);
  assert.equal(app.engine.getAuctionDetail(a.auctionId).currentPrice, 5000);
  app.clock.advance(50_000);
  assert.equal(app.engine.getAuctionDetail(a.auctionId).result.outcome, 'NO_SALE');
});

test('sealed: bids hidden while open; first-price pays own bid; vickrey pays second price', () => {
  for (const [type, expected] of [['FIRST_PRICE_SEALED', 9000], ['SECOND_PRICE_SEALED', 7000]]) {
    const app = makeApp();
    const s = seller(app);
    const b1 = bidder(app, { name: 'b1' });
    const b2 = bidder(app, { name: 'b2' });
    const a = app.engine.createListing({ sellerAgentId: s.agent.agent_id, productSpec: { category: 'x', title: 'Lot', quantity: 1 }, auctionType: type, reservePrice: 1000, durationMs: 10_000 });
    const put = (b, amount) => app.engine.submitBid({ auctionId: a.auctionId, agentId: b.agent.agent_id, amount, bidType: 'SEALED' });
    assert.equal(put(b1, 500).code, 'BELOW_RESERVE');
    assert.equal(put(b1, 9000).ok, true);
    assert.equal(put(b2, 7000).ok, true);
    const open = app.engine.getAuctionDetail(a.auctionId, b1.agent.agent_id);
    assert.equal(open.currentPrice, null);
    assert.equal(open.bids.length, 1); // only own bid visible
    assert.equal(open.bids[0].agentId, b1.agent.agent_id);
    app.clock.advance(10_000);
    const d = app.engine.getAuctionDetail(a.auctionId);
    assert.equal(d.result.winnerAgentId, b1.agent.agent_id);
    assert.equal(d.result.price, expected);
  }
});

test('vickrey with a single bidder pays the reserve', () => {
  const app = makeApp();
  const s = seller(app);
  const b = bidder(app);
  const a = app.engine.createListing({ sellerAgentId: s.agent.agent_id, productSpec: { category: 'x', title: 'Lot', quantity: 1 }, auctionType: 'SECOND_PRICE_SEALED', reservePrice: 1000, durationMs: 1000 });
  app.engine.submitBid({ auctionId: a.auctionId, agentId: b.agent.agent_id, amount: 8000 });
  app.clock.advance(1000);
  assert.equal(app.engine.getAuctionDetail(a.auctionId).result.price, 1000);
});

test('idempotent submit_bid: replay returns the same result without a second bid', () => {
  const app = makeApp();
  const s = seller(app);
  const b = bidder(app);
  const a = app.engine.createListing(english(s.agent.agent_id));
  const args = { auctionId: a.auctionId, agentId: b.agent.agent_id, amount: 1000, idempotencyKey: 'k1' };
  assert.equal(app.engine.submitBid(args).ok, true);
  const again = app.engine.submitBid(args);
  assert.equal(again.ok, true);
  assert.equal(again.replayed, true);
  assert.equal(app.engine.getAuction(a.auctionId).bids.length, 1);
});

test('self-bidding and bad listings are rejected', () => {
  const app = makeApp();
  const s = seller(app, { reserve_floor: 500 });
  const a = app.engine.createListing(english(s.agent.agent_id));
  // same human principal owning both agents
  const own = app.agents.createAgent({ userId: s.user.id, type: 'BIDDER', constraints: { budget_ceiling: 10_000 } });
  assert.equal(app.engine.submitBid({ auctionId: a.auctionId, agentId: own.agent_id, amount: 1000 }).code, 'SELF_BID');
  assert.throws(() => app.engine.createListing(english(s.agent.agent_id, { reservePrice: 100 })), { code: 'RESERVE_BELOW_FLOOR' });
  assert.throws(() => app.engine.createListing(english(s.agent.agent_id, { auctionType: 'BOGUS' })), { code: 'INVALID_AUCTION_TYPE' });
  assert.throws(() => app.engine.createListing(english(s.agent.agent_id, { durationMs: 0 })), { code: 'INVALID_DURATION' });
  assert.throws(() => app.engine.createListing(english(s.agent.agent_id, { productSpec: { category: 'x', title: 'y', quantity: 0 } })), { code: 'INVALID_SPEC' });
});

test('scheduled auctions open at startsAt; withdraw only without bids', () => {
  const app = makeApp();
  const s = seller(app);
  const b = bidder(app);
  const a = app.engine.createListing(english(s.agent.agent_id, { startsAt: app.clock.now() + 5000 }));
  assert.equal(a.status, 'SCHEDULED');
  assert.equal(app.engine.submitBid({ auctionId: a.auctionId, agentId: b.agent.agent_id, amount: 1000 }).code, 'AUCTION_NOT_OPEN');
  app.clock.advance(5000);
  app.engine.submitBid({ auctionId: a.auctionId, agentId: b.agent.agent_id, amount: 1000 });
  assert.throws(() => app.engine.withdrawListing({ listingId: a.auctionId, sellerAgentId: s.agent.agent_id }), { code: 'HAS_BIDS' });
  const c = app.engine.createListing(english(s.agent.agent_id));
  assert.equal(app.engine.withdrawListing({ listingId: c.auctionId, sellerAgentId: s.agent.agent_id }).status, 'CANCELLED');
});

test('listActiveAuctions applies the deterministic pre-filter', () => {
  const app = makeApp();
  const s = seller(app);
  app.engine.createListing(english(s.agent.agent_id));
  app.engine.createListing(english(s.agent.agent_id, { productSpec: { category: 'furniture', title: 'Desk', quantity: 2 } }));
  assert.equal(app.engine.listActiveAuctions({ category: 'Electronics' }).length, 1);
  assert.equal(app.engine.listActiveAuctions({ maxPrice: 500 }).length, 0);
  assert.equal(app.engine.listActiveAuctions({ minQuantity: 5 }).length, 1);
  assert.equal(app.engine.listActiveAuctions({ q: 'desk' }).length, 1);
});
