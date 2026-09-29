# KenyaBidder

An AI-agent auction **matchmaking** platform. Every buyer and seller gets a persistent agent that trades on their
behalf; the platform runs real auction mechanics to find the counterparty and the price, then introduces the two
parties to settle directly. **It never holds funds** — payment and delivery happen off-platform.

The product spec lives in [`kenyabidder-full-spec.md`](kenyabidder-full-spec.md) (also `.pdf` / `.pptx`).
This repository is the implementation: **Python · NiceGUI · FastMCP**.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python -m kenyabidder                     # console on http://127.0.0.1:8080
python -m kenyabidder --mcp-port 8765     # …and serve KenyaBidder's own MCP endpoint
pytest                                    # unit tests + real-browser end-to-end tests (skipped without Chromium)
```

The **first account you register becomes the administrator**. Everything is stored in one **DuckDB** file,
`data/kenyabidder.duckdb` (`--data ''` for in-memory only). PostgreSQL is supported by the same code path — see [Database](#database).

## What you can configure in the UI

| Where | What |
|---|---|
| **Admin → LLMs** | Register models: Anthropic, or any OpenAI-compatible endpoint (OpenAI, Ollama, vLLM, OpenRouter…). Key stored server-side (never shown again) or read from an env var. Per-model role availability, enable/disable, live connectivity test. |
| **Admin → MCP servers** | Register external MCP servers (HTTP, or stdio when `KENYABIDDER_ALLOW_STDIO=1`), discover their tools, switch individual tools off, restrict by agent type. The built-in KenyaBidder server is always present. |
| **Admin → Knowledge bases** | Create KBs and add documents by pasting text, uploading files, or fetching a URL. BM25 retrieval with a built-in test box. |
| **My agents → Brain, tools & knowledge** | Per agent: default **LLM**; the **algorithm per auction type** (`baseline`, `heuristic`, or `llm`, each with an optional LLM override); the exact **MCP tools** it may call; the **knowledge bases** it may search. Sellers pick a listing advisor (`rules` or `llm`). |
| **My agents → Rules** | Hard limits enforced by the guardrail: budget ceiling, reserve floor, escalation threshold, allowed auction types, watch spec, auto-relist. |

## Selling LLM tokens

Agents think with LLMs, and LLM calls cost money — so the platform **sells tokens** and **gates agents on them**.

* **Tokens are per user, per LLM.** An admin registers an LLM as *metered* (users buy tokens for it) or *free* (platform-funded). Output tokens
  can be weighted (default ×1; set ×5 to mirror provider pricing) and each LLM records *your* cost per 1k tokens for the margin report.
* **An agent mapped to a metered LLM operates only if its owner holds enough tokens** (at least one call's worth). Every provider call reserves a
  hold first, then settles the provider-reported usage — concurrent decisions cannot overspend one wallet, a failed/timed-out call costs nothing,
  and a provider that omits usage is charged a pessimistic estimate (never zero). Deterministic algorithms (`baseline`, `heuristic`) never need tokens.
* **Policy** (Admin → Billing): `block` (default) — the agent stops making LLM decisions and tells its owner once; `fallback` — it keeps working with
  the free heuristic. After a top-up, blocked agents pick their still-open auctions back up automatically.
* **Protections for the owner's wallet:** optional per-agent *daily token cap*, and a platform limit of LLM decisions per agent per hour (default 60)
  so a flood of spam listings cannot make one agent burn its owner's wallet. Listing text is length-capped for the same reason.
* **Buying:** Admin creates *packs* (e.g. 100,000 tokens for KES 500). Users pay by **M-Pesa STK push**, or by paying your Till/Paybill and entering
  the M-Pesa code for admin approval. `KENYABIDDER_DEV_PAYMENTS=1` adds an instant fake payment for development only.
* **Free trial:** an optional sign-up grant, once per account *and* per phone number, with a platform-wide daily budget.
* **Money-grade storage:** balances, the append-only ledger and orders live in the SQL database (DuckDB, transactional; see Database) — the database itself forbids negative
  balances and double-crediting one payment. Admin → Audit log verifies every balance against the ledger; every money/user/pricing action is logged.
* **Reports:** revenue, estimated LLM cost and margin per LLM, tokens outstanding (service you owe), and CSV export of ledger and orders.

This is the platform's own revenue and is separate from auction settlement: buyers and sellers still pay *each other* directly.

### M-Pesa setup
Set `MPESA_CONSUMER_KEY`, `MPESA_CONSUMER_SECRET`, `MPESA_SHORTCODE`, `MPESA_PASSKEY`, `MPESA_CALLBACK_BASE_URL` (public https origin), and optionally
`MPESA_ENV` (`sandbox`|`production`), `MPESA_TRANSACTION_TYPE` (`CustomerBuyGoodsOnline` for a Till) and `MPESA_PARTY_B`. Register the callback URL shown in
Admin → Billing with Safaricom. **The client is written from the Daraja documentation and tested against a mocked Safaricom API — run it against the
Daraja sandbox with your own credentials before going live.** The callback body is never trusted: it only triggers an authenticated STK-status query,
and a periodic reconciler recovers lost callbacks.

## Architecture

```
 Web (NiceGUI) ─┐                          ┌─ Auction engine (English / Dutch / 1st-price / Vickrey)
 WhatsApp ──────┼─▶ Channel Router ─▶ Harness ─┼─ Match service (confirm → reveal → outcome → reputation)
 FastMCP (HTTP)─┘        │            │       └─ Market intel · Knowledge bases
                         ▼            ▼
                   Orchestrator ─▶ Strategy ─▶ LLM (proposes only)      ┌─▶ assigned MCP tools (read-only)
                         │             └───────────── tool loop ───────┴─▶ assigned knowledge bases
                         ▼
        Guardrail Interceptor (rules, not an LLM) ─▶ Deterministic Execution Engine ─▶ Auction engine
