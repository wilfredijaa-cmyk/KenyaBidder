import { EventEmitter } from 'node:events';
import { randomUUID } from 'node:crypto';
import { AppError, badRequest, forbidden, isNonNegInt, isPosInt, notFound } from '../errors.js';

export const AUCTION_TYPES = Object.freeze(['ENGLISH', 'DUTCH', 'FIRST_PRICE_SEALED', 'SECOND_PRICE_SEALED']);
const SEALED = new Set(['FIRST_PRICE_SEALED', 'SECOND_PRICE_SEALED']);
const OPEN = new Set(['ACTIVE', 'EXTENDING']);

export const isOpen = (a) => OPEN.has(a.status);
export const isSealed = (a) => SEALED.has(a.auctionType);
export const effectiveEnd = (a) => a.extendedUntil ?? a.endsAt;

/**
 * Auction Engine (spec §8.1, §9). Pure, synchronous, clock-injected so the
 * hot path stays deterministic and testable. Supports English, Dutch,
 * First-Price Sealed and Second-Price (Vickrey) Sealed. Reverse, Multi-Unit
 * and Combinatorial are architecturally reserved (Milestone 4+).
 */
export class AuctionEngine extends EventEmitter {
  constructor({ store, clock }) {
    super();
    this.setMaxListeners(0);
    this.store = store;
    this.clock = clock;
  }

  // ---------- listings ----------

  createListing(input) {
    const now = this.clock.now();
    const seller = this.store.agents.get(input.sellerAgentId);
    if (!seller || seller.agent_type !== 'SELLER') throw badRequest('INVALID_SELLER', 'sellerAgentId must reference a SELLER agent');
    if (seller.status !== 'ACTIVE') throw forbidden('AGENT_NOT_ACTIVE', 'seller agent is not active');

    const spec = input.productSpec;
    if (!spec || typeof spec !== 'object') throw badRequest('INVALID_SPEC', 'productSpec is required');
    if (typeof spec.category !== 'string' || !spec.category.trim()) throw badRequest('INVALID_SPEC', 'productSpec.category is required');
    if (typeof spec.title !== 'string' || !spec.title.trim()) throw badRequest('INVALID_SPEC', 'productSpec.title is required');
    if (!isPosInt(spec.quantity)) throw badRequest('INVALID_SPEC', 'productSpec.quantity must be a positive integer');

    const type = input.auctionType;
    if (!AUCTION_TYPES.includes(type)) throw badRequest('INVALID_AUCTION_TYPE', `auctionType must be one of ${AUCTION_TYPES.join(', ')}`);

    const reservePrice = input.reservePrice ?? 0;
    if (!isNonNegInt(reservePrice)) throw badRequest('INVALID_PRICE', 'reservePrice must be a non-negative integer');
    const floor = seller.constraints.reserve_floor ?? 0;
    if (reservePrice < floor) throw badRequest('RESERVE_BELOW_FLOOR', `reservePrice is below the agent's reserve floor (${floor})`);
    if (!isPosInt(input.durationMs)) throw badRequest('INVALID_DURATION', 'durationMs must be a positive integer');
    const startsAt = input.startsAt ?? now;
    if (!Number.isInteger(startsAt)) throw badRequest('INVALID_START', 'startsAt must be an epoch-ms integer');

    const a = {
      auctionId: randomUUID(),
      auctionType: type,
      status: startsAt > now ? 'SCHEDULED' : 'ACTIVE',
      sellerAgentId: seller.agent_id,
      productSpec: { ...spec, category: spec.category.trim(), title: spec.title.trim() },
      reservePrice,
      currentPrice: null,
      minIncrement: 1,
      startPrice: null,
      dutch: null,
      antiSnipe: { windowMs: 0, extendMs: 0 },
      bids: [],
      startsAt,
      endsAt: startsAt + input.durationMs,
      extendedUntil: null,
      result: null,
      relistOf: input.relistOf ?? null,
      relistCount: input.relistCount ?? 0,
      createdAt: now,
    };

    if (type === 'ENGLISH') {
      const startPrice = input.startPrice ?? reservePrice;
      if (!isNonNegInt(startPrice)) throw badRequest('INVALID_PRICE', 'startPrice must be a non-negative integer');
      a.startPrice = startPrice;
      a.currentPrice = startPrice;
      a.minIncrement = input.minIncrement ?? 1;
      if (!isPosInt(a.minIncrement)) throw badRequest('INVALID_INCREMENT', 'minIncrement must be a positive integer');
      const s = input.antiSnipe ?? {};
      if (s.windowMs && s.extendMs && isPosInt(s.windowMs) && isPosInt(s.extendMs)) a.antiSnipe = { windowMs: s.windowMs, extendMs: s.extendMs };
    } else if (type === 'DUTCH') {
      const d = input.dutch;
      if (!d || !isPosInt(d.startPrice) || !isNonNegInt(d.floorPrice) || !isPosInt(d.decrement) || !isPosInt(d.intervalMs)) {
        throw badRequest('INVALID_DUTCH', 'dutch {startPrice, floorPrice, decrement, intervalMs} required (positive integers)');
      }
      if (d.startPrice <= d.floorPrice) throw badRequest('INVALID_DUTCH', 'dutch.startPrice must exceed floorPrice');
      if (d.floorPrice < reservePrice) throw badRequest('INVALID_DUTCH', 'dutch.floorPrice must be >= reservePrice');
      a.dutch = { ...d };
      a.startPrice = d.startPrice;
      a.currentPrice = d.startPrice;
    }

    this.store.auctions.set(a.auctionId, a);
    this.emit('auction.created', { auctionId: a.auctionId });
    return this.view(a, seller.agent_id);
  }

