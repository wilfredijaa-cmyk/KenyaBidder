"""Match / Introduction service (spec §8.2, §11.2). No funds ever move here."""
from __future__ import annotations

import uuid

from .errors import bad, forbidden, not_found
from .reputation import recompute_reputation

OUTCOMES = ["COMPLETED", "FELL_THROUGH", "NO_RESPONSE"]
PRE_REVEAL = ("PROPOSED", "SELLER_CONFIRMED", "BUYER_CONFIRMED")


class MatchService:
    def __init__(self, store, clock, engine, notify=None, match_ttl_ms: int = 24 * 3600_000, report_grace_ms: int = 72 * 3600_000):
        self.store, self.clock = store, clock
        self.notify = notify or (lambda *a, **k: None)
        self.match_ttl_ms = match_ttl_ms
        self.report_grace_ms = report_grace_ms  # how long the other side has to answer a first report
        self.dispute_blocks_finalize = None  # fn(match) -> bool: an open dispute keeps automatic outcome logic from deciding the match
        self.confirm_gate = None  # composition root: fn(user) -> raises if the user may not exchange contact details yet
        # create_match is triggered by the deterministic layer when an auction closes with a winner.
        engine.events.on("auction.settled", self._on_settled)

    def _on_settled(self, auction_id, result, **_):
        a = self.store.auctions.get(auction_id)
        if result["outcome"] == "SOLD" and not (a or {}).get("demo"):  # demo listings never create real matches / contact reveals
            self.create_match(auction_id)

    def create_match(self, auction_id: str) -> dict:
        a = self.store.auctions.get(auction_id)
        if not a or (a.get("result") or {}).get("outcome") != "SOLD":
            raise bad("NOT_SOLD", "auction has no winner")
        for m in self.store.matches.values():
            if m["auction_id"] == auction_id:
                return m  # idempotent
        now = self.clock.now()
        # roles follow the money: in an RFQ the poster is the BUYER and the lowest-quoting supplier is the SELLER
        reverse = a.get("direction") == "REVERSE"
        seller_id, buyer_id = (a["result"]["winner_agent_id"], a["poster_agent_id"]) if reverse else (a["seller_agent_id"], a["result"]["winner_agent_id"])
        m = {"match_id": str(uuid.uuid4()), "auction_id": auction_id, "seller_agent_id": seller_id,
             "buyer_agent_id": buyer_id, "direction": "REVERSE" if reverse else "FORWARD",
             "agreed_terms": {"price": a["result"]["price"], "quantity": a["product_spec"]["quantity"],
                              "title": a["product_spec"]["title"], "delivery_terms": None,
                              **({"max_price": a["max_price"], "saved": a["max_price"] - a["result"]["price"]} if reverse else {})},
             "status": "PROPOSED", "confirmations": {"seller_at": None, "buyer_at": None},
             "contact_reveal": None, "outcome_reports": [], "fault_agent_ids": [], "created_at": now, "updated_at": now}
        self.store.matches[m["match_id"]] = m
        text = f"Match created for \"{a['product_spec']['title']}\" at {a['result']['price']} KES. Confirm to exchange contact details"
        self.notify(m["seller_agent_id"], "match", f"{text} — you are the seller" + (" (your quote won the RFQ)." if reverse else "."), match_id=m["match_id"])
        self.notify(m["buyer_agent_id"], "match", f"{text} — " + ("the lowest supplier quote on your RFQ." if reverse else "you won this auction."), match_id=m["match_id"])
        return m

    def _side(self, m: dict, agent_id: str) -> str:
        if agent_id == m["seller_agent_id"]:
            return "seller"
        if agent_id == m["buyer_agent_id"]:
            return "buyer"
        raise forbidden("NOT_A_PARTY", "agent is not a party to this match")

    def get_match(self, match_id: str, agent_id: str | None = None) -> dict:
        m = self.store.matches.get(match_id)
        if not m:
            raise not_found("MATCH_NOT_FOUND", "match not found")
        if agent_id:
            self._side(m, agent_id)
        return m

    def matches_for(self, agent_id: str) -> list[dict]:
        return sorted((m for m in self.store.matches.values() if agent_id in (m["seller_agent_id"], m["buyer_agent_id"])),
                      key=lambda m: -m["created_at"])

    def confirm_match(self, match_id: str, agent_id: str) -> dict:
        m = self.get_match(match_id, agent_id)
        if m["status"] not in PRE_REVEAL:
            raise bad("INVALID_STATE", f"match is {m['status']}")
        side = self._side(m, agent_id)
        if self.confirm_gate:
            self.confirm_gate(self.store.users[self.store.agents[agent_id]["principal_user_id"]])
        now = self.clock.now()
        m["confirmations"][f"{side}_at"] = m["confirmations"][f"{side}_at"] or now
        m["updated_at"] = now
        c = m["confirmations"]
        if c["seller_at"] and c["buyer_at"]:
            self.reveal_contact(match_id)
        else:
            m["status"] = "SELLER_CONFIRMED" if side == "seller" else "BUYER_CONFIRMED"
            other = m["buyer_agent_id"] if side == "seller" else m["seller_agent_id"]
            self.notify(other, "match", f"The {side} confirmed the match. Confirm to reveal contact details.", match_id=match_id)
        return m

    def reveal_contact(self, match_id: str) -> dict:
        """Fires once both sides confirmed (default: immediate, §11.2). Logged for bypass analytics."""
        m = self.get_match(match_id)
        c = m["confirmations"]
        if not (c["seller_at"] and c["buyer_at"]):
            raise bad("NOT_CONFIRMED", "both parties must confirm first")
        if m["status"] == "CONTACT_REVEALED" or m["contact_reveal"]:
            return m

        def contact(agent_id):
            u = self.store.users[self.store.agents[agent_id]["principal_user_id"]]
            return {"name": u["name"], "phone": u["phone"], "email": u["email"],
                    "verified_business": (u.get("verified_business") or {}).get("name")}

        m["contact_reveal"] = {"seller_contact": contact(m["seller_agent_id"]), "buyer_contact": contact(m["buyer_agent_id"])}
        m["status"] = "CONTACT_REVEALED"
        m["updated_at"] = m["revealed_at"] = self.clock.now()
        self.store.reveal_log.append({"match_id": match_id, "at": m["updated_at"],
                                      "seller_agent_id": m["seller_agent_id"], "buyer_agent_id": m["buyer_agent_id"]})
        for aid in (m["seller_agent_id"], m["buyer_agent_id"]):
            self.notify(aid, "match", "Both sides confirmed — contact details are now visible. Settle delivery and payment directly with the counterparty.", match_id=match_id)
        return m

    def report_outcome(self, match_id: str, agent_id: str, outcome: str, notes: str = "") -> dict:
        m = self.get_match(match_id, agent_id)
        if outcome not in OUTCOMES:
            raise bad("INVALID_OUTCOME", f"outcome must be one of {', '.join(OUTCOMES)}")
        if m["status"] != "CONTACT_REVEALED":
            raise bad("INVALID_STATE", f"outcomes can only be reported once contact is revealed (match is {m['status']})")
        now = self.clock.now()
        rep = next((r for r in m["outcome_reports"] if r["agent_id"] == agent_id), None)
        if rep:
            rep.update(outcome=outcome, notes=notes, reported_at=now)
        else:
            m["outcome_reports"].append({"agent_id": agent_id, "outcome": outcome, "notes": notes, "reported_at": now})
        m["first_report_at"] = m.get("first_report_at") or now
        m["updated_at"] = now
        if not self._finalize(m):
            other = m["buyer_agent_id"] if agent_id == m["seller_agent_id"] else m["seller_agent_id"]
            days = self.report_grace_ms // (24 * 3600_000)
            self.notify(other, "match", f"The other party reported \"{outcome.replace('_', ' ').lower()}\" for this deal. Please report your side within {days} day(s) — "
                        "nothing affects your reputation until you have had the chance to answer.", match_id=match_id)
        return m

    def _finalize(self, m: dict, *, force: bool = False) -> bool:
        """Decide the outcome — but only when BOTH sides have reported (or the other side stayed silent past the grace period).

        A single unilateral report never damages anyone's reputation. Contradictory reports are DISPUTED: the platform does not
        adjudicate (spec §2), so neither side is penalised or credited. Where both blame each other, both are at fault."""
        if self.dispute_blocks_finalize and self.dispute_blocks_finalize(m):
            return False  # an administrator will rule; nothing is decided automatically meanwhile
        reports = m["outcome_reports"]
        if len(reports) < 2 and not force:
            return False

        def other(i):
            return m["buyer_agent_id"] if i == m["seller_agent_id"] else m["seller_agent_id"]

        bad_reports = [r for r in reports if r["outcome"] in ("FELL_THROUGH", "NO_RESPONSE")]
        kind = "FELL_THROUGH" if any(r["outcome"] == "FELL_THROUGH" for r in bad_reports) else "NO_RESPONSE"
        m["fault_agent_ids"] = []
        if len(reports) == 2:
            if not bad_reports:
                m["status"] = "COMPLETED"
            elif len(bad_reports) == 1:
                m["status"] = "DISPUTED"  # one says it fell through, the other says it completed: nobody is penalised
            else:
                m["status"], m["fault_agent_ids"] = kind, sorted({other(r["agent_id"]) for r in bad_reports})
        else:  # one report, the counterparty stayed silent through the grace period
            r = reports[0]
            if r["outcome"] == "COMPLETED":
                m["status"] = "COMPLETED"
            else:
                m["status"], m["fault_agent_ids"] = kind, [other(r["agent_id"])]
        m["updated_at"] = self.clock.now()
        recompute_reputation(self.store, m["seller_agent_id"])
        recompute_reputation(self.store, m["buyer_agent_id"])
        return True

    def expire_stale(self) -> None:
        """Unconfirmed matches lapse into NO_RESPONSE, attributed to whoever did not confirm."""
        now = self.clock.now()
        for m in self.store.matches.values():
            if m["status"] not in PRE_REVEAL or now - m["created_at"] < self.match_ttl_ms:
                continue
            m["status"] = "NO_RESPONSE"
            m["fault_agent_ids"] = [aid for aid, k in ((m["seller_agent_id"], "seller_at"), (m["buyer_agent_id"], "buyer_at"))
                                    if not m["confirmations"][k]]
            m["updated_at"] = now
            recompute_reputation(self.store, m["seller_agent_id"])
            recompute_reputation(self.store, m["buyer_agent_id"])
        for m in self.store.matches.values():  # a lone report stands once the other side has been silent for the whole grace period
            if m["status"] == "CONTACT_REVEALED" and len(m["outcome_reports"]) == 1 and now - m.get("first_report_at", now) >= self.report_grace_ms:
                self._finalize(m, force=True)
