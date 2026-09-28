import { Store } from '../src/store.js';
import { FakeClock } from '../src/clock.js';
import { createApp } from '../src/app.js';

/** Fully wired app on a fake clock, no timers, no HTTP. */
export function makeApp(opts = {}) {
  const clock = new FakeClock();
  const app = createApp({ store: new Store(), clock, ...opts });
  return { clock, ...app };
}

export function seller(app, { reserve_floor = 0, memory = {}, name = 'Amina' } = {}) {
  const { user } = app.agents.createUser({ name, phone: '+254700000001', email: 'amina@example.com' });
  const agent = app.agents.createAgent({ userId: user.id, type: 'SELLER', constraints: { reserve_floor }, memory });
  return { user, agent };
}

export function bidder(app, { ceiling = 100_000, name = 'David', constraints = {}, memory = {}, phone } = {}) {
  const { user } = app.agents.createUser({ name, phone: phone ?? '+254700000002', email: `${name}@example.com` });
  const agent = app.agents.createAgent({ userId: user.id, type: 'BIDDER', constraints: { budget_ceiling: ceiling, ...constraints }, memory });
  return { user, agent };
}

export const english = (sellerAgentId, over = {}) => ({
  sellerAgentId,
  productSpec: { category: 'electronics', title: 'Samsung A15 phones', quantity: 10 },
  auctionType: 'ENGLISH',
  reservePrice: 1000,
  startPrice: 1000,
  minIncrement: 100,
  durationMs: 60_000,
  ...over,
});
