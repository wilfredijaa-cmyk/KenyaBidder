"""M-Pesa (Safaricom Daraja) STK Push client.

Implemented from the public Daraja API documentation (OAuth → ``/mpesa/stkpush/v1/processrequest`` →
``/mpesa/stkpushquery/v1/query``). It is exercised in tests against a mocked Safaricom API; verify it against the
Daraja *sandbox* with your own credentials before going live.

Money-safety rule used by the billing service: the callback body is **never trusted to credit an order** — it only
triggers an ``stk_query`` against Safaricom, and only that authenticated answer can mark an order paid.
"""
from __future__ import annotations

import base64
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from .errors import AppError
from .timeutil import EAT

BASES = {"sandbox": "https://sandbox.safaricom.co.ke", "production": "https://api.safaricom.co.ke"}
RECEIPT_RE = re.compile(r"^[A-Z0-9]{8,14}$")


@dataclass
class MpesaConfig:
    consumer_key: str
    consumer_secret: str
    shortcode: str
    passkey: str
    callback_base_url: str  # public https origin Safaricom can reach, e.g. https://kenyabidder.example.com
    env: str = "sandbox"
    transaction_type: str = "CustomerPayBillOnline"  # or CustomerBuyGoodsOnline for a Till
    party_b: str | None = None  # the Till number for BuyGoods; defaults to the shortcode
    callback_secret: str = field(default_factory=lambda: secrets.token_urlsafe(24))

    @classmethod
    def from_env(cls, fallback_secret: str | None = None) -> "MpesaConfig | None":
        e = os.environ
        need = ("MPESA_CONSUMER_KEY", "MPESA_CONSUMER_SECRET", "MPESA_SHORTCODE", "MPESA_PASSKEY", "MPESA_CALLBACK_BASE_URL")
        if not all(e.get(k) for k in need):
            return None
        env = e.get("MPESA_ENV", "sandbox")
        if env not in BASES:
            raise ValueError("MPESA_ENV must be 'sandbox' or 'production'")
        return cls(e["MPESA_CONSUMER_KEY"], e["MPESA_CONSUMER_SECRET"], e["MPESA_SHORTCODE"], e["MPESA_PASSKEY"],
                   e["MPESA_CALLBACK_BASE_URL"].rstrip("/"), env, e.get("MPESA_TRANSACTION_TYPE", "CustomerPayBillOnline"),
                   e.get("MPESA_PARTY_B") or None, e.get("MPESA_CALLBACK_SECRET") or fallback_secret or secrets.token_urlsafe(24))


class MpesaClient:
    def __init__(self, cfg: MpesaConfig, http: httpx.AsyncClient | None = None, clock=None):
        self.cfg, self.http, self.clock = cfg, http, clock
        self._token: tuple[str, float] | None = None

    @property
    def base(self) -> str:
        return BASES[self.cfg.env]

    @property
    def callback_url(self) -> str:
        return f"{self.cfg.callback_base_url}/webhooks/mpesa/{self.cfg.callback_secret}"

    def _now_s(self) -> float:
        import time
        return self.clock.now() / 1000 if self.clock else time.time()

    async def _request(self, method: str, path: str, **kw) -> dict:
        client = self.http or httpx.AsyncClient(timeout=20)
        try:
            r = await client.request(method, f"{self.base}{path}", **kw)
        except httpx.HTTPError as e:
            raise AppError("MPESA_UNREACHABLE", f"could not reach M-Pesa: {type(e).__name__}", 502) from e
        finally:
            if self.http is None:
                await client.aclose()
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            data.setdefault("_http", r.status_code)
        return data

    async def token(self) -> str:
        if self._token and self._token[1] > self._now_s() + 60:
            return self._token[0]
        cred = base64.b64encode(f"{self.cfg.consumer_key}:{self.cfg.consumer_secret}".encode()).decode()
        data = await self._request("GET", "/oauth/v1/generate", params={"grant_type": "client_credentials"}, headers={"Authorization": f"Basic {cred}"})
        tok = data.get("access_token")
        if not tok:
            raise AppError("MPESA_AUTH", "M-Pesa rejected the configured credentials", 502)
        self._token = (tok, self._now_s() + int(data.get("expires_in", 3599)))
        return tok

    def _password(self, ts: str) -> str:
        return base64.b64encode(f"{self.cfg.shortcode}{self.cfg.passkey}{ts}".encode()).decode()

    def _ts(self) -> str:
        return datetime.fromtimestamp(self._now_s(), EAT).strftime("%Y%m%d%H%M%S")

    async def stk_push(self, msisdn: str, amount: int, account_ref: str, description: str) -> dict:
        ts = self._ts()
        body = {"BusinessShortCode": self.cfg.shortcode, "Password": self._password(ts), "Timestamp": ts,
                "TransactionType": self.cfg.transaction_type, "Amount": int(amount), "PartyA": msisdn,
                "PartyB": self.cfg.party_b or self.cfg.shortcode, "PhoneNumber": msisdn, "CallBackURL": self.callback_url,
                "AccountReference": account_ref[:12], "TransactionDesc": description[:13]}
        headers = {"Authorization": f"Bearer {await self.token()}"}  # a failure up to here means nothing was sent
        try:
            data = await self._request("POST", "/mpesa/stkpush/v1/processrequest", json=body, headers=headers)
        except AppError as e:
            e.ambiguous = e.code == "MPESA_UNREACHABLE"  # a timeout AFTER the request left: Safaricom may already have prompted the customer
            raise
        if str(data.get("ResponseCode")) != "0" or not data.get("CheckoutRequestID"):
            msg = data.get("errorMessage") or data.get("ResponseDescription") or "M-Pesa did not accept the request"
            err = AppError("MPESA_REJECTED", f"M-Pesa: {msg}", 502)
            err.ambiguous = int(data.get("_http") or 0) >= 500  # a 5xx says nothing about whether the prompt went out
            raise err
        return {"checkout_request_id": data["CheckoutRequestID"], "merchant_request_id": data.get("MerchantRequestID"),
                "customer_message": data.get("CustomerMessage", "")}

    async def stk_query(self, checkout_request_id: str) -> dict:
        """Authoritative status of one STK push: ``{"state": PAID|FAILED|PENDING, "code", "desc"}``."""
        ts = self._ts()
        data = await self._request("POST", "/mpesa/stkpushquery/v1/query", headers={"Authorization": f"Bearer {await self.token()}"},
                                   json={"BusinessShortCode": self.cfg.shortcode, "Password": self._password(ts), "Timestamp": ts,
                                         "CheckoutRequestID": checkout_request_id})
        if "ResultCode" in data:
            code = str(data["ResultCode"])
            return {"state": "PAID" if code == "0" else "FAILED", "code": code, "desc": data.get("ResultDesc", "")}
        err = str(data.get("errorCode", ""))
        if err.endswith("1001") or "being processed" in str(data.get("errorMessage", "")).lower():
            return {"state": "PENDING", "code": err, "desc": "the customer has not completed the payment yet"}
        raise AppError("MPESA_QUERY", f"could not determine payment status: {data.get('errorMessage') or data}", 502)


def callback_receipt(payload: dict) -> str | None:
    """Advisory receipt number from a Daraja callback body (validated, and never used to authorise a credit)."""
    try:
        items = payload["Body"]["stkCallback"]["CallbackMetadata"]["Item"]
        for it in items:
            if it.get("Name") == "MpesaReceiptNumber":
                v = str(it.get("Value", "")).upper()
                return v if RECEIPT_RE.match(v) else None
    except (KeyError, TypeError):
        pass
    return None