```

* **The LLM proposes; the harness decides and acts.** An LLM strategy returns a schema-constrained *conditional
  action* ("bid up to X, snipe in the last 5 s"). The Guardrail Interceptor validates it, and the Execution Engine
  fires it — no LLM call is ever in the bid path. Every proposal, decision and result is audit-logged.
* **Least privilege for tools.** An agent's LLM sees *only* the tools assigned to it. State-changing tools
  (`submit_bid`, `create_match`, `reveal_contact`, …) exist only on the *internal* FastMCP server and can never be
  assigned. Tool output, KB snippets and listing text are wrapped as `<untrusted_*>` data.
* **Failure never blocks a time-critical auction.** Any LLM error, timeout, invalid output or missing key falls back
  to the deterministic heuristic strategy and says so in the plan.
* **The agent, not the channel, is the identity.** Web and WhatsApp share one memory and one command set.

### Auction algorithms
Forward auctions (a seller lists, buyer agents bid up): English (with anti-sniping), Dutch, first-price sealed, second-price sealed (Vickrey).

**Reverse auctions / RFQs** (a buyer posts what it needs and the most it will pay; *supplier* agents bid the price down):
`REVERSE_ENGLISH` (open; each quote must undercut the best by the minimum decrement; anti-sniping) and `REVERSE_SEALED` (lowest sealed quote wins,
earliest breaks ties). Buyers post from **Auctions → Request quotes**; sellers configure a supplier strategy per RFQ type
(`baseline` undercut-to-floor, `heuristic` priced off comparable clearing prices, or `llm`) and a category watch, exactly like buyers.
The supplier's **price floor** is a hard guardrail limit; a buyer's `max_price` may not exceed its budget ceiling. In the resulting match the
winning supplier is the *seller* and the RFQ poster the *buyer*, so confirmation, contact reveal, outcome reports, disputes and reputation all work unchanged.
Multi-unit and combinatorial auctions remain reserved (they need a winner-determination solver).

## Trust & safety

* **Phone / email verification** — 6-digit codes (only an HMAC is stored), 10-minute expiry, 5 guesses, resend cooldown, hourly caps per account *and* per
  destination, and a platform-wide **daily SMS budget** against SMS-pumping fraud. With a real SMS gateway configured, a verified phone is required for the
  free-trial tokens and for exchanging contact details (Admin → Messaging can override).
* **Self-service password reset** — code to a *verified* phone/email; the answer never reveals whether an account exists; a reset ends every signed-in session.
* **Verified businesses** — users apply with a KRA PIN / business registration / ID number; an admin checks it out-of-band and approves. The badge shows
  on listings and contact details; listings and RFQs can be **verified-only**, and the platform can require verification above a KES amount.
* **Disputes** — after contact exchange either party can open a dispute (30-day window); the other side answers (7 days); an administrator rules
  (completed / seller at fault / buyer at fault / both / no fault). The ruling — not a unilateral report — updates reputation, and automatic outcome logic is paused meanwhile.
* **Privacy (Kenya DPA 2019)** — *Profile → Your data*: download everything we hold as JSON, or delete the account (identifiers erased/anonymised, financial
  records kept under an anonymous id, refused while money/deals/disputes are unresolved). Register with the ODPC as a data controller/processor and have the Terms
  reviewed by a lawyer before launch — those are organisational steps no code can do.

## Plans, languages, operations

* **Subscription plans** — a token pack with a *plan* (days + more agents + higher per-agent LLM decision allowance). Same audited payment flow; buying again
  extends; reminder 3 days before it ends; a refund cancels it.
* **Kiswahili** — language toggle (EN/SW) on every page; navigation, sign-in, listing/bidding, wallet, profile and SMS templates are translated. ⚠ First-draft
  translations without a native reviewer — have a Kiswahili speaker review `kenyabidder/i18n.py` before launch. Dynamic sentences still show in English.
* **Health & metrics** — `/healthz` (liveness), `/readyz` (database, tick loop, state flush), Prometheus `/metrics` (loopback only, or bearer
  `KENYABIDDER_METRICS_TOKEN`), JSON logs with `KENYABIDDER_LOG_JSON=1`.
* **Backups** — verified, retained (14 by default) DuckDB backups every 24h under `data/backups` (Admin → Health & backups; copy them off the machine!).
  Restore with the app stopped: `python -m kenyabidder restore data/backups/<file>`.

## Database

All money data (balances, the append-only token ledger, orders, M-Pesa receipt claims) and **all application state** (users, agents, auctions, matches…) live
in the SQL database. The engine keeps a working set in memory for bid latency and flushes *only what changed* about once a second (finished auctions are frozen;
append-only logs are trimmed in step); a graceful shutdown flushes everything. Money writes are transactional and never go through that path.

* **DuckDB** (default): `duckdb:///data/kenyabidder.duckdb`, embedded, single process. A **writer lease** stops a second process from silently forking the state.
* **PostgreSQL**: `KENYABIDDER_DATABASE_URL=postgresql://user:pw@host/db` (`pip install "psycopg[binary]"`). The SQL is one portable subset (`?` placeholders, sequences,
  `ON CONFLICT`, no partial indexes) with versioned migrations; the Postgres adapter is written but **only DuckDB is exercised in this repository's tests** — run the suite against your
  server with `KENYABIDDER_TEST_DATABASE_URL=postgresql://…` before relying on it.
