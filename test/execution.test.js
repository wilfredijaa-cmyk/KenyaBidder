import test from 'node:test';
import assert from 'node:assert/strict';
import { makeApp, seller, bidder, english } from './helpers.js';
import { TransientError, DirectTransport } from '../src/harness/execution.js';

const trig = (app, b, auctionId, kind, params, extra = {}) =>
  app.execution.registerTrigger({ agentId: b.agent.agent_id, auctionId, kind, params, ...extra });

test('english trigger bids immediately and re-bids when outbid, up to maxBid', () => {
  const app = makeApp();
  const s = seller(app);
  const b1 = bidder(app, { name: 'b1' });
  const b2 = bidder(app, { name: 'b2' });
  const a = app.engine.createListing(english(s.agent.agent_id));
  trig(app, b1, a.auctionId, 'ENGLISH_INCREMENTAL', { maxBid: 2000, incrementPct: 0, snipeWindowMs: 0 });
  assert.equal(app.engine.getAuction(a.auctionId).bids.at(-1).amount, 1000); // opened immediately
  trig(app, b2, a.auctionId, 'ENGLISH_INCREMENTAL', { maxBid: 1500, incrementPct: 0, snipeWindowMs: 0 });
  // b2 outbids to 1100; b1 responds 1200; ... b2 stops at 1500 -> b1 holds 1600
  const bids = app.engine.getAuction(a.auctionId).bids;
  assert.equal(bids.at(-1).agentId, b1.agent.agent_id);
  assert.equal(bids.at(-1).amount, 1600);
  app.clock.advance(60_000);
  const d = app.engine.getAuctionDetail(a.auctionId);
  assert.equal(d.result.winnerAgentId, b1.agent.agent_id);
  const t2 = app.execution.triggersFor(b2.agent.agent_id)[0];
  assert.equal(t2.status, 'EXHAUSTED');
});

test('snipe window: waits, then fires inside the window', () => {
  const app = makeApp();
  const s = seller(app);
  const b = bidder(app);
  const a = app.engine.createListing(english(s.agent.agent_id));
  trig(app, b, a.auctionId, 'ENGLISH_INCREMENTAL', { maxBid: 5000, snipeWindowMs: 5000 });
  assert.equal(app.engine.getAuction(a.auctionId).bids.length, 0);
  app.clock.advance(54_000);
  app.tick();
  assert.equal(app.engine.getAuction(a.auctionId).bids.length, 0);
  app.clock.advance(1_500); // T-4.5s
  app.tick();
  assert.equal(app.engine.getAuction(a.auctionId).bids.length, 1);
});

test('dutch trigger fires the instant price crosses the threshold', () => {
  const app = makeApp();
  const s = seller(app);
  const b = bidder(app);
  const a = app.engine.createListing({
    sellerAgentId: s.agent.agent_id, productSpec: { category: 'electronics', title: 'T', quantity: 1 },
    auctionType: 'DUTCH', reservePrice: 5000, durationMs: 100_000,
    dutch: { startPrice: 10_000, floorPrice: 5000, decrement: 1000, intervalMs: 10_000 },
  });
  trig(app, b, a.auctionId, 'DUTCH_ACCEPT', { threshold: 7000 });
  app.clock.advance(20_000);
  app.tick();
  assert.equal(app.engine.getAuction(a.auctionId).status, 'ACTIVE');
  app.clock.advance(10_000); // price 7000
  app.tick();
  const d = app.engine.getAuctionDetail(a.auctionId);
  assert.equal(d.status, 'SETTLED');
  assert.equal(d.result.price, 7000);
  assert.equal(app.store.matches.size, 1);
});

test('guardrail: bid above ceiling is blocked and audited, never submitted', () => {
  const app = makeApp();
  const s = seller(app);
  const b = bidder(app, { ceiling: 1500 });
  const a = app.engine.createListing(english(s.agent.agent_id));
  const t = trig(app, b, a.auctionId, 'ENGLISH_INCREMENTAL', { maxBid: 9000 }); // LLM/user asks for more than ceiling
  app.engine.submitBid({ auctionId: a.auctionId, agentId: bidder(app, { name: 'rival' }).agent.agent_id, amount: 1600 });
  app.execution.evaluateAuction(a.auctionId);
  assert.equal(t.status, 'BLOCKED');
  assert.equal(t.lastError, 'CEILING_EXCEEDED');
  const audit = app.audit.forAgent(b.agent.agent_id);
  assert.ok(audit.some((e) => e.guardrail_decision === 'REJECTED' && e.rejection_reason.startsWith('CEILING_EXCEEDED')));
  assert.equal(app.engine.getAuction(a.auctionId).bids.at(-1).amount, 1600);
});

