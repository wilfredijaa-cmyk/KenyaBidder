"""Data-subject tools (Kenya Data Protection Act 2019): a copy of your data, and deletion.

* **Export** — everything the platform holds about the user, as JSON: profile, agents (without secrets), matches, orders, token
  ledger, notifications, disputes, verification and subscription. It never contains another person's private details or any
  credential/hash.
* **Deletion** — personal identifiers are erased or anonymised at once and the account can never sign in again. What the law
  requires us to keep (payments and the token ledger, kept under the anonymous user id) stays; counterparties keep the history of a
  deal but see "deleted user". Deletion is refused while money, a live deal or a dispute is unresolved, so nobody can vanish
  owing something or holding someone else's tokens.
"""
from __future__ import annotations

import copy
import json
import re
import secrets

from .errors import AppError, bad, forbidden, not_found

LIVE_MATCH = ("PROPOSED", "SELLER_CONFIRMED", "BUYER_CONFIRMED", "CONTACT_REVEALED")
SECRET_KEYS = {"password_hash", "api_key", "code_hash", "storage_secret", "token", "secret"}


def _scrub(obj):
    """Deep copy without anything that looks like a credential."""
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items() if k not in SECRET_KEYS}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


class PrivacyService:
    def __init__(self, store, clock, wallet, billing, agents):
        self.store, self.clock, self.wallet, self.billing, self.agents = store, clock, wallet, billing, agents

    # ------------------------------------------------------------------ export

    def export(self, user_id: str) -> dict:
        u = self.store.users.get(user_id)
        if not u or u.get("deleted_at"):
            raise not_found("USER_NOT_FOUND", "user not found")
        mine = {a["agent_id"] for a in self.store.agents.values() if a["principal_user_id"] == user_id}

        def name_of(agent_id):
            return self.store.users.get(self.store.agents.get(agent_id, {}).get("principal_user_id"), {}).get("name", "deleted user")

        matches = []
        for m in self.store.matches.values():
            if m["seller_agent_id"] in mine or m["buyer_agent_id"] in mine:
                mm = _scrub({k: v for k, v in m.items() if k != "contact_reveal"})
                mm["counterparty"] = (name_of(m["buyer_agent_id"] if m["seller_agent_id"] in mine else m["seller_agent_id"])
                                      if m.get("contact_reveal") else "not revealed (contact details were never exchanged)")
                matches.append(mm)
        orders = self.billing.list_orders(user_id=user_id, limit=100_000)
        return {
            "exported_at": self.clock.now(), "format": "kenyabidder-export-v1",
            "profile": _scrub({k: v for k, v in u.items() if k not in ("alerts_sent",)}),
            "agents": [_scrub(a) for a in self.store.agents.values() if a["agent_id"] in mine],
            "matches": matches,
            "orders": [{k: o[k] for k in ("id", "pack_name", "tokens", "amount_kes", "provider", "status", "receipt", "created_at", "updated_at")} for o in orders],
            "token_balances": self.wallet.balances(user_id),
            "token_ledger": self.wallet.ledger(user_id=user_id, limit=100_000),
            "notifications": [n for n in self.store.notifications if n["agent_id"] in mine],
            "disputes": [_scrub(d) for d in self.store.disputes.values() if d["opened_by"] in mine or d["against"] in mine],
            "business_verification": [_scrub(b) for b in self.store.businesses.values() if b["user_id"] == user_id],
            "subscription": _scrub(self.store.subscriptions.get(user_id)),
            "consent": {"terms_accepted_at": u.get("terms_accepted_at"), "terms_version": u.get("terms_version")},
        }

    def export_json(self, user_id: str) -> str:
        return json.dumps(self.export(user_id), indent=2, default=str)

    # ------------------------------------------------------------------ deletion

    def blockers(self, user_id: str) -> list[str]:
        out = []
        mine = {a["agent_id"] for a in self.store.agents.values() if a["principal_user_id"] == user_id}
        if any(o["status"] in ("PENDING", "AWAITING_REVIEW") for o in self.billing.list_orders(user_id=user_id, limit=100_000)):
            out.append("a token payment is still being processed")
        if any((m["seller_agent_id"] in mine or m["buyer_agent_id"] in mine) and m["status"] in LIVE_MATCH for m in self.store.matches.values()):
            out.append("you have a live deal — finish or report it first")
        if any(d["status"] in ("OPEN", "RESPONDED") and (d["opened_by"] in mine or d["against"] in mine) for d in self.store.disputes.values()):
            out.append("you have an open dispute")
        if any(a["status"] in ("ACTIVE", "EXTENDING", "SCHEDULED") and a.get("poster_agent_id") in mine and a["bids"] for a in self.store.auctions.values()):
            out.append("one of your listings has live bids")
        if any(a["status"] in ("ACTIVE", "EXTENDING") and ((a["auction_type"] in ("FIRST_PRICE_SEALED", "SECOND_PRICE_SEALED", "REVERSE_SEALED") and any(b["agent_id"] in mine for b in a["bids"]))
                                                            or (a["bids"] and a["bids"][-1]["agent_id"] in mine)) for a in self.store.auctions.values()):
            out.append("your agent is currently leading or has a sealed bid on an open auction")
        u = self.store.users.get(user_id)
        if u and u["role"] == "admin" and sum(1 for x in self.store.users.values() if x["role"] == "admin" and not x.get("deleted_at")) <= 1:
            out.append("you are the only administrator")
        return out

    def delete_account(self, user_id: str, password: str, *, forfeit_tokens: bool = False) -> dict:
        u = self.store.users.get(user_id)
        if not u or u.get("deleted_at"):
            raise not_found("USER_NOT_FOUND", "user not found")
        self.agents.reauth(user_id, password)  # shared lockout: no brute-forcing the password through this form
        blockers = self.blockers(user_id)
        if blockers:
            raise AppError("DELETION_BLOCKED", "we cannot delete the account yet: " + "; ".join(blockers), 409)
        held = {k: v for k, v in self.wallet.balances(user_id).items() if v > 0}
        if held and not forfeit_tokens:
            raise bad("TOKENS_HELD", f"you still hold {sum(held.values()):,} tokens; deleting forfeits them (no refund) — confirm to proceed")
        now = self.clock.now()
        alias = f"deleted-{user_id[:6]}"
        self._scrub_logs(u)  # first, while the identifiers are still known (nothing personal is kept to finish it later)
        # 1. Lock the account FIRST: whatever happens next, it can never sign in again and nothing personal is left on the profile.
        u.update(name=alias, phone=None, email=None, phone_verified=False, email_verified=False, suspended=True, deleted_at=now,
                 deletion_pending=True, password_hash="deleted$" + secrets.token_hex(16), role="user", session_version=u.get("session_version", 0) + 1)
        u.pop("verified_business", None)
        # 2. Scrub everything else (idempotent: if it is interrupted, resume_pending() finishes it from the maintenance loop).
        self._scrub(user_id)
        self.store.admin_log.append({"at": now, "admin_id": None, "admin": "system", "action": "account_deleted", "detail": {"user": alias}})
        return {"deleted": True, "kept": "payment and token-ledger records (legal retention; they can carry the phone number used to pay or claim the signup grant), under an anonymous id"}

    def _scrub_logs(self, u: dict) -> None:
        """Delivery and ops logs outlive the account: the number, name and address must not stay readable in them."""
        phone = u.get("phone") or ""
        digits = re.sub(r"\D", "", phone)
        needles = {x for x in (phone, digits, "0" + digits[3:] if digits.startswith("254") else "", digits[3:] if digits.startswith("254") else "",
                               u.get("name"), u.get("email")) if x and len(x) >= 3}
        if not needles:
            return
        def loose(n: str) -> str:  # "0712345678" also matches "0712 345 678" / "+254-712-345-678"
            return r"[\s.-]*".join(re.escape(c) for c in n) if n.lstrip("+").isdigit() else re.escape(n)
        rx = re.compile("|".join(r"(?<!\w)" + loose(n) + r"(?!\w)" for n in sorted(needles, key=len, reverse=True)), re.I)
        for log in (self.store.outbox, self.store.admin_log):
            for e in log:
                for k in e:
                    if k not in ("action", "at", "channel"):  # structural fields, not personal data
                        _redact_in(e, k, rx)
        for log in (self.store.outbox, self.store.admin_log):
            log._dirty()  # in-place edits far from the end: make the durable copy rewrite the whole log

    def _scrub(self, user_id: str) -> None:
        u = self.store.users[user_id]
        mine = [a for a in self.store.agents.values() if a["principal_user_id"] == user_id]
        ids = {a["agent_id"] for a in mine}
        for a in mine:
            self.agents.set_status(a["agent_id"], "SUSPENDED")  # cancels every live trigger at once
            a["channel_identity_map"] = []
            a["durable_memory"].update(conversation=[], notes="", watch=None, one_off_authorizations=[])
            a["config"]["tools"], a["config"]["kb_ids"] = [], []
        for auction in self.store.auctions.values():  # open listings/RFQs nobody has bid on simply come down
            if auction.get("poster_agent_id") in ids and auction["status"] in ("SCHEDULED", "ACTIVE", "EXTENDING") and not auction["bids"]:
                auction["status"], auction["result"] = "CANCELLED", {"outcome": "CANCELLED", "reason": "poster deleted their account"}
        for m in self.store.matches.values():
            if m.get("contact_reveal"):
                for key, agent_key in (("seller_contact", "seller_agent_id"), ("buyer_contact", "buyer_agent_id")):
                    if m[agent_key] in ids and m["contact_reveal"].get(key):
                        m["contact_reveal"][key] = {"name": "deleted user", "phone": None, "email": None, "verified_business": None}
                        self.store.thaw("matches", m["match_id"])  # old matches are frozen in the database: make sure the erasure reaches it
        self.store.notifications[:] = [n for n in self.store.notifications if n["agent_id"] not in ids]
        for v in [v["id"] for v in self.store.verifications.values() if v["user_id"] == user_id]:
            del self.store.verifications[v]
        for b in self.store.businesses.values():
            if b["user_id"] == user_id:
                b.update(business_name="(deleted)", registration_no="(deleted)", notes="")
        for llm_id, bal in self.wallet.balances(user_id).items():
            if bal > 0:
                self.wallet.adjust(user_id, llm_id, -bal, ref=f"forfeit:{user_id}:{llm_id}", meta={"reason": "account deleted, tokens forfeited"})
        u.pop("deletion_pending", None)

    def resume_pending(self) -> int:
        """Finish any deletion that was interrupted after the account was locked."""
        n = 0
        for uid in [k for k, u in self.store.users.items() if u.get("deletion_pending")]:
            try:
                self._scrub(uid)
                n += 1
            except Exception:  # noqa: BLE001  stays pending; retried on the next maintenance run
                pass
        return n


def _redact_in(container, key, rx) -> None:
    """Replace identifiers inside container[key] (text, or nested dicts/lists of text) in place."""
    v = container[key]
    if isinstance(v, str):
        nv = rx.sub("[deleted]", v)
        if nv != v:
            container[key] = nv
    elif isinstance(v, (dict, list)):
        for k in (v.keys() if isinstance(v, dict) else range(len(v))):
            _redact_in(v, k, rx)
