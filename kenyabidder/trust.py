"""Business verification (badges) and the dispute workflow.

The platform never holds money, so trust is the product. Two mechanisms sit on top of match reputation:

* **Verified business** — a user submits a KRA PIN / business registration / ID number; an administrator checks it out-of-band
  (iTax, eCitizen/BRS) and approves or rejects. Approved users carry a badge everywhere; listings may be restricted to verified
  businesses, and the platform can require verification for high-value bids.
* **Disputes** — after contact is exchanged either party can open a dispute; the other side answers; an administrator rules and the
  ruling (not a unilateral report) is what changes reputation.
"""
from __future__ import annotations

import re
import uuid

from .errors import AppError, bad, conflict, forbidden, not_found
from .reputation import recompute_reputation

KINDS = {"KRA_PIN": "KRA PIN", "BUSINESS_REG": "Business registration number", "ID_NUMBER": "National ID number"}
_PATTERNS = {"KRA_PIN": r"[AP]\d{9}[A-Z]", "BUSINESS_REG": r"[A-Z0-9/.\-]{4,24}", "ID_NUMBER": r"\d{7,9}"}

CATEGORIES = {"NOT_DELIVERED": "Goods or service not delivered", "NOT_PAID": "Payment not received", "NOT_AS_DESCRIBED": "Not as described",
              "PRICE_CHANGED": "Price or terms changed after the match", "OTHER": "Something else"}
RULINGS = {"COMPLETED": "The deal was completed properly", "SELLER_AT_FAULT": "The seller is at fault", "BUYER_AT_FAULT": "The buyer is at fault",
           "BOTH_AT_FAULT": "Both parties share the fault", "NO_FAULT": "No fault found — reputation unaffected"}
ELIGIBLE = ("CONTACT_REVEALED", "COMPLETED", "FELL_THROUGH", "DISPUTED")
DISPUTE_WINDOW_MS = 30 * 24 * 3600_000
RESPONSE_WINDOW_MS = 7 * 24 * 3600_000
MAX_OPEN_PER_AGENT = 5
MAX_STATEMENTS_PER_PARTY = 3


