"""Listing hygiene (an idea from Jiji's fraud approach): a prohibited-items list at creation, user reports, auto-hide when several
distinct users report the same unbid listing, and an admin queue (take down / dismiss / ban the poster)."""
from __future__ import annotations

import re
import uuid

from .errors import AppError, bad, conflict, forbidden, not_found

REASONS = {"SPAM": "Spam or duplicate", "FRAUD": "Looks like a scam", "PROHIBITED": "Prohibited or illegal item", "MISLEADING": "Misleading description or price", "OTHER": "Something else"}
AUTO_HIDE_AFTER = 3
MAX_REPORTS_PER_DAY = 10
DEFAULT_PROHIBITED = ["firearm", "ammunition", "explosive", "cocaine", "heroin", "narcotic", "ivory", "rhino horn", "pangolin", "counterfeit", "stolen", "forged",
                      "fake passport", "fake id", "human organ", "child sexual"]


def prohibited_terms(store) -> list[str]:
    t = store.settings.get("prohibited_terms")
    return DEFAULT_PROHIBITED if t is None else list(t)


def check_prohibited(store, spec: dict) -> None:
    """Called by the engine on every new listing / RFQ."""
    text = " ".join(str(spec.get(k, "")) for k in ("title", "description", "category", "brand")).lower()
    for term in prohibited_terms(store):
        if term and re.search(r"(?<![a-z])" + re.escape(term.lower()) + r"s?(?![a-z])", text):
            raise AppError("PROHIBITED_ITEM", f"listings mentioning \"{term}\" are not allowed on KenyaBidder", 422)


class ModerationService:
    def __init__(self, store, clock, notify=None, agents=None):
        self.store, self.clock, self.agents = store, clock, agents
        self.notify = notify or (lambda *a, **k: None)

    def _auction(self, auction_id: str) -> dict:
        a = self.store.auctions.get(auction_id)
        if not a:
            raise not_found("AUCTION_NOT_FOUND", "auction not found")
        return a

    def report(self, user_id: str, auction_id: str, reason: str, note: str = "") -> dict:
        a = self._auction(auction_id)
        if reason not in REASONS:
            raise bad("INVALID_REASON", f"reason must be one of {', '.join(REASONS)}")
        poster = self.store.agents.get(a.get("poster_agent_id") or "") or {}
        if poster.get("principal_user_id") == user_id:
            raise forbidden("OWN_LISTING", "you can't report your own listing")
        mine = [r for r in self.store.reports.values() if r["reporter"] == user_id]
        if any(r["auction_id"] == auction_id for r in mine):
            raise conflict("ALREADY_REPORTED", "you already reported this listing")
        now = self.clock.now()
        if sum(1 for r in mine if now - r["at"] < 24 * 3600_000) >= MAX_REPORTS_PER_DAY:
            raise AppError("REPORT_LIMIT", "you've sent many reports today — please wait", 429)
        r = {"id": str(uuid.uuid4()), "auction_id": auction_id, "reporter": user_id, "reason": reason, "note": (note or "")[:300], "at": now, "status": "OPEN",
             "resolved_by": None, "resolution": None}
        self.store.reports[r["id"]] = r
        open_by = {x["reporter"] for x in self.store.reports.values() if x["auction_id"] == auction_id and x["status"] == "OPEN"}
        if len(open_by) >= AUTO_HIDE_AFTER and not a["bids"] and a["status"] in ("SCHEDULED", "ACTIVE", "EXTENDING") and not a.get("hidden"):
            a["hidden"] = True  # hidden pending review; a listing with live bids is never hidden by users alone (it could be griefed)
            if poster:
                self.notify(poster["agent_id"], "moderation", f"Your listing \"{a['product_spec']['title']}\" was hidden while moderators review reports.", auction_id=auction_id)
        return r

    def open_reports(self) -> list[dict]:
        return sorted((r for r in self.store.reports.values() if r["status"] == "OPEN"), key=lambda r: r["at"])

    def resolve(self, admin: dict, report_id: str, action: str, note: str = "") -> dict:
        r = self.store.reports.get(report_id)
        if not r:
            raise not_found("REPORT_NOT_FOUND", "report not found")
        if r["status"] != "OPEN":
            raise conflict("ALREADY_RESOLVED", "report already resolved")
        if action not in ("TAKEDOWN", "DISMISS", "BAN_POSTER"):
            raise bad("INVALID_ACTION", "action must be TAKEDOWN, DISMISS or BAN_POSTER")
        if action != "DISMISS" and not (note or "").strip():
            raise bad("NOTE_REQUIRED", "say why — the poster is told")
        a = self._auction(r["auction_id"])
        poster = self.store.agents.get(a.get("poster_agent_id") or "")
        if action in ("TAKEDOWN", "BAN_POSTER") and a["status"] in ("SCHEDULED", "ACTIVE", "EXTENDING"):
            a["status"], a["result"] = "CANCELLED", {"outcome": "CANCELLED", "reason": "removed by moderators: " + (note or "")[:200]}
            a["hidden"] = False
        if action == "BAN_POSTER" and poster and self.agents:
            self.agents.set_suspended(poster["principal_user_id"], True, by=admin.get("id"))
        status = "ACTIONED" if action != "DISMISS" else "DISMISSED"
        for x in self.store.reports.values():  # one decision settles every open report on the same listing
            if x["auction_id"] == r["auction_id"] and x["status"] == "OPEN":
                x.update(status=status, resolved_by=admin["name"], resolution=f"{action}: {note}"[:300])
        if action == "DISMISS":
            a["hidden"] = False
        elif poster:
            self.notify(poster["agent_id"], "moderation", f"Your listing \"{a['product_spec']['title']}\" was removed by moderators: {note}", auction_id=a["auction_id"])
        return r
