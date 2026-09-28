import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { timingSafeEqual } from 'node:crypto';
import { createApp } from './app.js';
import { Store } from './store.js';
import { SystemClock } from './clock.js';
import { AppError, badRequest, forbidden, notFound } from './errors.js';
import { handleRpc } from './mcp/registry.js';

const WEB_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', 'web');
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml' };
const MAX_BODY = 1_000_000;

const strEq = (a, b) => a.length === b.length && timingSafeEqual(Buffer.from(a), Buffer.from(b));

/** Build the HTTP server around an app. Exported for tests (listen on port 0). */
export function createServer(app, { internalKey = process.env.KENYABIDDER_INTERNAL_KEY ?? null, whatsappVerifyToken = process.env.WHATSAPP_VERIFY_TOKEN ?? null } = {}) {
  const sse = new Set(); // { res, userId }

  // Fan out: notifications to the owning user; public auction events to everyone (redacted).
  app.router.webListeners.add((n) => {
    const agent = app.store.agents.get(n.agentId);
    for (const c of sse) if (c.userId === agent?.principal_user_id) send(c.res, 'notification', n);
  });
  for (const ev of ['auction.created', 'auction.started', 'auction.bid', 'auction.price', 'auction.extended', 'auction.settled', 'auction.cancelled']) {
    app.engine.on(ev, ({ auctionId }) => {
      const a = app.store.auctions.get(auctionId);
      if (!a) return;
      const payload = { event: ev, auction: app.engine.view(a) };
      for (const c of sse) send(c.res, 'auction', payload);
    });
  }

  const json = (res, status, body) => {
    const s = JSON.stringify(body);
    res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'content-length': Buffer.byteLength(s) });
    res.end(s);
  };

  async function readJson(req) {
    const chunks = [];
    let size = 0;
    for await (const c of req) {
      size += c.length;
      if (size > MAX_BODY) throw new AppError(413, 'BODY_TOO_LARGE', 'request body too large');
      chunks.push(c);
    }
    if (!chunks.length) return {};
    try {
      return JSON.parse(Buffer.concat(chunks).toString('utf8'));
    } catch {
      throw badRequest('INVALID_JSON', 'body is not valid JSON');
    }
  }

  const authUser = (req, url) => {
    const h = req.headers.authorization ?? '';
    const token = h.startsWith('Bearer ') ? h.slice(7) : url.searchParams.get('token');
    const user = token ? app.agents.userForToken(token) : null;
    if (!user) throw new AppError(401, 'UNAUTHENTICATED', 'missing or invalid bearer token');
    return user;
  };

  const publicUser = (u) => ({ id: u.id, name: u.name, contact: u.contact });
  const agentView = (a) => ({ ...a, durable_memory: { ...a.durable_memory, conversation: a.durable_memory.conversation.slice(-20) } });

  async function route(req, res, url) {
    const { pathname } = url;
    const m = req.method;
    const seg = pathname.split('/').filter(Boolean);

    if (m === 'GET' && pathname === '/healthz') return json(res, 200, { ok: true });

    // ---- MCP servers (JSON-RPC 2.0) ----
    if (m === 'POST' && seg[0] === 'mcp' && seg.length === 2) {
      const tools = app.mcp[seg[1]];
      if (!tools) throw notFound('MCP_NOT_FOUND', 'unknown MCP server');
      const key = req.headers['x-internal-key'];
      const internal = !!(internalKey && typeof key === 'string' && strEq(key, internalKey));
      if (!internal) authUser(req, url);
      return json(res, 200, handleRpc(tools, await readJson(req), { internal }));
    }

    // ---- WhatsApp webhook (Cloud API shape or simple {from,text}) ----
    if (seg[0] === 'webhooks' && seg[1] === 'whatsapp') {
      if (m === 'GET') {
        if (whatsappVerifyToken && url.searchParams.get('hub.verify_token') === whatsappVerifyToken) {
          res.writeHead(200, { 'content-type': 'text/plain' });
          return res.end(url.searchParams.get('hub.challenge') ?? '');
        }
        throw forbidden('VERIFY_FAILED', 'webhook verification failed');
      }
      if (m === 'POST') {
        const body = await readJson(req);
        const msg = body.entry?.[0]?.changes?.[0]?.value?.messages?.[0];
        const from = msg?.from ?? body.from;
        const text = msg?.text?.body ?? body.text;
        if (!from || typeof text !== 'string') return json(res, 200, { ok: true, ignored: true });
        const out = await app.router.handleInbound({ channel: 'WHATSAPP', externalId: String(from), text });
        if (out.agentId) await app.router.whatsapp.send(String(from), out.reply);
        return json(res, 200, { ok: true, reply: out.reply });
      }
    }

    // ---- public bootstrap ----
    if (m === 'POST' && pathname === '/api/users') {
      const b = await readJson(req);
      const { user, token } = app.agents.createUser(b);
      return json(res, 201, { user: publicUser(user), token });
    }

    if (!pathname.startsWith('/api/')) throw notFound('NOT_FOUND', 'no such route');
    const user = authUser(req, url);
    const body = m === 'GET' || m === 'DELETE' ? {} : await readJson(req);
    const owned = (id) => app.agents.ownedAgent(id, user.id);

    if (m === 'GET' && pathname === '/api/me') return json(res, 200, { user: publicUser(user), agents: app.agents.agentsFor(user.id).map(agentView) });

    // events (SSE)
    if (m === 'GET' && pathname === '/api/events') {
      res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache', connection: 'keep-alive' });
      res.write(': connected\n\n');
      const client = { res, userId: user.id };
      sse.add(client);
      const ping = setInterval(() => res.write(': ping\n\n'), 25_000);
      ping.unref();
      req.on('close', () => {
        clearInterval(ping);
        sse.delete(client);
      });
      return;
    }

    // agents
    if (m === 'POST' && pathname === '/api/agents') return json(res, 201, agentView(app.agents.createAgent({ userId: user.id, type: body.type, constraints: body.constraints, memory: body.memory })));
    if (seg[0] === 'api' && seg[1] === 'agents' && seg[2]) {
      const agent = owned(seg[2]);
      const id = agent.agent_id;
      if (seg.length === 3 && m === 'GET') return json(res, 200, agentView(agent));
      if (seg.length === 3 && m === 'PATCH') return json(res, 200, agentView(app.agents.update(id, body)));
      if (seg[3] === 'channels' && m === 'POST') return json(res, 200, agentView(app.agents.linkChannel(id, body.channel, body.externalId)));
      if (seg[3] === 'triggers' && m === 'GET') return json(res, 200, app.execution.triggersFor(id));
      if (seg[3] === 'audit' && m === 'GET') return json(res, 200, app.audit.forAgent(id, Number(url.searchParams.get('limit')) || 100));
      if (seg[3] === 'notifications' && m === 'GET') return json(res, 200, app.router.notificationsFor(id));
      if (seg[3] === 'approvals' && m === 'GET') return json(res, 200, [...app.store.approvals.values()].filter((a) => a.agentId === id).sort((a, b) => b.createdAt - a.createdAt));
      if (seg[3] === 'matches' && m === 'GET') return json(res, 200, app.matches.matchesFor(id));
      if (seg[3] === 'summary' && m === 'GET') return json(res, 200, { summary: app.status(agent) });
      if (seg[3] === 'message' && m === 'POST') {
        const link = { channel: 'WEB', externalId: `web:${id}` };
        agent.durable_memory.lastChannel = 'WEB';
        const reply = await app.router.command(agent, String(body.text ?? ''));
        const conv = agent.durable_memory.conversation;
        conv.push({ role: 'user', channel: link.channel, text: String(body.text ?? '').slice(0, 500), at: app.clock.now() }, { role: 'agent', channel: link.channel, text: reply, at: app.clock.now() });
        return json(res, 200, { reply });
      }
    }

    // auctions
    if (m === 'GET' && pathname === '/api/auctions') {
      const q = url.searchParams;
      const num = (k) => (q.has(k) && q.get(k) !== '' ? Number(q.get(k)) : undefined);
      const viewer = q.get('agentId');
      if (viewer) owned(viewer);
      const filters = { category: q.get('category') || undefined, q: q.get('q') || undefined, maxPrice: num('maxPrice'), minQuantity: num('minQuantity'), includeScheduled: q.get('includeScheduled') === '1', auctionTypes: q.get('types')?.split(',').filter(Boolean) };
      return json(res, 200, app.engine.listActiveAuctions(filters, viewer));
    }
    if (m === 'GET' && pathname === '/api/auctions/all') {
      const viewer = url.searchParams.get('agentId');
      if (viewer) owned(viewer);
      app.engine.tick();
      return json(res, 200, [...app.store.auctions.values()].map((a) => app.engine.view(a, viewer)).sort((a, b) => b.createdAt - a.createdAt));
    }
    if (m === 'POST' && pathname === '/api/auctions') {
      owned(body.sellerAgentId);
      return json(res, 201, app.engine.createListing(body));
    }
    if (m === 'POST' && pathname === '/api/recommend-listing') return json(res, 200, app.intel.recommendListing(body));
    if (seg[1] === 'auctions' && seg[2]) {
      const viewer = url.searchParams.get('agentId') ?? body.agentId ?? null;
      if (viewer) owned(viewer);
      if (seg.length === 3 && m === 'GET') return json(res, 200, app.engine.getAuctionDetail(seg[2], viewer));
      if (seg[3] === 'withdraw' && m === 'POST') return json(res, 200, app.engine.withdrawListing({ listingId: seg[2], sellerAgentId: body.agentId, reason: body.reason }));
      if (seg[3] === 'watch' && m === 'POST') {
        if (!viewer) throw badRequest('AGENT_REQUIRED', 'agentId required');
        return json(res, 200, await app.orchestrator.consider(viewer, seg[2], { manual: true }));
      }
      if (seg[3] === 'bid' && m === 'POST') {
        // Manual human override: still goes through the Guardrail Interceptor.
        if (!viewer) throw badRequest('AGENT_REQUIRED', 'agentId required');
        const d = app.guardrail.evaluate({ agentId: viewer, auctionId: seg[2], action: 'place_bid', amount: body.amount, source: 'user' });
        const idem = `manual:${viewer}:${body.idempotencyKey ?? Date.now()}`;
        let result;
        if (d.decision === 'APPROVED') result = app.execution._submitWithRetry({ auctionId: seg[2], agentId: viewer, amount: body.amount, idempotencyKey: idem });
        app.audit.record({ agentId: viewer, auctionId: seg[2], proposedAction: { action: 'manual_bid', amount: body.amount }, guardrailDecision: d.decision, rejectionReason: d.decision === 'APPROVED' ? null : `${d.code}: ${d.reason}`, executedAction: result ? { amount: body.amount, idempotencyKey: idem } : null, executionResult: result ? { ok: result.ok, code: result.code ?? 'OK' } : null });
        if (d.decision !== 'APPROVED') return json(res, 422, { ok: false, code: d.code, message: d.reason });
        return json(res, result.ok ? 200 : 409, result);
      }
    }

    // approvals
    if (seg[1] === 'approvals' && seg[2] && seg[3] && m === 'POST') {
      const ap = app.store.approvals.get(seg[2]);
      if (!ap) throw notFound('APPROVAL_NOT_FOUND', 'approval not found');
      owned(ap.agentId);
      if (!['approve', 'reject'].includes(seg[3])) throw notFound('NOT_FOUND', 'no such route');
      return json(res, 200, await app.orchestrator.resolveApproval(seg[2], seg[3] === 'approve'));
    }

    // matches
    if (seg[1] === 'matches' && seg[2] && m === 'POST') {
      const agentId = body.agentId;
      owned(agentId);
      if (seg[3] === 'confirm') return json(res, 200, app.matches.confirmMatch(seg[2], agentId));
      if (seg[3] === 'report') return json(res, 200, app.matches.reportOutcome(seg[2], agentId, body.outcome, body.notes));
    }

    // market + kpis
    if (m === 'GET' && seg[1] === 'market' && seg[2]) return json(res, 200, { stats: app.intel.getHistoricalClearingPrices({ category: seg[2] }), demand: app.intel.getDemandSignal({ category: seg[2] }) });
    if (m === 'GET' && pathname === '/api/kpis') return json(res, 200, app.kpis());

    throw notFound('NOT_FOUND', `no route for ${m} ${pathname}`);
  }

  async function serveStatic(res, pathname) {
    const rel = normalize(pathname === '/' ? '/index.html' : pathname).replace(/^([/\\])+/, '');
    if (rel.includes('..')) throw notFound('NOT_FOUND', 'not found');
    const file = join(WEB_DIR, rel);
    try {
      const data = await readFile(file);
      res.writeHead(200, { 'content-type': MIME[extname(file)] ?? 'application/octet-stream', 'cache-control': 'no-cache' });
      res.end(data);
    } catch {
      throw notFound('NOT_FOUND', 'not found');
    }
  }

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    try {
      if (req.method === 'GET' && !url.pathname.startsWith('/api/') && !url.pathname.startsWith('/webhooks/') && url.pathname !== '/healthz') {
        return await serveStatic(res, url.pathname);
      }
      await route(req, res, url);
    } catch (e) {
      if (res.headersSent) return res.end();
      if (e instanceof AppError) return json(res, e.status, { error: e.code, message: e.message });
      console.error('[server] unhandled', e);
      json(res, 500, { error: 'INTERNAL', message: 'internal error' });
    }
  });

  server.sseClients = sse;
  return server;
}

function send(res, event, data) {
  res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
}

// ---------- entrypoint ----------
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const dataFile = process.env.KENYABIDDER_DATA ?? null;
  const store = dataFile ? Store.load(dataFile) : new Store();
  const app = createApp({ store, clock: new SystemClock() });
  const server = createServer(app);
  const port = Number(process.env.PORT ?? 3000);

  const timer = setInterval(() => {
    try {
      app.tick();
    } catch (e) {
      console.error('[tick]', e);
    }
  }, Number(process.env.KENYABIDDER_TICK_MS ?? 50));
  const saver = dataFile ? setInterval(() => store.save(dataFile), 5000) : null;

  const shutdown = () => {
    clearInterval(timer);
    if (saver) clearInterval(saver);
    if (dataFile) store.save(dataFile);
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 500).unref();
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);

  server.listen(port, () => console.log(`KenyaBidder listening on http://localhost:${port}${dataFile ? ` (state: ${dataFile})` : ''}`));
}
