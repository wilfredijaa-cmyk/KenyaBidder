import test, { after, before } from 'node:test';
import assert from 'node:assert/strict';
import { createApp } from '../src/app.js';
import { createServer } from '../src/server.js';
import { SystemClock } from '../src/clock.js';
import { Store } from '../src/store.js';

let app, server, base, timer;
const INTERNAL = 'test-internal-key';

before(async () => {
  app = createApp({ store: new Store(), clock: new SystemClock() });
  server = createServer(app, { internalKey: INTERNAL, whatsappVerifyToken: 'vt' });
  await new Promise((r) => server.listen(0, r));
  base = `http://127.0.0.1:${server.address().port}`;
  timer = setInterval(() => app.tick(), 20);
});
after(async () => {
  clearInterval(timer);
  for (const c of server.sseClients) c.res.end();
  await new Promise((r) => server.close(r));
});

async function api(path, { method = 'GET', token, body, headers = {} } = {}) {
  const res = await fetch(base + path, {
    method,
    headers: { 'content-type': 'application/json', ...(token ? { authorization: `Bearer ${token}` } : {}), ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch { data = text; }
  return { status: res.status, data };
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function signup(name, phone) {
  const r = await api('/api/users', { method: 'POST', body: { name, phone, email: `${name}@x.co` } });
  assert.equal(r.status, 201);
  return r.data.token;
}

test('full HTTP journey: list -> auto-bid -> settle -> match -> confirm -> reveal -> outcome', async () => {
  const sTok = await signup('Amina', '+254700000001');
  const b1Tok = await signup('David', '+254700000002');
  const b2Tok = await signup('Wanjiru', '+254700000003');

  const seller = (await api('/api/agents', { method: 'POST', token: sTok, body: { type: 'SELLER', constraints: { reserve_floor: 500 } } })).data;
  const mk = async (tok, ceiling) => (await api('/api/agents', { method: 'POST', token: tok, body: { type: 'BIDDER', constraints: { budget_ceiling: ceiling }, memory: { strategy: 'baseline', watch: { category: 'electronics' } } } })).data;
  const d = await mk(b1Tok, 5000);
  const w = await mk(b2Tok, 3000);

  const listing = await api('/api/auctions', { method: 'POST', token: sTok, body: { sellerAgentId: seller.agent_id, productSpec: { category: 'electronics', title: 'Phones', quantity: 20 }, auctionType: 'ENGLISH', reservePrice: 1000, startPrice: 1000, minIncrement: 100, durationMs: 1500 } });
  assert.equal(listing.status, 201);
  const id = listing.data.auctionId;

  await app.orchestrator.idle();
  const live = (await api(`/api/auctions/${id}?agentId=${d.agent_id}`, { token: b1Tok })).data;
  assert.ok(live.bidCount >= 2, 'both agents bid');
  assert.equal(live.reservePrice, undefined, 'reserve hidden from bidder');

  await sleep(1700);
  const done = (await api(`/api/auctions/${id}`, { token: b1Tok })).data;
  assert.equal(done.status, 'SETTLED');
  assert.equal(done.result.winnerAgentId, d.agent_id); // higher ceiling wins
  assert.ok(done.result.price > 2500 && done.result.price <= 3150, `price ${done.result.price}`); // beats the runner-up (ceiling 3000), well under own ceiling

  const matches = (await api(`/api/agents/${d.agent_id}/matches`, { token: b1Tok })).data;
  assert.equal(matches.length, 1);
  const mid = matches[0].match_id;
  assert.equal((await api(`/api/matches/${mid}/confirm`, { method: 'POST', token: sTok, body: { agentId: d.agent_id } })).status, 403); // not my agent
  assert.equal((await api(`/api/matches/${mid}/confirm`, { method: 'POST', token: sTok, body: { agentId: seller.agent_id } })).data.status, 'SELLER_CONFIRMED');
  const revealed = (await api(`/api/matches/${mid}/confirm`, { method: 'POST', token: b1Tok, body: { agentId: d.agent_id } })).data;
  assert.equal(revealed.status, 'CONTACT_REVEALED');
  assert.equal(revealed.contact_reveal.seller_contact.phone, '+254700000001');
  for (const [tok, agentId] of [[sTok, seller.agent_id], [b1Tok, d.agent_id]]) {
    await api(`/api/matches/${mid}/report`, { method: 'POST', token: tok, body: { agentId, outcome: 'COMPLETED' } });
  }
  const me = (await api('/api/me', { token: sTok })).data;
  assert.equal(me.agents[0].reputation.completed_matches, 1);

  const audit = (await api(`/api/agents/${d.agent_id}/audit`, { token: b1Tok })).data;
  assert.ok(audit.length >= 1 && audit.every((e) => e.guardrail_decision));
  const kpis = (await api('/api/kpis', { token: sTok })).data;
  assert.ok(kpis.agentPerformance.bidLatencyMs.p95 < 200);
});

test('auth, ownership and validation', async () => {
  assert.equal((await api('/api/me')).status, 401);
  assert.equal((await api('/api/me', { token: 'nope' })).status, 401);
  assert.equal((await api('/api/users', { method: 'POST', body: {} })).status, 400);
  const a = await signup('A', '+254711000001');
  const b = await signup('B', '+254711000002');
  const agent = (await api('/api/agents', { method: 'POST', token: a, body: { type: 'BIDDER', constraints: { budget_ceiling: 100 } } })).data;
  assert.equal((await api(`/api/agents/${agent.agent_id}`, { token: b })).status, 403);
  assert.equal((await api(`/api/agents/${agent.agent_id}`, { method: 'PATCH', token: b, body: { status: 'PAUSED' } })).status, 403);
  assert.equal((await api('/api/agents', { method: 'POST', token: a, body: { type: 'BIDDER', constraints: { budget_ceiling: -1 } } })).status, 400);
  assert.equal((await api('/api/agents', { method: 'POST', token: a, body: { type: 'WIZARD' } })).status, 400);
  assert.equal((await api('/api/nothing', { token: a })).status, 404);
  const bad = await fetch(base + '/api/agents', { method: 'POST', headers: { authorization: `Bearer ${a}` }, body: '{oops' });
  assert.equal(bad.status, 400);
  assert.equal((await api(`/api/agents/${agent.agent_id}`, { method: 'PATCH', token: a, body: { constraints: { escalation_threshold_pct: 0 } } })).status, 400);
  assert.equal((await api('/../../etc/passwd')).status, 404);
  assert.equal((await fetch(base + '/%2e%2e/%2e%2e/etc/passwd')).status, 404);
});

test('manual bid override still passes the guardrails', async () => {
  const sTok = await signup('S', '+254722000001');
  const bTok = await signup('B', '+254722000002');
  const seller = (await api('/api/agents', { method: 'POST', token: sTok, body: { type: 'SELLER' } })).data;
  const buyer = (await api('/api/agents', { method: 'POST', token: bTok, body: { type: 'BIDDER', constraints: { budget_ceiling: 2000 } } })).data;
  const l = (await api('/api/auctions', { method: 'POST', token: sTok, body: { sellerAgentId: seller.agent_id, productSpec: { category: 'x', title: 'y', quantity: 1 }, auctionType: 'ENGLISH', startPrice: 100, durationMs: 60_000 } })).data;
  const over = await api(`/api/auctions/${l.auctionId}/bid`, { method: 'POST', token: bTok, body: { agentId: buyer.agent_id, amount: 9999 } });
  assert.equal(over.status, 422);
  assert.equal(over.data.code, 'CEILING_EXCEEDED');
  const ok = await api(`/api/auctions/${l.auctionId}/bid`, { method: 'POST', token: bTok, body: { agentId: buyer.agent_id, amount: 150 } });
  assert.equal(ok.status, 200);
  const low = await api(`/api/auctions/${l.auctionId}/bid`, { method: 'POST', token: sTok, body: { agentId: seller.agent_id, amount: 200 } });
  assert.ok(low.status >= 400);
});

test('SSE streams auction events and owner notifications', async () => {
  const sTok = await signup('SS', '+254733000001');
  const bTok = await signup('BB', '+254733000002');
  const seller = (await api('/api/agents', { method: 'POST', token: sTok, body: { type: 'SELLER' } })).data;
  const buyer = (await api('/api/agents', { method: 'POST', token: bTok, body: { type: 'BIDDER', constraints: { budget_ceiling: 5000 }, memory: { strategy: 'baseline', watch: { category: 'sse' } } } })).data;
  const ctl = new AbortController();
  const res = await fetch(`${base}/api/events?token=${bTok}`, { signal: ctl.signal });
  assert.equal(res.headers.get('content-type'), 'text/event-stream');
  const reader = res.body.getReader();
  await api('/api/auctions', { method: 'POST', token: sTok, body: { sellerAgentId: seller.agent_id, productSpec: { category: 'sse', title: 'x', quantity: 1 }, auctionType: 'ENGLISH', startPrice: 100, durationMs: 60_000 } });
  let buf = '';
  const deadline = Date.now() + 3000;
  while (Date.now() < deadline && !(buf.includes('event: auction') && buf.includes('event: notification'))) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += new TextDecoder().decode(value);
  }
  ctl.abort();
  assert.match(buf, /event: auction/);
  assert.match(buf, /event: notification/);
  assert.ok(!buf.includes('reservePrice'), 'public stream must not leak the reserve');
  assert.ok(buyer.agent_id);
});

test('MCP over HTTP: submit_bid needs the internal key; tools/list hides it', async () => {
  const tok = await signup('M', '+254744000001');
  const list = await api('/mcp/auction', { method: 'POST', token: tok, body: { jsonrpc: '2.0', id: 1, method: 'tools/list' } });
  const names = list.data.result.tools.map((t) => t.name);
  assert.ok(names.includes('get_auction_detail') && !names.includes('submit_bid'));
  const internal = await api('/mcp/auction', { method: 'POST', headers: { 'x-internal-key': INTERNAL }, body: { jsonrpc: '2.0', id: 2, method: 'tools/list' } });
  assert.ok(internal.data.result.tools.some((t) => t.name === 'submit_bid'));
  assert.equal((await api('/mcp/auction', { method: 'POST', headers: { 'x-internal-key': 'wrong' }, body: { jsonrpc: '2.0', id: 3, method: 'tools/list' } })).status, 401);
  assert.equal((await api('/mcp/nowhere', { method: 'POST', token: tok, body: {} })).status, 404);
});

test('WhatsApp webhook: verify handshake and inbound command routed to the canonical agent', async () => {
  const tok = await signup('W', '+254755000001');
  const agent = (await api('/api/agents', { method: 'POST', token: tok, body: { type: 'BIDDER', constraints: { budget_ceiling: 1000 } } })).data;
  assert.equal((await api(`/api/agents/${agent.agent_id}/channels`, { method: 'POST', token: tok, body: { channel: 'WHATSAPP', externalId: '254755000001' } })).status, 200);
  assert.equal((await fetch(`${base}/webhooks/whatsapp?hub.verify_token=vt&hub.challenge=abc`)).status, 200);
  assert.equal((await fetch(`${base}/webhooks/whatsapp?hub.verify_token=bad&hub.challenge=abc`)).status, 403);
  const payload = { entry: [{ changes: [{ value: { messages: [{ from: '254755000001', text: { body: 'ceiling 2500' } }] } }] }] };
  const r = await api('/webhooks/whatsapp', { method: 'POST', body: payload });
  assert.match(r.data.reply, /2500/);
  const after = (await api(`/api/agents/${agent.agent_id}`, { token: tok })).data;
  assert.equal(after.constraints.budget_ceiling, 2500);
  assert.equal(after.durable_memory.lastChannel, 'WHATSAPP');
  const web = await api(`/api/agents/${agent.agent_id}/message`, { method: 'POST', token: tok, body: { text: 'status' } });
  assert.match(web.data.reply, /2500/);
});

test('static UI is served', async () => {
  const r = await fetch(base + '/');
  assert.equal(r.status, 200);
  assert.match(r.headers.get('content-type'), /html/);
});
