import { readFileSync, writeFileSync, existsSync, mkdirSync, renameSync } from 'node:fs';
import { dirname } from 'node:path';

const MAP_KEYS = ['users', 'agents', 'auctions', 'matches', 'triggers', 'approvals', 'idempotency', 'breakers'];
const LIST_KEYS = ['audit', 'notifications', 'marketHistory', 'outbox', 'revealLog'];

/** In-memory state store with JSON snapshot persistence (stand-in for Postgres + Redis, spec §15.1). */
export class Store {
  constructor() {
    for (const k of MAP_KEYS) this[k] = new Map();
    for (const k of LIST_KEYS) this[k] = [];
    this.tokens = new Map(); // bearer token -> userId
    this.considered = new Set(); // `${agentId}:${auctionId}` already run through strategy
    this.rateWindows = new Map(); // agentId -> [timestamps]
  }

  snapshot() {
    const out = {};
    for (const k of MAP_KEYS) out[k] = [...this[k].entries()];
    for (const k of LIST_KEYS) out[k] = this[k];
    out.tokens = [...this.tokens.entries()];
    out.considered = [...this.considered];
    return out;
  }

  static restore(data) {
    const s = new Store();
    for (const k of MAP_KEYS) if (data[k]) s[k] = new Map(data[k]);
    for (const k of LIST_KEYS) if (data[k]) s[k] = data[k];
    if (data.tokens) s.tokens = new Map(data.tokens);
    if (data.considered) s.considered = new Set(data.considered);
    return s;
  }

  save(path) {
    mkdirSync(dirname(path), { recursive: true });
    const tmp = `${path}.tmp`;
    writeFileSync(tmp, JSON.stringify(this.snapshot()));
    renameSync(tmp, path);
  }

  static load(path) {
    if (!existsSync(path)) return new Store();
    return Store.restore(JSON.parse(readFileSync(path, 'utf8')));
  }
}
