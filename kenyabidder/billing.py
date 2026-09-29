"""Selling LLM tokens: packs, checkout, payment orders, grants, refunds and revenue reporting.

This is the platform's *own* revenue (like SaaS billing, spec §16.6) and is deliberately separate from auction
settlement: buyers and sellers still pay each other directly — the platform never touches that money.
"""
from __future__ import annotations

import csv
import hmac
import io
import re
import uuid

from .db import IntegrityError
from .errors import AppError, bad, conflict, forbidden, not_found
from .payments import MpesaClient, callback_receipt
from .phone import mpesa_msisdn, normalize_phone
from .wallet import WalletDB, billing_of

RECEIPT_RE = re.compile(r"^[A-Z0-9]{8,14}$")
OPEN_STATUSES = ("PENDING", "AWAITING_REVIEW")
MIN_PRICE_KES, MAX_PRICE_KES = 10, 250_000  # M-Pesa transaction limits
ORDER_COLS = "id,user_id,pack_id,pack_name,llm_id,tokens,amount_kes,provider,status,phone,external_ref,receipt,note,created_at,updated_at,data"


def _row(r: dict) -> dict:
    import json
    d = dict(r)
    d["data"] = json.loads(d.get("data") or "{}")
    d["reference"] = "KB-" + d["id"][:6].upper()
    return d