  withdrawListing({ listingId, sellerAgentId, reason = '' }) {
    const a = this._get(listingId);
    if (a.sellerAgentId !== sellerAgentId) throw forbidden('NOT_LISTING_OWNER', 'only the listing seller may withdraw');
    this._advance(a);
    if (a.status !== 'SCHEDULED' && !isOpen(a)) throw new AppError(409, 'NOT_WITHDRAWABLE', `auction is ${a.status}`);
    if (a.bids.length > 0) throw new AppError(409, 'HAS_BIDS', 'cannot withdraw a listing that already has bids');
    a.status = 'CANCELLED';
    a.result = { outcome: 'CANCELLED', reason };
    this.emit('auction.cancelled', { auctionId: a.auctionId });
    return this.view(a, sellerAgentId);
  }

  // ---------- reads ----------

  _get(id) {
    const a = this.store.auctions.get(id);
    if (!a) throw notFound('AUCTION_NOT_FOUND', `auction ${id} not found`);
    return a;
  }

  /** Raw internal record — trusted harness code only, never shown to LLMs/clients. */
  getAuction(id) {
    const a = this._get(id);
    this._advance(a);
    return a;
  }

  currentPrice(a, now = this.clock.now()) {
    if (a.auctionType === 'DUTCH') {
      const at = Math.min(Math.max(now, a.startsAt), a.endsAt);
      const steps = Math.floor((at - a.startsAt) / a.dutch.intervalMs);
      return Math.max(a.dutch.floorPrice, a.dutch.startPrice - steps * a.dutch.decrement);
    }
    return a.currentPrice;
  }

  /** Lowest amount a bid must reach to be valid right now. */
  minNextBid(a, now = this.clock.now()) {
    if (a.auctionType === 'ENGLISH') return a.bids.length ? a.currentPrice + a.minIncrement : a.startPrice;
    if (a.auctionType === 'DUTCH') return this.currentPrice(a, now);
    return Math.max(a.reservePrice, 1);
  }

  /** Client-safe view: hides the reserve from non-sellers and sealed bids until settlement. */
  view(a, viewerAgentId = null) {
    const now = this.clock.now();
    const isSeller = viewerAgentId && viewerAgentId === a.sellerAgentId;
    const sealedOpen = isSealed(a) && a.status !== 'SETTLED';
    const v = {
      auctionId: a.auctionId,
      auctionType: a.auctionType,
      status: a.status,
      sellerAgentId: a.sellerAgentId,
      productSpec: a.productSpec,
      currentPrice: sealedOpen ? null : this.currentPrice(a, now),
      minNextBid: isOpen(a) ? this.minNextBid(a, now) : null,
      minIncrement: a.minIncrement,
      startPrice: a.startPrice,
      dutch: a.dutch,
      antiSnipe: a.antiSnipe,
      startsAt: a.startsAt,
      endsAt: a.endsAt,
      extendedUntil: a.extendedUntil,
      bidCount: a.bids.length,
      reserveMet: a.auctionType === 'ENGLISH' ? a.currentPrice >= a.reservePrice && a.bids.length > 0 : undefined,
      result: a.result && (isSeller || a.status === 'SETTLED') ? a.result : null,
      relistOf: a.relistOf,
    };
    if (isSeller) v.reservePrice = a.reservePrice;
    if (sealedOpen) {
      v.bids = a.bids.filter((b) => b.agentId === viewerAgentId).map(pubBid);
    } else {
      v.bids = a.bids.map(pubBid);
    }
    return v;
  }

