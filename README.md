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

The **first account you register becomes the administrator**. State is snapshotted to `data/state.json`
(`--data ''` for in-memory only).

## What you can configure in the UI

| Where | What |
|---|---|
| **Admin → LLMs** | Register models: Anthropic, or any OpenAI-compatible endpoint (OpenAI, Ollama, vLLM, OpenRouter…). Key stored server-side (never shown again) or read from an env var. Per-model role availability, enable/disable, live connectivity test. |
| **Admin → MCP servers** | Register external MCP servers (HTTP, or stdio when `KENYABIDDER_ALLOW_STDIO=1`), discover their tools, switch individual tools off, restrict by agent type. The built-in KenyaBidder server is always present. |
| **Admin → Knowledge bases** | Create KBs and add documents by pasting text, uploading files, or fetching a URL. BM25 retrieval with a built-in test box. |
| **My agents → Brain, tools & knowledge** | Per agent: default **LLM**; the **algorithm per auction type** (`baseline`, `heuristic`, or `llm`, each with an optional LLM override); the exact **MCP tools** it may call; the **knowledge bases** it may search. Sellers pick a listing advisor (`rules` or `llm`). |
| **My agents → Rules** | Hard limits enforced by the guardrail: budget ceiling, reserve floor, escalation threshold, allowed auction types, watch spec, auto-relist. |

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
English (with anti-sniping), Dutch, first-price sealed, second-price sealed (Vickrey). Reverse, multi-unit and
combinatorial are reserved for a later milestone (they need a winner-determination solver).

## Configuration

| Variable | Purpose |
|---|---|
| `KENYABIDDER_DATA` | State snapshot path (default `data/state.json`). Contains LLM/MCP credentials → written `0600`. |
| `KENYABIDDER_MCP_PORT` / `KENYABIDDER_MCP_KEY` | Serve KenyaBidder's read-only FastMCP endpoint on `127.0.0.1:<port>/mcp`; bearer key (generated if unset). |
| `KENYABIDDER_SECRET` | Session-cookie secret (generated and persisted if unset). |
| `KENYABIDDER_ALLOW_STDIO=1` | Allow admins to register stdio MCP servers (they execute local commands). |
| `KENYABIDDER_ALLOW_PRIVATE_FETCH=1` | Allow knowledge-base URL ingestion from private addresses (off by default — SSRF guard). |
| `WHATSAPP_APP_SECRET` | **Required** to accept WhatsApp webhooks (verifies `X-Hub-Signature-256`). `KENYABIDDER_INSECURE_WEBHOOK=1` skips it for local dev only. |
| `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` | Cloud API verification handshake and outbound messages. |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | Default key sources for LLMs registered without an explicit key. |

## Assumptions & known limits

* Amounts are integer **KES**; the spec does not define a currency.
* State is an in-memory store with atomic JSON snapshots (a stand-in for Postgres + Redis, spec §15). Single process.
* The Execution Engine calls the auction engine in-process. The internal FastMCP server (`build_server(app, internal=True)`)
  exposes the same operations for a future out-of-process harness.
* Fall-through fault assumption: a `FELL_THROUGH`/`NO_RESPONSE` report counts against the reporter's counterparty.
* Market Intelligence has no forecasting yet; new-tier value ceiling is a single configurable number, not per category.
* Sign-in is name + password with per-name lockout; put the app behind TLS before exposing it. Run the KB URL fetcher
  and stdio MCP support only on hosts you trust the admins of.
