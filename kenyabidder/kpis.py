"""Platform KPIs (spec §5), derived from the audit log, auctions and matches."""
from __future__ import annotations


def _pctile(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(p * len(s)))]


def _ratio(n, d):
    return round(n / d, 3) if d else None


def compute_kpis(store) -> dict:
    audit = store.audit
    blocked = sum(1 for e in audit if e["guardrail_decision"] != "APPROVED")
    lat = [e["execution_result"]["latency_ms"] for e in audit if (e["execution_result"] or {}).get("latency_ms") is not None]
    settled = [a for a in store.auctions.values() if a["status"] == "SETTLED"]
    sold = [a for a in settled if a["result"]["outcome"] == "SOLD"]
    matches = list(store.matches.values())
    revealed = [m for m in matches if m["contact_reveal"]]
    finals = [m for m in matches if m["status"] in ("COMPLETED", "FELL_THROUGH", "NO_RESPONSE")]
    executed = [e for e in audit if e["guardrail_decision"] == "APPROVED"]
    ttm = [max(m["confirmations"]["seller_at"], m["confirmations"]["buyer_at"]) - m["created_at"] for m in revealed]
    return {
        "agent_performance": {
            "auctions_settled": len(settled), "sell_through_rate": _ratio(len(sold), len(settled)),
            "bid_latency_ms": {"p50": _pctile(lat, 0.5), "p95": _pctile(lat, 0.95), "max": max(lat) if lat else None, "samples": len(lat)},
        },
        "system_reliability": {
            "guardrail_intercept_rate": _ratio(blocked, len(audit)), "audit_entries": len(audit),
            "execution_success_rate": _ratio(sum(1 for e in executed if (e["execution_result"] or {}).get("ok")), len(executed)),
        },
        "matching": {
            "matches": len(matches), "match_confirmation_rate": _ratio(len(revealed), len(matches)),
            "time_to_match_ms": {"p50": _pctile(ttm, 0.5)},
            "fall_through_rate": _ratio(sum(1 for m in finals if m["status"] != "COMPLETED"), len(finals)),
        },
    }
