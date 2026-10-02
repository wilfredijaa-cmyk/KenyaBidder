"""One-time codes: phone/email verification and self-service password reset.

Security shape (all of it deterministic, none of it LLM):
  * 6-digit codes from ``secrets``; only an HMAC of the code is stored, so a leaked database cannot be replayed;
  * 10-minute expiry, 5 wrong guesses lock the code, issuing a new code retires the old one;
  * 60s resend cooldown, 5 codes/hour per account and per destination, plus the platform-wide daily SMS budget — a stranger cannot
    use the form to spam a phone or run up the SMS bill;
  * password-reset answers are identical whether or not the account exists (no user enumeration), and a successful reset
    ends every signed-in session of that account.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid

from .config import is_production
from .errors import AppError, bad, not_found
from .i18n import template
from .messaging import mask

TTL_MS = 10 * 60_000
MAX_ATTEMPTS = 5
COOLDOWN_MS = 60_000
MAX_PER_HOUR = 5
GENERIC_RESET = "If that account has a verified phone or email, we have sent a code to it."


class VerificationService:
    def __init__(self, store, clock, messenger, agents):
        self.store, self.clock, self.messenger, self.agents = store, clock, messenger, agents
        self.on_phone_verified = None  # composition root: claims the signup grant, unblocks match confirmations…

    # ---------- policy ----------

    def phone_required(self) -> bool:
        """Verified phones are mandatory once a real SMS gateway exists (or when an admin insists)."""
        v = self.store.settings.get("require_verified_phone")
        return self.messenger.sms_real if v is None else bool(v)

    def set_phone_required(self, value: bool | None) -> None:
        if value is None:
            self.store.settings.pop("require_verified_phone", None)
        else:
            self.store.settings["require_verified_phone"] = bool(value)

    # ---------- internals ----------

    def _code_hash(self, vid: str, code: str) -> str:
        key = self.store.settings.setdefault("otp_secret", secrets.token_hex(32)).encode()
        return hmac.new(key, f"{vid}:{code}".encode(), hashlib.sha256).hexdigest()

    def _user(self, user_id: str) -> dict:
        u = self.store.users.get(user_id)
        if not u:
            raise not_found("USER_NOT_FOUND", "user not found")
        return u

    def _limits(self, user_id: str, purpose: str, dest: str, now: int) -> None:
        mine = [v for v in self.store.verifications.values() if now - v["created_at"] < 3600_000]
        same = [v for v in mine if v["user_id"] == user_id and v["purpose"] == purpose]
        if same and now - max(v["created_at"] for v in same) < COOLDOWN_MS:
            raise AppError("CODE_COOLDOWN", "a code was just sent — wait a minute before asking for another", 429)
        if sum(1 for v in mine if v["user_id"] == user_id) >= MAX_PER_HOUR or sum(1 for v in mine if v["dest"] == dest) >= MAX_PER_HOUR:
            raise AppError("CODE_LIMIT", "too many codes requested — try again in an hour", 429)

    async def _issue(self, user: dict, purpose: str, channel: str, dest: str, text_for, *, background: bool = False) -> dict:
        now = self.clock.now()
        self._limits(user["id"], purpose, dest, now)
        for v in self.store.verifications.values():  # a new code retires every older pending one
            if v["user_id"] == user["id"] and v["purpose"] == purpose and v["status"] == "PENDING":
                v["status"] = "SUPERSEDED"
        vid, code = str(uuid.uuid4()), f"{secrets.randbelow(10**6):06d}"
        rec = {"id": vid, "user_id": user["id"], "purpose": purpose, "channel": channel, "dest": dest, "code_hash": self._code_hash(vid, code),
               "created_at": now, "expires_at": now + TTL_MS, "attempts": 0, "status": "PENDING"}
        self.store.verifications[vid] = rec

        async def deliver():
            try:
                if channel == "SMS":
                    await self.messenger.send_sms(dest, text_for(code), purpose.lower())
                else:
                    await self.messenger.send_email(dest, "Your KenyaBidder code", text_for(code), purpose.lower())
            except AppError:
                rec["status"] = "FAILED"
                if background:  # the user was told "sent" regardless: let an immediate retry through instead of trapping them in the cooldown
                    self.store.verifications.pop(vid, None)
                raise
        if background:  # password reset: answer immediately so response time cannot reveal whether the account exists
            if not self.messenger.spawn(deliver()):
                await deliver()
        else:
            await deliver()
        out = {"id": vid, "channel": channel, "sent_to": mask(dest), "expires_in_s": TTL_MS // 1000}
        provider = self.messenger.sms if channel == "SMS" else self.messenger.email
        if not getattr(provider, "real", False) and not is_production():  # never on a production site: the code IS the proof of ownership
            out["dev_code"] = code  # no real gateway configured: show the code on screen so the flow can be tried end to end
        return out

    def _check(self, user_id: str, purpose: str, code: str) -> dict:
        now = self.clock.now()
        rec = next((v for v in sorted(self.store.verifications.values(), key=lambda v: -v["created_at"])
                    if v["user_id"] == user_id and v["purpose"] == purpose and v["status"] == "PENDING"), None)
        if not rec or now > rec["expires_at"]:
            if rec:
                rec["status"] = "EXPIRED"
            raise AppError("CODE_INVALID", "that code is wrong or has expired — request a new one", 400)
        rec["attempts"] += 1
        if not hmac.compare_digest(self._code_hash(rec["id"], str(code or "").strip()), rec["code_hash"]):
            if rec["attempts"] >= MAX_ATTEMPTS:
                rec["status"] = "LOCKED"
            raise AppError("CODE_INVALID", "that code is wrong or has expired — request a new one", 400)
        rec["status"] = "USED"
        return rec

    # ---------- phone / email verification ----------

    async def send_phone_code(self, user_id: str) -> dict:
        u = self._user(user_id)
        if not u.get("phone"):
            raise bad("NO_PHONE", "add a phone number to your profile first")
        if u.get("phone_verified"):
            raise bad("ALREADY_VERIFIED", "this phone number is already verified")
        return await self._issue(u, "PHONE", "SMS", u["phone"], lambda c: template("phone_code", u.get("lang"), code=c))

    def confirm_phone(self, user_id: str, code: str) -> dict:
        u = self._user(user_id)
        rec = self._check(user_id, "PHONE", code)
        if rec["dest"] != u.get("phone"):
            raise AppError("CODE_INVALID", "the phone number changed since that code was sent — request a new one", 400)
        u["phone_verified"], u["phone_verified_at"] = True, self.clock.now()
        if self.on_phone_verified:
            self.on_phone_verified(u)
        return u

    async def send_email_code(self, user_id: str) -> dict:
        u = self._user(user_id)
        if not u.get("email"):
            raise bad("NO_EMAIL", "add an email address to your profile first")
        if u.get("email_verified"):
            raise bad("ALREADY_VERIFIED", "this email address is already verified")
        return await self._issue(u, "EMAIL", "EMAIL", u["email"], lambda c: template("email_code", u.get("lang"), code=c))

    def confirm_email(self, user_id: str, code: str) -> dict:
        u = self._user(user_id)
        rec = self._check(user_id, "EMAIL", code)
        if rec["dest"] != u.get("email"):
            raise AppError("CODE_INVALID", "the email address changed since that code was sent — request a new one", 400)
        u["email_verified"], u["email_verified_at"] = True, self.clock.now()
        return u

    # ---------- password reset ----------

    def _throttle(self, key: str, limit: int, window_ms: int) -> None:
        book = self.store.settings.setdefault("reset_requests", {})
        now = self.clock.now()
        if len(book) > 2_000:
            for k in [k for k, ts in book.items() if not [t for t in ts if now - t < window_ms]]:
                del book[k]
        ts = [t for t in book.get(key, []) if now - t < window_ms]
        if len(ts) >= limit:
            raise AppError("TOO_MANY_REQUESTS", "too many reset requests — try again later", 429)
        book[key] = [*ts, now]

    async def request_password_reset(self, name: str) -> dict:
        """Always answers the same thing. Codes go to a VERIFIED phone (SMS) or, failing that, a verified email."""
        self._throttle("all", 60, 60_000)  # global brake on someone hammering the form with invented names
        self._throttle((name or "").strip().lower()[:60], 5, 3600_000)
        u = self.agents._find(name)
        if u and not u.get("suspended"):
            try:
                if u.get("phone") and u.get("phone_verified"):
                    await self._issue(u, "RESET", "SMS", u["phone"], lambda c: template("reset_sms", u.get("lang"), code=c), background=True)
                elif u.get("email") and u.get("email_verified"):
                    await self._issue(u, "RESET", "EMAIL", u["email"], lambda c: template("reset_email", u.get("lang"), code=c), background=True)
            except AppError:
                pass  # rate limit / delivery problems must look identical to success from the outside
        return {"message": GENERIC_RESET}

    def reset_password(self, name: str, code: str, new_password: str) -> dict:
        u = self.agents._find(name)
        if not u or u.get("suspended"):
            raise AppError("CODE_INVALID", "that code is wrong or has expired — request a new one", 400)
        if len(new_password or "") < 8:
            raise bad("INVALID_USER", "password must be at least 8 characters")
        rec = self._check(u["id"], "RESET", code)
        try:
            self.agents.set_password(u["id"], new_password)
        except Exception:
            rec["status"], rec["attempts"] = "PENDING", rec["attempts"] - 1  # a weak password must not burn a valid code: let them pick a better one
            raise
        return u
