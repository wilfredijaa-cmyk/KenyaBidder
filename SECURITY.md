# Security

## Reporting
Email the maintainers privately (do not open a public issue) with steps to reproduce. We aim to acknowledge within 2 working days.

## Threat model (summary)
Internet-facing web app for mutually distrusting businesses. Assets, in order: **(1) token balances and payment records**, **(2) user contact details** (revealed only
after both parties confirm a match), **(3) API keys** (LLM, M-Pesa, SMS, WhatsApp), **(4) account integrity** (nobody bids as someone else), **(5) availability** during auctions.
The platform never holds buyers' or sellers' money. LLM output, listing text, tool results and knowledge-base text are **untrusted input**.

| Threat | Controls |
|---|---|
| Account takeover (credential stuffing, guessing) | scrypt hashes; per-account lockout (counted *before* the async check, so concurrent guesses cannot dodge it); per-IP failure throttle; password policy (10+ chars, common-password and personal-data checks); optional/required TOTP 2FA for sign-in (administrators required in production) with single-use codes and recovery codes; sessions: 12h absolute, 2h idle (30 min for admins), rotated on login, ended by password change/reset/2FA enrolment/"sign out everywhere"; SMS/email codes hashed, expiring, attempt-limited |
| Session theft / fixation / CSRF | HttpOnly, SameSite=Lax cookie (Secure when HTTPS), signed with `KENYABIDDER_SECRET`; state changes happen over the authenticated websocket, not cross-site form posts; CSP `frame-ancestors 'none'`, `X-Frame-Options: DENY` |
| XSS | all user text rendered through escaped NiceGUI elements; no user-controlled `ui.html`/markdown; CSP (`object-src 'none'`, `base-uri 'self'`, `form-action 'self'`); `X-Content-Type-Options: nosniff` |
| Injection | every SQL value is a bound parameter (the few f-strings interpolate constant column lists only; bandit-reviewed); no shell, `eval` or pickle on user data; webhook JSON parsed with size caps |
| Bid manipulation / fraud | LLM only proposes; deterministic guardrail + execution engine; hard ceilings/floors; idempotent bids; shill-bid check (same account, phone *or* email); verified-only listings; value-gated verification; NEW-tier caps; circuit breakers; per-agent rate limits; global emergency stop; user "pause all agents" |
| Payment fraud | M-Pesa callback only *triggers* an authenticated status query; one M-Pesa code ↔ one purchase (DB-enforced); ambiguous STK timeouts never fail an order; reconciliation loop; ledger append-only with integrity verification; CHECK(balance >= 0) |
| Webhook forgery/replay | WhatsApp HMAC signature required (dev bypass is refused in production); message-id replay protection; secret-path M-Pesa callback + access log disabled + log redaction; body-size caps |
| Prompt injection | untrusted-content delimiters; tools assigned per agent and read-only; state-changing tools never exposed to LLMs; outputs clamped by guardrails (an injected "bid 10x" is refused) |
| SSRF | knowledge-base URL fetch blocks private ranges; LLM/MCP endpoint URLs block cloud-metadata/link-local; no credentials in URLs |
| Secrets exposure | API keys, MCP tokens, 2FA seeds and signing secrets encrypted at rest (Fernet) with a key kept outside the database; backups therefore don't carry them in the clear; log redaction of keys, bearer tokens, phone numbers and the M-Pesa callback path; `0600` data files |
| DoS | per-IP request budget, per-IP websocket cap, request/body-size limits, listing and agent caps, token metering and hourly decision limits, bounded in-memory structures (pruned) |
| Insider / admin abuse | admin actions audit-logged; 2FA required; short admin idle timeout; no admin path reads passwords; deletion/ledger changes recorded |
| Privacy | data export, account deletion with anonymisation, minimal contact reveal, SMS/email bodies never stored |

## Operator checklist
1. Run behind TLS (Caddy config in `deploy/`), set `KENYABIDDER_ENV=production`, `KENYABIDDER_PUBLIC_URL=https://…`; the app refuses to start on insecure combinations.
2. Set `KENYABIDDER_BOOTSTRAP_TOKEN` (the setup code needed to create the first administrator on a fresh install), `KENYABIDDER_SECRET`, `KENYABIDDER_ENCRYPTION_KEY`, `KENYABIDDER_METRICS_TOKEN`, `WHATSAPP_APP_SECRET`, `KENYABIDDER_TRUSTED_PROXIES` (so per-IP limits see real clients).
3. Never set `KENYABIDDER_DEV_PAYMENTS`, `KENYABIDDER_INSECURE_WEBHOOK`, `KENYABIDDER_PASSWORD_POLICY=basic`, or `KENYABIDDER_ALLOW_STDIO` unless you accept the consequences.
4. Enrol every administrator in 2FA before the first public day; keep recovery codes offline.
5. Copy backups off the host; restore-test them; keep the encryption key separate from backups.
6. Put the host behind a WAF/CDN for volumetric DDoS; the in-app limits are a last line, not the first.
7. Subscribe to dependency advisories (`pip-audit` runs in CI) and redeploy promptly.

## Sessions, recovery and admin-configured endpoints
* Sessions have idle and absolute timeouts (shorter for admins); suspending a user, changing a password or 2FA bumps a session version that ends open sessions at the next click. Login does not reuse a pre-login session: its storage is cleared.
* Lost 2FA / password / no admin left: on the host, `python -m kenyabidder reset-2fa <name>`, `reset-password <name>`, `create-admin <name>`.
* LLM/MCP base URLs may not point at cloud-metadata/link-local addresses (checked on save and at connect). API keys may be referenced by environment-variable *name* only from a provider-key allowlist; platform secrets can never be referenced (extend with `KENYABIDDER_ALLOWED_KEY_ENVS`).
* Listings under moderator review or taken down are visible only to the poster and administrators.

## Known limitations
* Rate limiting and lockout counters are in-process (single active engine process by design); a shared store (Redis) is needed before running multiple app replicas.
* CSP must allow `'unsafe-inline'`/`'unsafe-eval'` because the UI framework (Vue/Quasar) needs them; the other CSP directives still apply.
* No device-fingerprinting or ML risk scoring; velocity/identity rules only.
