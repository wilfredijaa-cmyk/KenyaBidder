const pctile = (arr, p) => {
  if (!arr.length) return null;
  const s = [...arr].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.floor(p * s.length))];
};

/** Platform KPIs (spec §5), derived from audit log, auctions and matches. */
export function computeKpis(store) {
  const audit = store.audit;
  const decisions = audit.length;
  const blocked = audit.filter((e) => e.guardrail_decision !== 'APPROVED').length;
  const latencies = audit.filter((e) => e.execution_result?.latencyMs != null).map((e) => e.execution_result.latencyMs);
  const auctions = [...store.auctions.values()];
  const settled = auctions.filter((a) => a.status === 'SETTLED');
  const sold = settled.filter((a) => a.result?.outcome === 'SOLD');
  const matches = [...store.matches.values()];
  const revealed = matches.filter((m) => m.contact_reveal);
  const finals = matches.filter((m) => ['COMPLETED', 'FELL_THROUGH', 'NO_RESPONSE'].includes(m.status));
  const ttm = revealed.map((m) => Math.max(m.confirmations.sellerAt, m.confirmations.buyerAt) - m.created_at);
  const executed = audit.filter((e) => e.guardrail_decision === 'APPROVED');
  const ratio = (n, d) => (d ? Math.round((n / d) * 1000) / 1000 : null);

  return {
    agentPerformance: {
      auctionsSettled: settled.length,
      sellThroughRate: ratio(sold.length, settled.length),
      bidLatencyMs: { p50: pctile(latencies, 0.5), p95: pctile(latencies, 0.95), max: latencies.length ? Math.max(...latencies) : null, samples: latencies.length },
    },
    systemReliability: {
      guardrailInterceptRate: ratio(blocked, decisions),
      auditEntries: decisions,
      executionSuccessRate: ratio(executed.filter((e) => e.execution_result?.ok).length, executed.length),
    },
    matching: {
      matches: matches.length,
      matchConfirmationRate: ratio(revealed.length, matches.length),
      timeToMatchMs: { p50: pctile(ttm, 0.5) },
      fallThroughRate: ratio(finals.filter((m) => m.status !== 'COMPLETED').length, finals.length),
    },
  };
}