test('escalation threshold requires approval; approving lets the bid through', () => {
  const app = makeApp();
  const s = seller(app);
  const b = bidder(app, { ceiling: 10_000, constraints: { escalation_threshold_pct: 50 } });
  const a = app.engine.createListing(english(s.agent.agent_id, { startPrice: 6000, reservePrice: 6000 }));
  const t = trig(app, b, a.auctionId, 'ENGLISH_INCREMENTAL', { maxBid: 9000 });
  assert.equal(t.status, 'AWAITING_APPROVAL');
  assert.equal(app.engine.getAuction(a.auctionId).bids.length, 0);
  const approval = [...app.store.approvals.values()][0];
  assert.equal(approval.code, 'ESCALATION_THRESHOLD');
  app.execution.resolveApproval(approval.id, true);
  assert.equal(app.engine.getAuction(a.auctionId).bids.at(-1).amount, 6000);
});

test('rejecting an approval cancels the trigger', () => {
  const app = makeApp();
  const s = seller(app);
  const b = bidder(app, { ceiling: 10_000, constraints: { escalation_threshold_pct: 50 } });
  const a = app.engine.createListing(english(s.agent.agent_id, { startPrice: 6000, reservePrice: 6000 }));
  const t = trig(app, b, a.auctionId, 'ENGLISH_INCREMENTAL', { maxBid: 9000 });
  app.execution.resolveApproval([...app.store.approvals.values()][0].id, false);
  assert.equal(t.status, 'CANCELLED');
  assert.equal(app.engine.getAuction(a.auctionId).bids.length, 0);
  assert.throws(() => app.execution.resolveApproval([...app.store.approvals.values()][0].id, true), { code: 'APPROVAL_RESOLVED' });
});

test('revoking agent authority cancels pending triggers immediately', () => {
  const app = makeApp();
  const s = seller(app);
  const b = bidder(app);
  const a = app.engine.createListing(english(s.agent.agent_id));
  const t = trig(app, b, a.auctionId, 'ENGLISH_INCREMENTAL', { maxBid: 5000, snipeWindowMs: 5000 });
  app.agents.setStatus(b.agent.agent_id, 'PAUSED');
  assert.equal(t.status, 'CANCELLED');
  app.clock.advance(56_000);
  app.tick();
  assert.equal(app.engine.getAuction(a.auctionId).bids.length, 0);
});

test('network drop AFTER the bid landed: reconciles, does not double-bid', () => {
  const app = makeApp();
  const s = seller(app);
  const b = bidder(app);
  const a = app.engine.createListing(english(s.agent.agent_id));
  const direct = new DirectTransport(app.engine);
  let calls = 0;
  app.execution.transport = { submitBid(args) { calls++; direct.submitBid(args); throw new TransientError(); } };
  trig(app, b, a.auctionId, 'ENGLISH_INCREMENTAL', { maxBid: 5000 });
  assert.equal(app.engine.getAuction(a.auctionId).bids.length, 1);
  assert.equal(calls, 1);
  const last = app.audit.forAgent(b.agent.agent_id)[0];
  assert.equal(last.execution_result.reconciled, true);
});

test('network drop BEFORE the bid landed: retries with the same idempotency key', () => {
  const app = makeApp();
  const s = seller(app);
  const b = bidder(app);
  const a = app.engine.createListing(english(s.agent.agent_id));
  const direct = new DirectTransport(app.engine);
  const keys = [];
  let n = 0;
  app.execution.transport = { submitBid(args) { keys.push(args.idempotencyKey); if (n++ < 2) throw new TransientError(); return direct.submitBid(args); } };
  trig(app, b, a.auctionId, 'ENGLISH_INCREMENTAL', { maxBid: 5000 });
  assert.equal(app.engine.getAuction(a.auctionId).bids.length, 1);
  assert.equal(new Set(keys).size, 1);
  assert.equal(keys.length, 3);
});

test('persistent transport failure is surfaced, not retried forever', () => {
  const app = makeApp();
  const s = seller(app);
  const b = bidder(app);
  const a = app.engine.createListing(english(s.agent.agent_id));
  app.execution.transport = { submitBid() { throw new TransientError(); } };
  const t = trig(app, b, a.auctionId, 'ENGLISH_INCREMENTAL', { maxBid: 5000 });
  assert.equal(t.status, 'BLOCKED');
  assert.equal(t.lastError, 'TRANSPORT_FAILURE');
});

test('rate limit stops a runaway bidding war and recovers next window', () => {
  const app = makeApp({ guardrailConfig: { maxProposalsPerMinute: 5 } });
  const s = seller(app);
  const b1 = bidder(app, { name: 'b1', ceiling: 1_000_000 });
  const b2 = bidder(app, { name: 'b2', ceiling: 1_000_000 });
  const a = app.engine.createListing(english(s.agent.agent_id, { durationMs: 10 * 60_000 }));
  trig(app, b1, a.auctionId, 'ENGLISH_INCREMENTAL', { maxBid: 1_000_000 });
  trig(app, b2, a.auctionId, 'ENGLISH_INCREMENTAL', { maxBid: 1_000_000 });
  const count = app.engine.getAuction(a.auctionId).bids.length;
  assert.ok(count <= 10, `bids ${count}`);
  app.clock.advance(61_000);
  app.tick();
  assert.ok(app.engine.getAuction(a.auctionId).bids.length > count);
});