class TrustService:
    def __init__(self, store, clock, matches, notify=None):
        self.store, self.clock, self.matches = store, clock, matches
        self.notify = notify or (lambda *a, **k: None)
        matches.dispute_blocks_finalize = self._open_for

    # ------------------------------------------------------------------ business verification

    def is_verified(self, user_id: str | None) -> bool:
        u = self.store.users.get(user_id or "")
        return bool(u and u.get("verified_business"))

    def application_for(self, user_id: str) -> dict | None:
        apps = [b for b in self.store.businesses.values() if b["user_id"] == user_id]
        return max(apps, key=lambda b: b["submitted_at"]) if apps else None

    def apply(self, user_id: str, business_name: str, kind: str, registration_no: str, notes: str = "") -> dict:
        u = self.store.users.get(user_id)
        if not u:
            raise not_found("USER_NOT_FOUND", "user not found")
        if kind not in KINDS:
            raise bad("INVALID_KIND", f"kind must be one of {', '.join(KINDS)}")
        name = (business_name or "").strip()
        if not 2 <= len(name) <= 80:
            raise bad("INVALID_NAME", "business (or legal) name must be 2-80 characters")
        reg = re.sub(r"\s+", "", (registration_no or "")).upper()
        if not re.fullmatch(_PATTERNS[kind], reg):
            raise bad("INVALID_REGISTRATION", {"KRA_PIN": "a KRA PIN looks like A123456789Z", "BUSINESS_REG": "enter your business registration number (4-24 letters, digits, / . -)",
                                                 "ID_NUMBER": "a national ID number is 7-9 digits"}[kind])
        if u.get("verified_business"):
            raise conflict("ALREADY_VERIFIED", "this account is already verified")
        cur = self.application_for(user_id)
        if cur and cur["status"] == "PENDING":
            raise conflict("ALREADY_PENDING", "you already have an application under review")
        for b in self.store.businesses.values():  # nobody can borrow someone else's registration to earn their badge
            if b["registration_no"] == reg and b["status"] in ("PENDING", "APPROVED") and b["user_id"] != user_id:
                raise conflict("REGISTRATION_TAKEN", "that registration number is already used by another account")
        now = self.clock.now()
        b = {"id": str(uuid.uuid4()), "user_id": user_id, "business_name": name, "kind": kind, "registration_no": reg, "notes": (notes or "")[:500],
             "status": "PENDING", "submitted_at": now, "reviewed_at": None, "reviewed_by": None, "review_note": ""}
        self.store.businesses[b["id"]] = b
        return b

    def review(self, admin: dict, application_id: str, approve: bool, note: str = "") -> dict:
        b = self.store.businesses.get(application_id)
        if not b:
            raise not_found("APPLICATION_NOT_FOUND", "application not found")
        if b["status"] != "PENDING":
            raise conflict("ALREADY_REVIEWED", f"this application is already {b['status'].lower()}")
        if not approve and not (note or "").strip():
            raise bad("NOTE_REQUIRED", "say why the application was rejected so the applicant can fix it")
        now = self.clock.now()
        b.update(status="APPROVED" if approve else "REJECTED", reviewed_at=now, reviewed_by=admin["name"], review_note=(note or "")[:500])
        u = self.store.users.get(b["user_id"])
        if approve and u:
            u["verified_business"] = {"name": b["business_name"], "kind": b["kind"], "since": now, "application_id": b["id"]}
        self._notify_user(b["user_id"], "verification", f"Your business verification was {'approved — you now carry the verified badge' if approve else 'not approved: ' + (note or '')}.")
        return b

    def revoke(self, admin: dict, user_id: str, note: str) -> None:
        u = self.store.users.get(user_id)
        if not u or not u.get("verified_business"):
            raise not_found("NOT_VERIFIED", "that user has no verified badge")
        if not (note or "").strip():
            raise bad("NOTE_REQUIRED", "a reason is required to revoke a badge")
        app = self.store.businesses.get(u["verified_business"].get("application_id") or "")
        if app:
            app.update(status="REVOKED", reviewed_at=self.clock.now(), reviewed_by=admin["name"], review_note=note[:500])
        u.pop("verified_business", None)
        self._notify_user(user_id, "verification", f"Your verified badge was removed: {note}")

    def _notify_user(self, user_id: str, kind: str, text: str) -> None:
        for a in self.store.agents.values():
            if a["principal_user_id"] == user_id:
                self.notify(a["agent_id"], kind, text)
                break  # one message per user, not per agent

    def pending_applications(self) -> list[dict]:
        return sorted((b for b in self.store.businesses.values() if b["status"] == "PENDING"), key=lambda b: b["submitted_at"])

    # ------------------------------------------------------------------ disputes

    def _open_for(self, match: dict) -> bool:
        return bool(match.get("dispute_open"))

    def _party(self, m: dict, agent_id: str) -> str:
        if agent_id == m["seller_agent_id"]:
            return "seller"
        if agent_id == m["buyer_agent_id"]:
            return "buyer"
        raise forbidden("NOT_A_PARTY", "agent is not a party to this match")

    def open_dispute(self, agent_id: str, match_id: str, category: str, statement: str) -> dict:
        m = self.matches.get_match(match_id, agent_id)
        if category not in CATEGORIES:
            raise bad("INVALID_CATEGORY", f"category must be one of {', '.join(CATEGORIES)}")
        text = (statement or "").strip()
        if not 10 <= len(text) <= 1000:
            raise bad("INVALID_STATEMENT", "describe what happened in 10-1000 characters")
        if not m.get("contact_reveal") or m["status"] not in ELIGIBLE:
            raise bad("NOT_DISPUTABLE", "a dispute can be opened once contact details have been exchanged")
        now = self.clock.now()
        if now - (m.get("revealed_at") or m["updated_at"]) > DISPUTE_WINDOW_MS:
            raise bad("WINDOW_CLOSED", "disputes must be opened within 30 days of the contact exchange")
        earlier = [d for d in self.store.disputes.values() if d["match_id"] == match_id]
        if any(d["status"] != "WITHDRAWN" for d in earlier) or len(earlier) >= 3:
            raise conflict("ALREADY_DISPUTED", "this match already has a dispute")  # a withdrawn one may be reopened, but not forever
        if sum(1 for d in self.store.disputes.values() if d["opened_by"] == agent_id and d["status"] in ("OPEN", "RESPONDED")) >= MAX_OPEN_PER_AGENT:
            raise AppError("TOO_MANY_DISPUTES", "you already have several disputes open — wait for them to be resolved", 429)
        other = m["buyer_agent_id"] if self._party(m, agent_id) == "seller" else m["seller_agent_id"]
        d = {"id": str(uuid.uuid4()), "match_id": match_id, "opened_by": agent_id, "against": other, "category": category, "status": "OPEN",
             "statements": [{"agent_id": agent_id, "text": text, "at": now}], "opened_at": now, "respond_by": now + RESPONSE_WINDOW_MS,
             "ruling": None, "resolved_at": None}
        self.store.disputes[d["id"]] = d
        m["dispute_open"], m["dispute_id"] = True, d["id"]
        self.notify(other, "dispute", f"A dispute was opened on \"{m['agreed_terms']['title']}\" ({CATEGORIES[category]}). "
                    f"Please give your side within {RESPONSE_WINDOW_MS // (24 * 3600_000)} days.", match_id=match_id, dispute_id=d["id"])
        return d

    def add_statement(self, agent_id: str, dispute_id: str, text: str) -> dict:
        d = self._get(dispute_id)
        if agent_id not in (d["opened_by"], d["against"]):
            raise forbidden("NOT_A_PARTY", "agent is not a party to this dispute")
        if d["status"] not in ("OPEN", "RESPONDED"):
            raise bad("DISPUTE_CLOSED", f"dispute is {d['status'].lower()}")
        text = (text or "").strip()
        if not 5 <= len(text) <= 1000:
            raise bad("INVALID_STATEMENT", "a statement is 5-1000 characters")
        if sum(1 for s in d["statements"] if s["agent_id"] == agent_id) >= MAX_STATEMENTS_PER_PARTY:
            raise bad("TOO_MANY_STATEMENTS", f"each party can add up to {MAX_STATEMENTS_PER_PARTY} statements")
        d["statements"].append({"agent_id": agent_id, "text": text, "at": self.clock.now()})
        if agent_id == d["against"] and d["status"] == "OPEN":
            d["status"] = "RESPONDED"
            self.notify(d["opened_by"], "dispute", "The other party answered your dispute. An administrator will now review it.", dispute_id=d["id"])
        return d

    def withdraw(self, agent_id: str, dispute_id: str) -> dict:
        d = self._get(dispute_id)
        if agent_id != d["opened_by"]:
            raise forbidden("NOT_OPENER", "only the party who opened a dispute can withdraw it")
        if d["status"] not in ("OPEN", "RESPONDED"):
            raise bad("DISPUTE_CLOSED", f"dispute is {d['status'].lower()}")
        d.update(status="WITHDRAWN", resolved_at=self.clock.now())
        m = self.store.matches[d["match_id"]]
        m["dispute_open"] = False
        if m["status"] == "CONTACT_REVEALED" and m["outcome_reports"]:
            self.matches._finalize(m)  # outcome reports made while the dispute was open can now take effect
        self.notify(d["against"], "dispute", "The dispute against you was withdrawn.", dispute_id=d["id"])
        return d

    def rule(self, admin: dict, dispute_id: str, outcome: str, note: str) -> dict:
        d = self._get(dispute_id)
        if outcome not in RULINGS:
            raise bad("INVALID_RULING", f"ruling must be one of {', '.join(RULINGS)}")
        if d["status"] not in ("OPEN", "RESPONDED"):
            raise bad("DISPUTE_CLOSED", f"dispute is already {d['status'].lower()}")
        if not (note or "").strip():
            raise bad("NOTE_REQUIRED", "explain the ruling — both parties will see it")
        now = self.clock.now()
        if d["status"] == "OPEN" and now < d["respond_by"]:
            raise bad("AWAITING_RESPONSE", "the other party still has time to answer — rule once they respond or the deadline passes")
        m = self.store.matches[d["match_id"]]
        seller, buyer = m["seller_agent_id"], m["buyer_agent_id"]
        m["fault_agent_ids"] = {"COMPLETED": [], "NO_FAULT": [], "SELLER_AT_FAULT": [seller], "BUYER_AT_FAULT": [buyer], "BOTH_AT_FAULT": sorted([seller, buyer])}[outcome]
        m["status"] = {"COMPLETED": "COMPLETED", "NO_FAULT": "DISPUTED"}.get(outcome, "FELL_THROUGH")
        m["dispute_open"], m["updated_at"] = False, now
        d.update(status="RULED", resolved_at=now, ruling={"outcome": outcome, "note": note[:1000], "by": admin["name"], "at": now})
        recompute_reputation(self.store, seller)
        recompute_reputation(self.store, buyer)
        for aid in (seller, buyer):
            self.notify(aid, "dispute", f"Dispute ruling on \"{m['agreed_terms']['title']}\": {RULINGS[outcome]}. {note}", match_id=m["match_id"], dispute_id=d["id"])
        return d

    def _get(self, dispute_id: str) -> dict:
        d = self.store.disputes.get(dispute_id)
        if not d:
            raise not_found("DISPUTE_NOT_FOUND", "dispute not found")
        return d

    def dispute_for_match(self, match_id: str) -> dict | None:
        found = [d for d in self.store.disputes.values() if d["match_id"] == match_id]
        return sorted(found, key=lambda d: d["opened_at"])[-1] if found else None  # stable: ties keep insertion order

    def open_disputes(self) -> list[dict]:
        return sorted((d for d in self.store.disputes.values() if d["status"] in ("OPEN", "RESPONDED")), key=lambda d: d["opened_at"])
