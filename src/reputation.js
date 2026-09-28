const RECENT = 20;
const DECAY = 0.9;

/**
 * Recompute an agent's reputation from finalized matches (spec §11.1).
 * Fall-through rate is recency-weighted so it reflects current behaviour.
 */
export function recomputeReputation(store, agentId) {
  const agent = store.agents.get(agentId);
  if (!agent) return null;
  const final = [...store.matches.values()]
    .filter((m) => (m.seller_agent_id === agentId || m.buyer_agent_id === agentId) && ['COMPLETED', 'FELL_THROUGH', 'NO_RESPONSE'].includes(m.status))
    .sort((a, b) => b.updated_at - a.updated_at);

  const completed = final.filter((m) => m.status === 'COMPLETED').length;
  const recent = final.slice(0, RECENT);
  let wSum = 0;
  let wFail = 0;
  let failTotal = 0;
  recent.forEach((m, i) => {
    const w = DECAY ** i;
    wSum += w;
    if (m.fault_agent_ids?.includes(agentId)) wFail += w;
  });
  for (const m of final) if (m.fault_agent_ids?.includes(agentId)) failTotal++;
  const rate = wSum ? wFail / wSum : 0;

  const responseTimes = [];
  for (const m of final) {
    const t = m.seller_agent_id === agentId ? m.confirmations?.sellerAt : m.confirmations?.buyerAt;
    if (t) responseTimes.push((t - m.created_at) / 1000);
  }
  const avgResponse = responseTimes.length ? Math.round(responseTimes.reduce((a, b) => a + b, 0) / responseTimes.length) : 0;

  let tier = 'NEW';
  if (completed >= 15 && rate < 0.1 && avgResponse <= 3600) tier = 'TRUSTED';
  else if (completed >= 3 && rate < 0.2) tier = 'ESTABLISHED';

  agent.reputation = {
    completed_matches: completed,
    fell_through_count: failTotal,
    avg_response_time_seconds: avgResponse,
    tier,
    score: Math.max(0, Math.round((1 - rate) * 100 - Math.max(0, 3 - completed) * 5)),
  };
  return agent.reputation;
}
