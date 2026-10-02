import uuid


class AuditLog:
    """Append-only audit log (spec §7.3, §10 AuditEntry)."""

    def __init__(self, store, clock):
        self.store, self.clock = store, clock

    def record(self, *, agent_id, auction_id=None, proposed_action, guardrail_decision, rejection_reason=None,
               executed_action=None, execution_result=None) -> dict:
        e = {"entry_id": str(uuid.uuid4()), "agent_id": agent_id, "auction_id": auction_id,
             "proposed_action": proposed_action, "guardrail_decision": guardrail_decision,
             "rejection_reason": rejection_reason, "executed_action": executed_action,
             "execution_result": execution_result, "timestamp": self.clock.now()}
        self.store.audit.append(e)
        return e

    def for_agent(self, agent_id: str, limit: int = 100) -> list[dict]:
        out = []
        for e in reversed(self.store.audit):
            if e["agent_id"] == agent_id:
                out.append(e)
                if len(out) >= limit:
                    break
        return out