  getAuctionDetail(id, viewerAgentId = null) {
    return this.view(this.getAuction(id), viewerAgentId);
  }

  /** Deterministic pre-filter (spec §11.3) — no LLM involved. */
  listActiveAuctions(filters = {}, viewerAgentId = null) {
    this.tick();
    const statuses = new Set(filters.includeScheduled ? ['ACTIVE', 'EXTENDING', 'SCHEDULED'] : ['ACTIVE', 'EXTENDING']);
    const now = this.clock.now();
    const q = filters.q ? String(filters.q).toLowerCase() : null;
    const out = [];
    for (const a of this.store.auctions.values()) {
      if (!statuses.has(a.status)) continue;
      if (filters.category && a.productSpec.category.toLowerCase() !== String(filters.category).toLowerCase()) continue;
      if (filters.auctionTypes?.length && !filters.auctionTypes.includes(a.auctionType)) continue;
      if (filters.minQuantity && a.productSpec.quantity < filters.minQuantity) continue;
      if (filters.endsBefore && effectiveEnd(a) > filters.endsBefore) continue;
      if (filters.maxPrice != null && this.minNextBid(a, now) > filters.maxPrice) continue;
      if (q && !`${a.productSpec.title} ${a.productSpec.description ?? ''}`.toLowerCase().includes(q)) continue;
      out.push(this.view(a, viewerAgentId));
    }
    return out.sort((x, y) => x.endsAt - y.endsAt);
  }

  // ---------- lifecycle ----------

  /** Advance every auction to the current clock time. */
  tick() {
    for (const a of this.store.auctions.values()) this._advance(a);
  }

  refresh(id) {
    return this.getAuction(id);
  }

  _advance(a) {
    const now = this.clock.now();
    if (a.status === 'SCHEDULED' && now >= a.startsAt) {
      a.status = 'ACTIVE';
      this.emit('auction.started', { auctionId: a.auctionId });
    }
    if (!isOpen(a)) return;
    if (now >= effectiveEnd(a)) {
      this._close(a, now);
    } else if (a.auctionType === 'DUTCH') {
      const p = this.currentPrice(a, now);
      if (p !== a.currentPrice) {
        a.currentPrice = p;
        this.emit('auction.price', { auctionId: a.auctionId, price: p });
      }
    }
  }

  _close(a, now, dutchWinner = null) {
    if (a.status === 'SETTLED' || a.status === 'CLOSING') return; // a lot can only be settled once
    a.status = 'CLOSING';
    const reserve = a.reservePrice;
    let result = { outcome: 'NO_SALE', winnerAgentId: null, price: null };

    if (a.auctionType === 'ENGLISH') {
      const top = a.bids.at(-1);
      if (top && top.amount >= reserve) result = { outcome: 'SOLD', winnerAgentId: top.agentId, price: top.amount };
    } else if (a.auctionType === 'DUTCH') {
      if (dutchWinner) result = { outcome: 'SOLD', winnerAgentId: dutchWinner.agentId, price: dutchWinner.amount };
    } else {
      const ranked = [...a.bids].filter((b) => b.amount >= reserve).sort((x, y) => y.amount - x.amount || x.at - y.at);
      if (ranked.length) {
        const price = a.auctionType === 'FIRST_PRICE_SEALED' ? ranked[0].amount : Math.max(ranked[1]?.amount ?? 0, reserve);
        result = { outcome: 'SOLD', winnerAgentId: ranked[0].agentId, price };
      }
    }

    a.result = result;
    a.status = 'SETTLED';
    a.closedAt = now;
    if (a.auctionType === 'DUTCH' && result.price != null) a.currentPrice = result.price;
    if (result.outcome === 'SOLD') {
      this.store.marketHistory.push({
        category: a.productSpec.category.toLowerCase(),
        auctionType: a.auctionType,
        price: result.price,
        quantity: a.productSpec.quantity,
        at: now,
      });
    }
    this.emit('auction.settled', { auctionId: a.auctionId, result });
  }