test('kind must match auction type; params are validated', () => {
  const app = makeApp();
  const s = seller(app);
  const b = bidder(app);
  const a = app.engine.createListing(english(s.agent.agent_id));
  assert.throws(() => trig(app, b, a.auctionId, 'DUTCH_ACCEPT', { threshold: 1 }), { code: 'KIND_MISMATCH' });
  assert.throws(() => trig(app, b, a.auctionId, 'ENGLISH_INCREMENTAL', { maxBid: -5 }), { code: 'INVALID_PARAMS' });
});

test('market anomaly trips the circuit breaker and halts autonomous bidding', () => {
  const app = makeApp();
  const s = seller(app);
  for (let i = 0; i < 5; i++) app.store.marketHistory.push({ category: 'electronics', auctionType: 'ENGLISH', price: 1000, quantity: 1, at: 1 });
  const b = bidder(app, { ceiling: 1_000_000 });
  const a = app.engine.createListing(english(s.agent.agent_id, { startPrice: 50_000, reservePrice: 50_000 }));
  const t = trig(app, b, a.auctionId, 'ENGLISH_INCREMENTAL', { maxBid: 900_000 });
  assert.equal(t.status, 'BLOCKED');
  assert.equal(t.lastError, 'MARKET_ANOMALY');
  assert.ok(app.store.breakers.has('electronics'));
  assert.ok(app.router.notificationsFor(b.agent.agent_id).some((n) => n.kind === 'anomaly'));
});

test('sealed trigger submits once; SEALED bid on vickrey wins', () => {
  const app = makeApp();
  const s = seller(app);
  const b = bidder(app);
  const a = app.engine.createListing({ sellerAgentId: s.agent.agent_id, productSpec: { category: 'x', title: 'Lot', quantity: 1 }, auctionType: 'SECOND_PRICE_SEALED', reservePrice: 100, durationMs: 5000 });
  const t = trig(app, b, a.auctionId, 'SEALED_BID', { amount: 3000 });
  assert.equal(t.status, 'DONE');
  app.clock.advance(5000);
  assert.equal(app.engine.getAuctionDetail(a.auctionId).result.winnerAgentId, b.agent.agent_id);
});

test('unauthorized auction type escalates instead of bidding', () => {
  const app = makeApp();
  const s = seller(app);
  const b = bidder(app, { constraints: { authorized_auction_types: ['ENGLISH'] } });
  const a = app.engine.createListing({ sellerAgentId: s.agent.agent_id, productSpec: { category: 'x', title: 'Lot', quantity: 1 }, auctionType: 'SECOND_PRICE_SEALED', reservePrice: 100, durationMs: 5000 });
  const t = trig(app, b, a.auctionId, 'SEALED_BID', { amount: 3000 });
  assert.equal(t.status, 'AWAITING_APPROVAL');
  assert.equal(app.engine.getAuction(a.auctionId).bids.length, 0);
  app.execution.resolveApproval(t.approvalId, true);
  assert.equal(app.engine.getAuction(a.auctionId).bids.length, 1);
});

test('dutch: standing trigger beats a late manual accept — exactly one winner, one match', () => {
  const app = makeApp();
  const s = seller(app);
  const t1 = bidder(app, { name: 't1' });
  const manual = bidder(app, { name: 'manual' });
  const a = app.engine.createListing({
    sellerAgentId: s.agent.agent_id, productSpec: { category: 'electronics', title: 'T', quantity: 1 },
    auctionType: 'DUTCH', reservePrice: 5000, durationMs: 100_000,
    dutch: { startPrice: 10_000, floorPrice: 5000, decrement: 1000, intervalMs: 10_000 },
  });
  trig(app, t1, a.auctionId, 'DUTCH_ACCEPT', { threshold: 9500 });
  const settled = [];
  app.engine.on('auction.settled', (e) => settled.push(e));
  app.clock.advance(15_000); // price 9000, trigger waiting
  // The price-change event fires the standing trigger first; the late manual accept must not double-settle.
  const res = app.engine.submitBid({ auctionId: a.auctionId, agentId: manual.agent.agent_id, amount: 9000 });
  assert.equal(res.ok, false);
  assert.equal(res.code, 'AUCTION_NOT_OPEN');
  assert.equal(settled.length, 1);
  assert.equal(app.engine.getAuction(a.auctionId).result.winnerAgentId, t1.agent.agent_id);
  assert.equal(app.store.matches.size, 1);
});