* Multiple *processes* can safely share the database for money/orders, but the in-memory auction engine is a single writer by design (the lease enforces it). Scaling the engine
  horizontally means sharding auctions by id — the next architectural step, not attempted here.
* Upgrading from the earlier JSON + SQLite version imports `state.json` and `wallet.db` automatically on first start (files are renamed `.imported`, never deleted).

## Configuration

| Variable | Purpose |
|---|---|
| `KENYABIDDER_DATA` | Data location (default `data/state.json`); the database file `kenyabidder.duckdb` and `backups/` live beside it. Contains LLM/MCP credentials → `0600`. |
| `KENYABIDDER_DATABASE_URL` | `duckdb:///path.duckdb` (default beside the data file) or `postgresql://…`. |
| `KENYABIDDER_BACKUP_DIR` / `_KEEP` / `_EVERY_HOURS` | Backup location, how many to retain (14) and the interval (24). |
| `KENYABIDDER_FORCE_LEASE=1` | Take over the writer lease after a crash left it held (never while another instance is alive). |
| `AT_USERNAME`, `AT_API_KEY`, `AT_SENDER_ID`, `AT_SANDBOX=1` | Africa's Talking SMS gateway (codes and alerts). Without them SMS runs in development mode (codes are shown on screen, nothing is sent). |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_STARTTLS` | Email for verification codes and alerts. |
| `KENYABIDDER_METRICS_TOKEN` | Bearer token to scrape `/metrics` remotely (otherwise loopback only). `KENYABIDDER_LOG_JSON=1` for JSON logs. |
| `KENYABIDDER_MCP_PORT` / `KENYABIDDER_MCP_KEY` | Serve KenyaBidder's read-only FastMCP endpoint on `127.0.0.1:<port>/mcp`; bearer key (generated if unset). |
| `KENYABIDDER_SECRET` | Session-cookie secret (generated and persisted if unset). |
| `KENYABIDDER_ALLOW_STDIO=1` | Allow admins to register stdio MCP servers (they execute local commands). |
| `KENYABIDDER_DEV_PAYMENTS=1` | Offer an instant fake payment method. **Never in production.** |
| `MPESA_*` | M-Pesa STK push (see above). |
| `KENYABIDDER_ALLOW_PRIVATE_FETCH=1` | Allow knowledge-base URL ingestion from private addresses (off by default — SSRF guard). |
| `WHATSAPP_APP_SECRET` | **Required** to accept WhatsApp webhooks (verifies `X-Hub-Signature-256`). `KENYABIDDER_INSECURE_WEBHOOK=1` skips it for local dev only. |
| `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` | Cloud API verification handshake and outbound messages. |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | Default key sources for LLMs registered without an explicit key. |