  // ---------- bidding ----------

  /**
   * submit_bid (Execution Engine only — never exposed to an LLM).
   * Idempotent per (agentId, idempotencyKey). Returns {ok:false, code} instead of throwing.
   */
  submitBid({ auctionId, agentId, amount, bidType = 'BID', idempotencyKey = null }) {
    const key = idempotencyKey ? `${agentId}:${idempotencyKey}` : null;
    if (key && this.store.idempotency.has(key)) return { ...this.store.idempotency.get(key), replayed: true };
    const res = this._submit({ auctionId, agentId, amount, bidType, idempotencyKey });
    if (key && res.ok) this.store.idempotency.set(key, res);
    return res;
  }

  _submit({ auctionId, agentId, amount, bidType, idempotencyKey }) {
    const fail = (code, message, extra = {}) => ({ ok: false, code, message, ...extra });
    const a = this.store.auctions.get(auctionId);
    if (!a) return fail('AUCTION_NOT_FOUND', 'auction not found');
    const agent = this.store.agents.get(agentId);
    if (!agent || agent.agent_type !== 'BIDDER') return fail('INVALID_BIDDER', 'agent is not a bidder');
    if (agent.status !== 'ACTIVE') return fail('AGENT_NOT_ACTIVE', 'agent is not active');
    this._advance(a);
    if (!isOpen(a)) return fail('AUCTION_NOT_OPEN', `auction is ${a.status}`);
    const seller = this.store.agents.get(a.sellerAgentId);
    if (seller && seller.principal_user_id === agent.principal_user_id) return fail('SELF_BID', 'principal cannot bid on their own listing');
    if (!isPosInt(amount)) return fail('INVALID_AMOUNT', 'amount must be a positive integer');

    const now = this.clock.now();
    const bid = { bidId: randomUUID(), agentId, amount, at: now, bidType, idempotencyKey };

    if (a.auctionType === 'ENGLISH') {
      const min = this.minNextBid(a, now);
      if (amount < min) return fail('BID_TOO_LOW', `minimum bid is ${min}`, { minRequired: min });
      if (a.bids.at(-1)?.agentId === agentId) return fail('ALREADY_HIGHEST', 'agent already holds the highest bid');
      a.bids.push(bid);
      a.currentPrice = amount;
      const end = effectiveEnd(a);
      if (a.antiSnipe.windowMs > 0 && end - now <= a.antiSnipe.windowMs) {
        a.extendedUntil = end + a.antiSnipe.extendMs;
        a.status = 'EXTENDING';
        this.emit('auction.extended', { auctionId, extendedUntil: a.extendedUntil });
      }
      this.emit('auction.bid', { auctionId, agentId, amount });
    } else if (a.auctionType === 'DUTCH') {
      const price = this.currentPrice(a, now);
      if (amount < price) return fail('BID_TOO_LOW', `current price is ${price}`, { minRequired: price });
      bid.amount = price; // accepting the current asking price
      a.bids.push(bid);
      this._close(a, now, bid); // settle first: listeners reacting to the bid must see a closed auction
      this.emit('auction.bid', { auctionId, agentId, amount: price });
    } else {
      if (amount < a.reservePrice) return fail('BELOW_RESERVE', `sealed bid must be at least ${a.reservePrice}`, { minRequired: a.reservePrice });
      const i = a.bids.findIndex((b) => b.agentId === agentId);
      if (i >= 0) a.bids.splice(i, 1); // a sealed bid may be revised until close
      a.bids.push(bid);
      this.emit('auction.bid', { auctionId, agentId, sealed: true });
    }
    return { ok: true, bid: pubBid(bid), auction: this.view(a, agentId) };
  }
}

const pubBid = (b) => ({ agentId: b.agentId, amount: b.amount, at: b.at, bidType: b.bidType });
