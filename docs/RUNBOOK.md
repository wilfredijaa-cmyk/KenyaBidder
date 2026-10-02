# Runbook

## Deploy
```bash
cp deploy/.env.example deploy/.env      # fill in every value (see SECURITY.md checklist)
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
curl -fsS https://$KENYABIDDER_DOMAIN/readyz          # {"ok": true, "checks": {...}}
```
The first account registered becomes the administrator — register it immediately, enable 2FA, then announce the site.

## Health, metrics, alerts
* `/healthz` liveness, `/readyz` readiness (database, tick loop, state flush, writer lease). Alert if `/readyz` is not 200 for 2 minutes.
* `/metrics` (Prometheus; bearer `KENYABIDDER_METRICS_TOKEN`, blocked at the proxy). Useful alerts: `kenyabidder_ready == 0`; `kenyabidder_tick_age_seconds > 5`;
  `kenyabidder_state_flush_age_seconds > 30`; `kenyabidder_backup_age_seconds > 90000`; `kenyabidder_backup_failing == 1`; `kenyabidder_orders{status="AWAITING_REVIEW"}` growing;
  `kenyabidder_disputes_open` and `kenyabidder_verifications_pending` older than your SLA; `kenyabidder_messages_failed_total` rising.
* Logs are JSON when `KENYABIDDER_LOG_JSON=1`; secrets/phones are redacted.

## Backups and restore
* DuckDB: automatic verified backups (Admin → Health & backups) — copy them off the host. PostgreSQL: use `pg_dump`/managed snapshots (`pg_dump -Fc`), and keep `KENYABIDDER_ENCRYPTION_KEY` **separately** (without it encrypted keys in a restored DB are unreadable).
* Restore (DuckDB, app stopped): `python -m kenyabidder restore data/backups/<file>`. Restore-test monthly.

## Incidents
| Situation | Action |
|---|---|
| Fraud wave / bad bug in bidding | Admin → Health & backups → **Pause the whole marketplace** (instant). Investigate via Audit log; resume when safe. |
| Compromised user | Admin → Users → Suspend (pauses agents, cancels plans, ends sessions). Reset password; check ledger. |
| Compromised admin | Suspend + reset; rotate `KENYABIDDER_SECRET` (signs everyone out), LLM/MCP keys, `WHATSAPP_APP_SECRET`; review Audit log. |
| Leaked encryption key | Generate a new key, set it, re-save LLM/MCP keys and re-enrol 2FA for admins (old ciphertext becomes unreadable by design). |
| M-Pesa callbacks stopped | Orders reconcile automatically every 30s via STK query; check `MPESA_CALLBACK_BASE_URL`/TLS. Manual orders: Admin → Billing → review queue. |
| "LOST THE WRITER LEASE" in logs | A second instance is running against the same database. Stop the extra one; restart this one. After a crash, `KENYABIDDER_FORCE_LEASE=1` once. |
| Ledger integrity warning | Do not sell tokens. Admin → Health shows the mismatch; restore from the last good backup and replay from the orders table (paid orders are authoritative). |
| SMS budget exhausted | Check for SMS-pumping (messages log); raise the budget only after confirming it is genuine. |

## Account recovery (run on the host, app may be running)
* Admin lost 2FA: `python -m kenyabidder reset-2fa <name>` then have them re-enrol. Forgotten password: `reset-password <name>`.
* No administrator left: `python -m kenyabidder create-admin <name>`.
* PostgreSQL restarts/failovers are survived: the app reconnects on the next query (reads retry once; writes are not blindly repeated).

## Upgrades
Migrations are versioned and run at startup (`schema_version`). Take a backup first; roll back by restoring it. One active app process at a time (lease).

## Capacity notes
One process serves the whole engine (in-memory working set, ~1 s flush). Size RAM for open auctions + active agents; scale vertically first. Horizontal scale needs auction sharding and a shared rate-limit store (see Next steps in the README).
