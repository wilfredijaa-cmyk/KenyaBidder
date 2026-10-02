"""Health, readiness and Prometheus metrics (no external dependency: the text exposition format is trivial)."""
from __future__ import annotations

import json
import logging
import time
from collections import Counter

TICK_STALE_S = 5
FLUSH_STALE_S = 30


def readiness(rt) -> tuple[bool, dict]:
    """Ready = database answers, the tick loop is alive, and (in durable mode) state is being flushed."""
    now = time.time()
    checks = {"database": rt.db.healthy()}
    if rt.started_at:  # only judge liveness of loops once they have been started
        checks["tick_loop"] = now - rt.last_tick_at < TICK_STALE_S
        if rt.persistence:
            checks["writer_lease"] = not rt.lost_lease
            checks["state_flush"] = now - max(rt.last_flush_at, rt.started_at) < FLUSH_STALE_S and rt.flush_errors_streak < 3
    return all(checks.values()), checks


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render(rt) -> str:
    a, s = rt.app, rt.app.store
    now = time.time()
    lines: list[str] = []

    def metric(name: str, help_: str, typ: str, samples: list[tuple[dict, float]]) -> None:
        lines.append(f"# HELP kenyabidder_{name} {help_}")
        lines.append(f"# TYPE kenyabidder_{name} {typ}")
        for labels, v in samples:
            lab = "{" + ",".join(f'{k}="{_esc(x)}"' for k, x in labels.items()) + "}" if labels else ""
            lines.append(f"kenyabidder_{name}{lab} {v}")

    ready, checks = readiness(rt)
    metric("up", "1 while the process runs", "gauge", [({}, 1)])
    metric("ready", "1 when every readiness check passes", "gauge", [({}, int(ready))])
    metric("check_ok", "Individual readiness checks", "gauge", [({"check": k}, int(v)) for k, v in checks.items()])
    metric("users", "Registered (non-deleted) users", "gauge", [({}, sum(1 for u in s.users.values() if not u.get("deleted_at")))])
    ag = Counter((x["agent_type"], x["status"]) for x in s.agents.values())
    metric("agents", "Agents by type and status", "gauge", [({"type": t, "status": st}, n) for (t, st), n in sorted(ag.items())])
    metric("auctions", "Auctions by status", "gauge", [({"status": k}, n) for k, n in sorted(Counter(x["status"] for x in s.auctions.values()).items())])
    metric("bids_open", "Bids placed on currently open auctions", "gauge", [({}, sum(len(x["bids"]) for x in s.auctions.values() if x["status"] in ("ACTIVE", "EXTENDING")))])
    metric("matches", "Matches by status", "gauge", [({"status": k}, n) for k, n in sorted(Counter(m["status"] for m in s.matches.values()).items())])
    metric("disputes_open", "Disputes waiting for a ruling", "gauge", [({}, len(a.trust.open_disputes()))])
    metric("verifications_pending", "Business verification applications waiting", "gauge", [({}, len(a.trust.pending_applications()))])
    metric("reports_open", "Listing reports waiting for a moderator", "gauge", [({}, len(a.moderation.open_reports()))])
    sv = a.insights.platform_savings()
    metric("rfq_saved_kes", "Total KES saved against buyers' maximum prices on awarded RFQs", "gauge", [({}, sv["total_saved_kes"])])
    metric("triggers_live", "Live execution triggers", "gauge", [({}, sum(1 for t in s.triggers.values() if t["status"] in ("ACTIVE", "AWAITING_APPROVAL")))])
    metric("agents_blocked", "Agents waiting for tokens or an allowance window", "gauge", [({}, len(a.orchestrator.blocked))])
    metric("orchestrator_tasks", "In-flight strategy tasks", "gauge", [({}, len(a.orchestrator.pending))])
    orders = a.wallet.database.query("SELECT status, COUNT(*) AS n FROM orders GROUP BY status")
    metric("orders", "Payment orders by status", "gauge", [({"status": r["status"]}, r["n"]) for r in orders])
    liability = a.wallet.outstanding_liability()
    metric("token_liability", "Sold-but-unspent tokens per LLM (what customers still hold)", "gauge",
           [({"llm": s.llms.get(k, {}).get("name", k)}, v) for k, v in sorted(liability.items())])
    metric("sms_sent_today", "SMS sent today against the platform budget", "gauge", [({}, (s.settings.get("sms_day") or {}).get("n", 0))])
    metric("messages_failed_total", "Failed SMS/email deliveries in the retained log", "gauge", [({}, sum(1 for m in s.message_log if not m["ok"]))])
    metric("tick_age_seconds", "Seconds since the auction tick loop last ran", "gauge", [({}, round(now - rt.last_tick_at, 3) if rt.last_tick_at else -1)])
    if rt.persistence:
        metric("state_flush_age_seconds", "Seconds since state was last flushed to the database", "gauge", [({}, round(now - rt.last_flush_at, 3) if rt.last_flush_at else -1)])
        metric("state_flush_errors_total", "State flush failures since start", "counter", [({}, rt.flush_errors)])
    if rt.backups:
        metric("backup_age_seconds", "Seconds since the last successful backup", "gauge", [({}, round(a.clock.now() / 1000 - rt.backups.last_at / 1000, 1) if rt.backups.last_at else -1)])
        metric("backup_failing", "1 if the last backup attempt failed", "gauge", [({}, int(bool(rt.backups.last_error)))])
    return "\n".join(lines) + "\n"


class JsonFormatter(logging.Formatter):
    """One JSON object per line — what log shippers (Loki, CloudWatch, Datadog) want."""

    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def configure_logging(json_logs: bool | None = None) -> None:
    import os
    if json_logs is None:
        json_logs = os.environ.get("KENYABIDDER_LOG_JSON") == "1"
    h = logging.StreamHandler()
    from .security import redact
    fmt = JsonFormatter() if json_logs else logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    plain = fmt.format
    fmt.format = lambda record: redact(plain(record))  # redact the FINAL text (tracebacks and exception messages included)
    h.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(logging.INFO)