## Next steps

1. **Run the suite against PostgreSQL** (`KENYABIDDER_TEST_DATABASE_URL`) and shard the auction engine by id for multi-process scale.
2. **Native review of the Kiswahili strings**, and translating dynamic sentences / the agent-generated messages.
3. **Automatic recurring billing** (M-Pesa Ratiba) for subscriptions; today a renewal is a fresh purchase.
4. **Document upload for business verification** (today an admin checks the registration number out-of-band) and dispute evidence attachments.
5. Multi-unit and combinatorial auctions (winner-determination solver), per-IP sign-in limits, and a proper billing/accounting export.

## Assumptions & known limits

* Amounts are integer **KES**; the spec does not define a currency.
* The engine's working set is in memory and flushed to the database about every second, so a hard crash can lose at most ~1 second of *non-money* state
  (money is transactional). Single active engine process (enforced by the writer lease).
* The Execution Engine calls the auction engine in-process. The internal FastMCP server (`build_server(app, internal=True)`)
  exposes the same operations for a future out-of-process harness.
* Fall-through fault assumption: a `FELL_THROUGH`/`NO_RESPONSE` report counts against the reporter's counterparty.
* Market Intelligence has no forecasting yet; new-tier value ceiling is a single configurable number, not per category.
* Match outcomes need both sides' reports (72h grace for a silent side); one side's word alone never affects a reputation.
* Price statistics have no recency window and mix auction types (they are normalised per unit).
* Sign-in is name + password with per-name lockout (an attacker can lock out a known name — add per-IP limits behind your proxy); put the app behind TLS before exposing it. Run the KB URL fetcher
  and stdio MCP support only on hosts you trust the admins of.
