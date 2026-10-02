"""Reputation tiers from finalized matches (spec §11.1). Fall-through rate is recency-weighted."""
RECENT, DECAY = 20, 0.9
FINAL = {"COMPLETED", "FELL_THROUGH", "NO_RESPONSE"}


def recompute_reputation(store, agent_id: str) -> dict | None:
    agent = store.agents.get(agent_id)
    if not agent:
        return None
    final = sorted((m for m in store.matches.values()
                    if agent_id in (m["seller_agent_id"], m["buyer_agent_id"]) and m["status"] in FINAL),
                   key=lambda m: -m["updated_at"])
    completed = sum(1 for m in final if m["status"] == "COMPLETED")
    w_sum = w_fail = 0.0
    for i, m in enumerate(final[:RECENT]):
        w = DECAY ** i
        w_sum += w
        if agent_id in m["fault_agent_ids"]:
            w_fail += w
    fail_total = sum(1 for m in final if agent_id in m["fault_agent_ids"])
    rate = w_fail / w_sum if w_sum else 0.0
    times = []
    for m in final:
        t = m["confirmations"]["seller_at" if m["seller_agent_id"] == agent_id else "buyer_at"]
        if t:
            times.append((t - m["created_at"]) / 1000)
    avg = round(sum(times) / len(times)) if times else 0
    tier = "NEW"
    if completed >= 15 and rate < 0.1 and avg <= 3600:
        tier = "TRUSTED"
    elif completed >= 3 and rate < 0.2:
        tier = "ESTABLISHED"
    agent["reputation"] = {"completed_matches": completed, "fell_through_count": fail_total,
                           "avg_response_time_seconds": avg, "tier": tier,
                           "score": max(0, round((1 - rate) * 100 - max(0, 3 - completed) * 5))}
    return agent["reputation"]