def csv_safe(v) -> str:
    """Neutralise spreadsheet formula injection in exported user-controlled text."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


class BillingService:
    ORDER_TTL_MS = 30 * 60_000
    MAX_OPEN_ORDERS_PER_USER = 3
    STK_PER_PHONE_PER_HOUR = 3

    def __init__(self, store, clock, db: WalletDB, llms, meter, notify=None, mpesa: MpesaClient | None = None,
                 dev_payments: bool = False, on_credit=None):
        self.store, self.clock, self.db, self.llms, self.meter = store, clock, db, llms, meter
        self.notify = notify or (lambda *a, **k: None)
        self.mpesa, self.dev_payments, self.on_credit = mpesa, dev_payments, on_credit
        self.grant_gate = None  # fn(user) -> bool; set by the composition root (verified phone required?)
        self.sql = db.database  # orders / claims are plain SQL on the same database as the ledger

    # ------------------------------------------------------------------ admin log

    def log_admin(self, admin: dict | None, action: str, **detail) -> None:
        self.store.admin_log.append({"at": self.clock.now(), "admin_id": (admin or {}).get("id"), "admin": (admin or {}).get("name", "system"),
                                     "action": action, "detail": detail})
        del self.store.admin_log[:-5000]

    # ------------------------------------------------------------------ settings

    @property
    def policy(self) -> str:
        return self.meter.policy

    def set_policy(self, admin: dict, policy: str) -> None:
        if policy not in ("block", "fallback"):
            raise bad("INVALID_POLICY", "policy must be 'block' or 'fallback'")
        self.store.settings["token_policy"] = policy
        self.log_admin(admin, "set_token_policy", policy=policy)

    def set_limits(self, admin: dict | None, *, signup_grants_per_day: int | None = None, max_llm_decisions_per_hour: int | None = None) -> None:
        wanted = {k: v for k, v in (("signup_grants_per_day", signup_grants_per_day), ("max_llm_decisions_per_hour", max_llm_decisions_per_hour)) if v is not None}
        for key, v in wanted.items():  # validate ALL before writing ANY
            if not (isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100_000):
                raise bad("INVALID_LIMIT", f"{key} must be a whole number from 0 to 100,000 (0 = unlimited)")
        if wanted:
            self.store.settings.update(wanted)
            self.log_admin(admin, "set_limits", **wanted)

    def manual_instructions(self) -> str:
        return self.store.settings.get("manual_payment_instructions", "")

    def set_manual_instructions(self, admin: dict, text: str) -> None:
        self.store.settings["manual_payment_instructions"] = (text or "").strip()[:600]
        self.log_admin(admin, "set_manual_payment_instructions")

    def methods(self) -> list[dict]:
        """Payment methods currently usable, in display order."""
        out = []
        if self.mpesa:
            out.append({"id": "mpesa", "label": "M-Pesa (STK push to your phone)"})
        if self.manual_instructions():
            out.append({"id": "manual", "label": "Pay to our Till/Paybill, then enter the M-Pesa code"})
        if self.dev_payments:
            out.append({"id": "dev", "label": "Instant test payment (development only)"})
        return out

    # ------------------------------------------------------------------ packs

    def _validate_pack(self, name, llm_id, tokens, price_kes) -> None:
        if not str(name or "").strip():
            raise bad("INVALID_PACK", "name is required")
        entry = self.llms.get(llm_id)
        if not self.meter.is_metered(entry):
            raise bad("INVALID_PACK", f"{entry['name']} is a free (platform-funded) LLM — there is nothing to sell")
        if not (isinstance(tokens, int) and not isinstance(tokens, bool) and 1_000 <= tokens <= 1_000_000_000):
            raise bad("INVALID_PACK", "tokens must be a whole number between 1,000 and 1,000,000,000")
        if not (isinstance(price_kes, int) and not isinstance(price_kes, bool) and MIN_PRICE_KES <= price_kes <= MAX_PRICE_KES):
            raise bad("INVALID_PACK", f"price must be a whole number of KES between {MIN_PRICE_KES} and {MAX_PRICE_KES:,}")

    def add_pack(self, admin: dict, *, name: str, llm_id: str, tokens: int, price_kes: int, description: str = "", enabled: bool = True) -> dict:
        self._validate_pack(name, llm_id, tokens, price_kes)
        p = {"id": str(uuid.uuid4()), "name": name.strip()[:60], "llm_id": llm_id, "tokens": tokens, "price_kes": price_kes,
             "description": (description or "").strip()[:200], "enabled": enabled, "created_at": self.clock.now()}
        self.store.packs[p["id"]] = p
        self.log_admin(admin, "add_pack", pack=p["name"], tokens=tokens, price_kes=price_kes)
        return p

    def update_pack(self, admin: dict, pack_id: str, **patch) -> dict:
        p = self.get_pack(pack_id)
        merged = {**p, **{k: v for k, v in patch.items() if k in ("name", "llm_id", "tokens", "price_kes", "description", "enabled") and v is not None}}
        self._validate_pack(merged["name"], merged["llm_id"], merged["tokens"], merged["price_kes"])
        merged["name"], merged["description"] = merged["name"].strip()[:60], (merged["description"] or "").strip()[:200]
        p.update(merged)
        self.log_admin(admin, "update_pack", pack=p["name"], tokens=p["tokens"], price_kes=p["price_kes"], enabled=p["enabled"])
        return p

    def remove_pack(self, admin: dict, pack_id: str) -> None:
        p = self.get_pack(pack_id)
        del self.store.packs[pack_id]  # past orders keep their own snapshot of tokens/price
        self.log_admin(admin, "remove_pack", pack=p["name"])

    def get_pack(self, pack_id: str) -> dict:
        p = self.store.packs.get(pack_id)
        if not p:
            raise not_found("PACK_NOT_FOUND", "token pack not found")
        return p

    def list_packs(self, enabled_only: bool = False) -> list[dict]:
        return sorted((p for p in self.store.packs.values() if (not enabled_only or (p["enabled"] and self._sellable(p)))),
                      key=lambda p: (p["price_kes"], p["name"]))

    def _sellable(self, p: dict) -> bool:
        e = self.store.llms.get(p["llm_id"])
        return bool(e and e["enabled"] and self.meter.is_metered(e))

    # ------------------------------------------------------------------ grants

    def signup_grants(self) -> dict[str, int]:
        return dict(self.store.settings.get("signup_grants", {}))

    def set_signup_grant(self, admin: dict, llm_id: str, tokens: int) -> None:
        entry = self.llms.get(llm_id)
        if not self.meter.is_metered(entry):
            raise bad("INVALID_GRANT", f"{entry['name']} is free — no grant needed")
        if not (isinstance(tokens, int) and not isinstance(tokens, bool) and 0 <= tokens <= 100_000_000):
            raise bad("INVALID_GRANT", "tokens must be a whole number (0 removes the grant)")
        g = self.store.settings.setdefault("signup_grants", {})
        if tokens:
            g[llm_id] = tokens
        else:
            g.pop(llm_id, None)
        self.log_admin(admin, "set_signup_grant", llm=entry["name"], tokens=tokens)

    def grant_signup_tokens(self, user: dict) -> list[dict]:
        """Free trial tokens, once per *phone number* (so re-registering does not farm grants). Idempotent."""
        phone = normalize_phone(user.get("phone"))
        if not phone or not self.signup_grants():
            return []
        if self.grant_gate and not self.grant_gate(user):  # phone must be verified first (the grant is claimed when it is)
            return []
        if self.db.has_signup_grant(user["id"]):  # one free trial per ACCOUNT (changing the phone number must not earn another)…
            return []
        budget = self.store.settings.get("signup_grants_per_day", 100)  # …and phones are unverified, so bound what fake sign-ups can extract
        if budget and self.db.signup_grants_since(self.clock.now() - 24 * 3600_000) >= budget:
            return []
        out = []
        for llm_id, tokens in self.signup_grants().items():
            entry = self.store.llms.get(llm_id)
            if not entry or not entry["enabled"] or not self.meter.is_metered(entry):
                continue
            e = self.db.credit(user["id"], llm_id, tokens, "GRANT", ref=f"signup:{phone}:{llm_id}", meta={"reason": "signup", "phone": phone})
            if not e.get("duplicate"):
                out.append(e)
        if out:
            self._credited(user["id"], out[0]["llm_id"], f"Welcome! {sum(e['tokens'] for e in out):,} free tokens are in your wallet.")
        return out

    def admin_grant(self, admin: dict, user_id: str, llm_id: str, tokens: int, reason: str = "") -> dict:
        if user_id not in self.store.users:
            raise not_found("USER_NOT_FOUND", "user not found")
        entry = self.llms.get(llm_id)
        e = self.db.credit(user_id, llm_id, tokens, "GRANT", ref=f"admin:{uuid.uuid4()}", meta={"reason": (reason or "admin grant")[:200], "by": admin["name"]})
        self.log_admin(admin, "grant_tokens", user=self.store.users[user_id]["name"], llm=entry["name"], tokens=tokens, reason=reason)
        self._credited(user_id, llm_id, f"{tokens:,} {entry['name']} tokens were added to your wallet.")
        return e

    def admin_adjust(self, admin: dict, user_id: str, llm_id: str, delta: int, reason: str) -> dict:
        if user_id not in self.store.users:
            raise not_found("USER_NOT_FOUND", "user not found")
        if not (reason or "").strip():
            raise bad("REASON_REQUIRED", "a reason is required for balance adjustments")
        self.llms.get(llm_id)
        e = self.db.adjust(user_id, llm_id, delta, ref=f"adjust:{uuid.uuid4()}", meta={"reason": reason.strip()[:200], "by": admin["name"]})
        self.log_admin(admin, "adjust_tokens", user=self.store.users[user_id]["name"], llm_id=llm_id, delta=delta, reason=reason)
        if delta > 0:
            self._credited(user_id, llm_id, None)
        return e

    def _credited(self, user_id: str, llm_id: str, message: str | None) -> None:
        self.meter.reset_low_balance_alert(user_id, llm_id)
        if message:
            agents = sorted((a for a in self.store.agents.values() if a["principal_user_id"] == user_id), key=lambda a: a["created_at"])
            if agents:
                self.notify(agents[0]["agent_id"], "tokens", message, llm_id=llm_id)
        if self.on_credit:
            self.on_credit(user_id, llm_id)

    # ------------------------------------------------------------------ orders

    def get_order(self, order_id: str) -> dict | None:
        r = self.sql.query_one(f"SELECT {ORDER_COLS} FROM orders WHERE id=?", (order_id,))
        return _row(r) if r else None

    def owned_order(self, user_id: str, order_id: str) -> dict:
        o = self.get_order(order_id)
        if not o:
            raise not_found("ORDER_NOT_FOUND", "order not found")
        if o["user_id"] != user_id:
            raise forbidden("NOT_YOUR_ORDER", "that order belongs to another user")
        return o

    def list_orders(self, *, user_id: str | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
        where, args = [], []
        if user_id:
            where.append("user_id=?"); args.append(user_id)
        if status:
            where.append("status=?"); args.append(status)
        sql = f"SELECT {ORDER_COLS} FROM orders" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC LIMIT ?"
        return [_row(r) for r in self.sql.query(sql, (*args, limit))]

    def _insert_order(self, user: dict, pack: dict, provider: str, phone: str | None, status: str = "PENDING") -> dict:
        now = self.clock.now()
        oid = uuid.uuid4().hex
        with self.db.tx() as c:
            open_n = c.scalar("SELECT COUNT(*) FROM orders WHERE user_id=? AND status IN ('PENDING','AWAITING_REVIEW')", (user["id"],), 0)
            if open_n >= self.MAX_OPEN_ORDERS_PER_USER:
                raise AppError("TOO_MANY_ORDERS", f"you already have {open_n} unfinished orders — finish or cancel one first", 429)
            c.execute("INSERT INTO orders(id,user_id,pack_id,pack_name,llm_id,tokens,amount_kes,provider,status,phone,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (oid, user["id"], pack["id"], pack["name"], pack["llm_id"], pack["tokens"], pack["price_kes"], provider, status, phone, now, now))
        return self.get_order(oid)

    def _set_status(self, order_id: str, frm: tuple[str, ...], to: str, **fields) -> bool:
        """Compare-and-swap on the order status; True if this call performed the transition."""
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.db.tx() as c:
            n = c.execute(f"UPDATE orders SET status=?, updated_at=?{', ' + sets if sets else ''} WHERE id=? AND status IN ({','.join('?' * len(frm))})",
                          (to, self.clock.now(), *fields.values(), order_id, *frm))
            if n == 1 and to in ("REJECTED", "FAILED", "CANCELLED", "EXPIRED", "REFUNDED"):
                c.execute("DELETE FROM receipt_claims WHERE order_id=?", (order_id,))  # a dead order frees its M-Pesa code
            return n == 1

    @staticmethod
    def _claim_receipt(c, receipt: str, order_id: str) -> None:
        """'One M-Pesa code, one purchase': claimed while an order is awaiting review or paid. Raises IntegrityError if another order holds it."""
        held = c.query_one("SELECT order_id FROM receipt_claims WHERE receipt=?", (receipt,))
        if held and held["order_id"] != order_id:
            raise IntegrityError(f"receipt {receipt} is already claimed by another order")
        if not held:
            c.execute("INSERT INTO receipt_claims(receipt, order_id) VALUES (?,?)", (receipt, order_id))

    async def checkout(self, user: dict, pack_id: str, method: str, phone: str | None = None) -> dict:
        pack = self.get_pack(pack_id)
        if not pack["enabled"] or not self._sellable(pack):
            raise bad("PACK_UNAVAILABLE", "this pack is not currently on sale")
        if method not in {m["id"] for m in self.methods()}:
            raise bad("METHOD_UNAVAILABLE", f"payment method {method!r} is not available")
        if method == "dev":
            o = self._insert_order(user, pack, "dev", None)
            return self.mark_paid(o["id"], receipt=None, method="dev")
        if method == "manual":
            return self._insert_order(user, pack, "manual", normalize_phone(user.get("phone")))
        # M-Pesa STK push
        msisdn = mpesa_msisdn(phone or user.get("phone"))
        if not msisdn:
            raise bad("INVALID_PHONE", "enter a Safaricom number such as 0712 345 678")
        since = self.clock.now() - 3600_000
        n = self.sql.scalar("SELECT COUNT(*) FROM orders WHERE provider='mpesa' AND phone=? AND created_at>=?", ("+" + msisdn, since), 0)
        if n >= self.STK_PER_PHONE_PER_HOUR:  # nobody can use us to spam a phone with payment prompts
            raise AppError("PHONE_RATE_LIMIT", "too many payment prompts sent to that number in the last hour — try again later", 429)
        o = self._insert_order(user, pack, "mpesa", "+" + msisdn)
        try:
            r = await self.mpesa.stk_push(msisdn, o["amount_kes"], o["reference"], "KenyaBidder")
        except AppError as e:
            self._set_status(o["id"], ("PENDING",), "FAILED", note=e.message[:200])
            raise
        self._set_status(o["id"], ("PENDING",), "PENDING", external_ref=r["checkout_request_id"])
        return self.get_order(o["id"])

    def submit_receipt(self, user: dict, order_id: str, receipt: str) -> dict:
        o = self.owned_order(user["id"], order_id)
        if o["provider"] != "manual" or o["status"] not in ("PENDING", "EXPIRED"):  # a slow customer who paid the Till is still credited
            raise bad("INVALID_STATE", "this order is not waiting for a payment code")
        code = re.sub(r"\s", "", receipt or "").upper()
        if not RECEIPT_RE.match(code):
            raise bad("INVALID_RECEIPT", "enter the M-Pesa confirmation code from your SMS (8–14 letters and digits, e.g. SGH7X2K9LP)")
        try:
            with self.db.tx() as c:
                self._claim_receipt(c, code, order_id)
                ok = self._set_status(order_id, ("PENDING", "EXPIRED"), "AWAITING_REVIEW", receipt=code)
                if not ok:
                    raise bad("INVALID_STATE", "this order is not waiting for a payment code")
        except IntegrityError:
            raise conflict("DUPLICATE_RECEIPT", "that payment code has already been submitted") from None
        return self.get_order(order_id)

    def cancel_order(self, user: dict, order_id: str) -> dict:
        self.owned_order(user["id"], order_id)
        if not self._set_status(order_id, ("PENDING",), "CANCELLED"):
            raise bad("INVALID_STATE", "only unpaid orders can be cancelled")
        return self.get_order(order_id)

    def admin_review(self, admin: dict, order_id: str, approve: bool, note: str = "") -> dict:
        o = self.get_order(order_id)
        if not o:
            raise not_found("ORDER_NOT_FOUND", "order not found")
        if o["status"] != "AWAITING_REVIEW":
            raise bad("INVALID_STATE", f"order is {o['status']}, not awaiting review")
        if approve:
            r = self.mark_paid(order_id, receipt=None, method=f"manual:{admin['name']}")
        else:
            if not self._set_status(order_id, ("AWAITING_REVIEW",), "REJECTED", note=(note or "payment could not be verified")[:200]):
                raise bad("INVALID_STATE", "order was already reviewed")
            r = self.get_order(order_id)
        self.log_admin(admin, "approve_order" if approve else "reject_order", order=o["reference"], user=self.store.users.get(o["user_id"], {}).get("name"),
                       amount_kes=o["amount_kes"], receipt=o["receipt"], note=note)
        return r

    def mark_paid(self, order_id: str, *, receipt: str | None, method: str) -> dict:
        """Atomically: PENDING/AWAITING_REVIEW → PAID **and** credit the tokens. Safe to call any number of times."""
        with self.db.tx() as c:
            row = c.query_one(f"SELECT {ORDER_COLS} FROM orders WHERE id=?", (order_id,))
            if not row:
                raise not_found("ORDER_NOT_FOUND", "order not found")
            o = _row(row)
            if o["status"] == "PAID":
                return {**o, "already_paid": True}
            recoverable = ("PENDING", "AWAITING_REVIEW") + (("EXPIRED", "CANCELLED") if o["provider"] == "mpesa" else ())  # a late STK approval still counts
            if o["status"] not in recoverable:
                raise conflict("INVALID_STATE", f"order is {o['status']} and cannot be marked paid")
            final_receipt = receipt or o["receipt"]
            if final_receipt:
                self._claim_receipt(c, final_receipt, order_id)  # IntegrityError if another order already holds this code
            n = c.execute("UPDATE orders SET status='PAID', updated_at=?, receipt=?, note=? WHERE id=? AND status=?",
                          (self.clock.now(), final_receipt, f"paid via {method}", order_id, o["status"]))
            if n != 1:
                raise conflict("RACE", "order changed concurrently")
            self.db.credit(o["user_id"], o["llm_id"], o["tokens"], "TOPUP", ref=f"order:{order_id}",
                           meta={"order": o["reference"], "amount_kes": o["amount_kes"], "method": method, "receipt": final_receipt}, c=c)
        paid = self.get_order(order_id)
        name = self.store.llms.get(paid["llm_id"], {}).get("name", "LLM")
        self._credited(paid["user_id"], paid["llm_id"], f"Payment received — {paid['tokens']:,} {name} tokens added to your wallet.")
        return paid

    def admin_refund(self, admin: dict, order_id: str, reason: str) -> dict:
        """Mark a paid order refunded (the money goes back off-platform) and reclaim whatever tokens are still unspent."""
        o = self.get_order(order_id)
        if not o or o["status"] != "PAID":
            raise bad("INVALID_STATE", "only paid orders can be refunded")
        if not (reason or "").strip():
            raise bad("REASON_REQUIRED", "a reason is required")
        reclaim = min(o["tokens"], self.db.balance(o["user_id"], o["llm_id"]))
        with self.db.tx() as c:
            if not c.execute("UPDATE orders SET status='REFUNDED', updated_at=?, note=? WHERE id=? AND status='PAID'",
                             (self.clock.now(), f"refunded: {reason.strip()[:150]} (reclaimed {reclaim:,} unspent tokens)", order_id)):
                raise bad("INVALID_STATE", "order is no longer refundable")
            c.execute("DELETE FROM receipt_claims WHERE order_id=?", (order_id,))
            if reclaim:
                self.db._write(c, user_id=o["user_id"], llm_id=o["llm_id"], kind="ADJUST", delta=-reclaim, ref=f"refund:{order_id}",
                               meta={"reason": f"refund of {o['reference']}", "by": admin["name"]})
        self.log_admin(admin, "refund_order", order=o["reference"], amount_kes=o["amount_kes"], reclaimed=reclaim, reason=reason)
        return self.get_order(order_id)

    # ------------------------------------------------------------------ M-Pesa callbacks & reconciliation

    async def handle_mpesa_callback(self, secret: str, payload: dict) -> dict:
        """Daraja posts here after the customer answers the prompt. The body only *triggers* a status query —
        an attacker who forged it (or replayed it) cannot mint tokens: only Safaricom's authenticated answer can."""
        if not self.mpesa:
            raise AppError("MPESA_DISABLED", "M-Pesa is not configured", 503)
        if not hmac.compare_digest(str(secret).encode(), self.mpesa.cfg.callback_secret.encode()):
            raise forbidden("BAD_CALLBACK_SECRET", "invalid callback path")
        ack = {"ResultCode": 0, "ResultDesc": "Accepted"}
        try:
            checkout_id = payload["Body"]["stkCallback"]["CheckoutRequestID"]
        except (KeyError, TypeError):
            return ack
        r = self.sql.query_one(f"SELECT {ORDER_COLS} FROM orders WHERE external_ref=? AND provider='mpesa'", (checkout_id,))
        if r:
            try:
                await self._confirm(_row(r), callback_receipt(payload))
            except AppError:
                pass  # left pending; reconcile() retries
        return ack

    async def _confirm(self, o: dict, receipt_hint: str | None) -> dict:
        if o["status"] == "PAID":
            return o
        q = await self.mpesa.stk_query(o["external_ref"])
        if q["state"] == "PAID":
            try:
                return self.mark_paid(o["id"], receipt=receipt_hint, method="mpesa")
            except IntegrityError:
                # Safaricom says paid but the receipt code is already on another order: don't guess — a human decides
                self._set_status(o["id"], ("PENDING", "EXPIRED", "CANCELLED"), "AWAITING_REVIEW", note="M-Pesa confirmed payment but the receipt code is already used")
                return self.get_order(o["id"])
        if q["state"] == "FAILED":
            # final answer from Safaricom: also retire EXPIRED/CANCELLED orders so reconcile() stops re-querying them
            self._set_status(o["id"], ("PENDING", "EXPIRED", "CANCELLED"), "FAILED", note=(q["desc"] or "payment was not completed")[:200])
        return self.get_order(o["id"])

    async def check_order(self, user: dict, order_id: str) -> dict:
        """User pressed 'I have paid / check status'."""
        o = self.owned_order(user["id"], order_id)
        if o["provider"] != "mpesa" or not o["external_ref"] or o["status"] not in ("PENDING", "EXPIRED", "CANCELLED"):
            return o
        return await self._confirm(o, None)

    async def reconcile(self, limit: int = 20) -> int:
        """Safety net for lost callbacks: re-query recent unpaid STK orders. Returns how many were settled."""
        if not self.mpesa:
            return 0
        now = self.clock.now()
        rows = self.sql.query(f"SELECT {ORDER_COLS} FROM orders WHERE provider='mpesa' AND external_ref IS NOT NULL AND status IN ('PENDING','EXPIRED') "
                              "AND created_at<? AND created_at>? ORDER BY created_at LIMIT ?", (now - 20_000, now - 3 * 3600_000, limit))
        settled = 0
        for r in rows:
            try:
                if (await self._confirm(_row(r), None))["status"] == "PAID":
                    settled += 1
            except AppError:
                continue
        return settled

    MANUAL_ORDER_TTL_MS = 7 * 24 * 3600_000

    def expire_stale(self) -> int:
        """STK prompts lapse after 30 min. Manual orders wait a week: people pay the Till first and type the code later."""
        now = self.clock.now()
        with self.db.tx() as c:
            return c.execute("UPDATE orders SET status='EXPIRED', updated_at=? WHERE status='PENDING' AND "
                             "((provider!='manual' AND created_at<?) OR (provider='manual' AND created_at<?))",
                             (now, now - self.ORDER_TTL_MS, now - self.MANUAL_ORDER_TTL_MS))

    # ------------------------------------------------------------------ reporting

    def revenue_report(self, since_ms: int = 0) -> dict:
        paid = self.sql.query("SELECT llm_id, COUNT(*) AS n, SUM(amount_kes) AS kes, SUM(tokens) AS tok FROM orders WHERE status='PAID' AND updated_at>=? GROUP BY llm_id", (since_ms,))
        pending = self.sql.scalar("SELECT COUNT(*) FROM orders WHERE status='AWAITING_REVIEW'", (), 0)
        totals, liability = self.db.token_totals(since_ms), self.db.outstanding_liability()
        by = {r["llm_id"]: {**r, "n": int(r["n"]), "kes": int(r["kes"] or 0)} for r in paid}
        rows, revenue, cost = [], 0, 0.0
        for llm_id in set(by) | set(totals) | set(liability):
            e = self.store.llms.get(llm_id, {"name": f"(deleted {llm_id[:6]})"})
            t = totals.get(llm_id, {})
            kes = by.get(llm_id, {}).get("kes") or 0
            c = (t.get("used", 0) / 1000) * billing_of(e)["cost_per_1k_kes"] if llm_id in self.store.llms else 0.0
            revenue += kes
            cost += c
            rows.append({"llm_id": llm_id, "llm": e["name"], "orders": by.get(llm_id, {}).get("n", 0), "revenue_kes": kes, "tokens_sold": t.get("sold", 0),
                         "tokens_granted": t.get("granted", 0), "tokens_used": t.get("used", 0), "est_cost_kes": round(c, 2),
                         "margin_kes": round(kes - c, 2), "tokens_outstanding": liability.get(llm_id, 0)})
        rows.sort(key=lambda r: -r["revenue_kes"])
        return {"revenue_kes": revenue, "est_cost_kes": round(cost, 2), "margin_kes": round(revenue - cost, 2),
                "margin_pct": round((revenue - cost) / revenue * 100, 1) if revenue else None, "awaiting_review": pending, "by_llm": rows}

    def ledger_csv(self, *, user_id: str | None = None) -> str:
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["time_utc_ms", "user", "llm", "agent", "kind", "tokens", "used", "balance_after", "ref", "note"])
        for e in reversed(self.db.ledger(user_id=user_id, limit=100_000)):
            u = self.store.users.get(e["user_id"], {}).get("name", e["user_id"][:8])
            l = self.store.llms.get(e["llm_id"], {}).get("name", e["llm_id"][:8])
            w.writerow([e["at"], csv_safe(u), csv_safe(l), (e["agent_id"] or "")[:8], e["kind"], e["tokens"], e["used"], e["balance_after"],
                        csv_safe(e["ref"]), csv_safe(e["meta"].get("reason") or e["meta"].get("purpose") or "")])
        return out.getvalue()

    def orders_csv(self, *, user_id: str | None = None) -> str:
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["reference", "created_ms", "user", "pack", "llm", "tokens", "amount_kes", "provider", "status", "receipt", "note"])
        for o in reversed(self.list_orders(user_id=user_id, limit=100_000)):
            w.writerow([o["reference"], o["created_at"], csv_safe(self.store.users.get(o["user_id"], {}).get("name", "")), csv_safe(o["pack_name"]),
                        csv_safe(self.store.llms.get(o["llm_id"], {}).get("name", "")), o["tokens"], o["amount_kes"], o["provider"], o["status"],
                        csv_safe(o["receipt"]), csv_safe(o["note"])])
        return out.getvalue()
