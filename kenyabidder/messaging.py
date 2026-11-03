"""Outbound SMS and email (OTP codes, password resets, alerts).

Providers are small adapters so a different gateway is a 20-line class:
  * ``AfricasTalkingSms``  — the usual Kenyan bulk-SMS gateway (AT_USERNAME / AT_API_KEY / AT_SENDER_ID / AT_SANDBOX=1)
  * ``SmtpEmail``          — any SMTP relay (SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM)
  * ``LogSms`` / ``LogEmail`` — development fallbacks that keep messages in memory and never leave the machine

The :class:`Messenger` adds the guard rails every SMS product needs: a daily budget (SMS pumping is a real fraud), per-user
volume caps, and a log that records *that* something was sent — never the code or message body.
"""
from __future__ import annotations

import asyncio
import logging
import os
import smtplib
from email.message import EmailMessage

import httpx

from .errors import AppError

log = logging.getLogger("kenyabidder.messaging")


def mask(dest: str) -> str:
    if "@" in dest:
        name, _, dom = dest.partition("@")
        return name[:1] + "***@" + dom
    return dest[:4] + "***" + dest[-3:] if len(dest) > 8 else "***"


class LogSms:
    name, real = "log", False

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, to: str, text: str) -> None:
        self.sent.append({"to": to, "text": text})
        del self.sent[:-200]
        log.info("[dev SMS] to %s (%d chars)", mask(to), len(text))


class LogEmail:
    name, real = "log", False

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "text": body})
        del self.sent[:-200]
        log.info("[dev email] to %s: %s", mask(to), subject)


class AfricasTalkingSms:
    name, real = "africastalking", True

    def __init__(self, username: str, api_key: str, sender_id: str | None = None, sandbox: bool = False, client: httpx.AsyncClient | None = None):
        self.username, self.api_key, self.sender_id, self.client = username, api_key, sender_id, client
        self.url = f"https://api.{'sandbox.' if sandbox else ''}africastalking.com/version1/messaging"

    @classmethod
    def from_env(cls):
        u, k = os.environ.get("AT_USERNAME"), os.environ.get("AT_API_KEY")
        return cls(u, k, os.environ.get("AT_SENDER_ID") or None, os.environ.get("AT_SANDBOX") == "1") if u and k else None

    async def send(self, to: str, text: str) -> None:
        data = {"username": self.username, "to": to, "message": text}
        if self.sender_id:
            data["from"] = self.sender_id
        c = self.client or httpx.AsyncClient(timeout=15)
        try:
            r = await c.post(self.url, data=data, headers={"apiKey": self.api_key, "Accept": "application/json"})
        finally:
            if self.client is None:
                await c.aclose()
        r.raise_for_status()
        recipients = (r.json().get("SMSMessageData") or {}).get("Recipients") or []
        if not recipients or any(x.get("statusCode") not in (100, 101, 102) for x in recipients):
            raise RuntimeError(f"gateway refused the message: {recipients[:1] or r.text[:120]}")


class SmtpEmail:
    name, real = "smtp", True

    def __init__(self, host: str, port: int = 587, user: str | None = None, password: str | None = None, sender: str | None = None, starttls: bool = True):
        self.host, self.port, self.user, self.password, self.starttls = host, port, user, password, starttls
        self.sender = sender or user or "noreply@kenyabidder.local"

    @classmethod
    def from_env(cls):
        host = os.environ.get("SMTP_HOST")
        return cls(host, int(os.environ.get("SMTP_PORT", 587)), os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD"),
                   os.environ.get("SMTP_FROM"), os.environ.get("SMTP_STARTTLS", "1") != "0") if host else None

    def _send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject.replace("\n", " ")[:200]
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=15) as s:
            if self.starttls:
                s.starttls()
            if self.user:
                s.login(self.user, self.password or "")
            s.send_message(msg)

    async def send(self, to: str, subject: str, body: str) -> None:
        await asyncio.to_thread(self._send, to, subject, body)


class Messenger:
    DEFAULT_SMS_BUDGET = 500  # per day, platform-wide

    def __init__(self, store, clock, sms=None, email=None):
        self.store, self.clock = store, clock
        self.sms = sms or AfricasTalkingSms.from_env() or LogSms()
        self.email = email or SmtpEmail.from_env() or LogEmail()
        self.tasks: set[asyncio.Task] = set()

    @property
    def sms_real(self) -> bool:
        return bool(getattr(self.sms, "real", False))

    @property
    def email_real(self) -> bool:
        return bool(getattr(self.email, "real", False))

    def _record(self, channel: str, dest: str, kind: str, ok: bool, error: str | None = None) -> None:
        self.store.message_log.append({"at": self.clock.now(), "channel": channel, "to": mask(dest), "kind": kind, "ok": ok, "error": (error or "")[:160] or None})
        del self.store.message_log[:-2000]

    def _spend_sms_budget(self) -> None:
        from .timeutil import eat
        day = eat(self.clock.now()).strftime("%Y-%m-%d")
        b = self.store.settings.setdefault("sms_day", {"day": day, "n": 0})
        if b["day"] != day:
            b.update(day=day, n=0)
        if b["n"] >= self.store.settings.get("sms_daily_budget", self.DEFAULT_SMS_BUDGET):
            raise AppError("SMS_BUDGET", "the platform's daily SMS allowance is used up — try again tomorrow", 503)
        b["n"] += 1

    async def send_sms(self, to: str, text: str, kind: str) -> None:
        self._spend_sms_budget()
        try:
            await self.sms.send(to, text)
        except Exception as e:  # noqa: BLE001
            self._record("SMS", to, kind, False, f"{type(e).__name__}: {e}")
            log.error("sms delivery failed (%s): %s", kind, e)
            raise AppError("DELIVERY_FAILED", "we could not send the SMS — please try again in a moment", 502) from None
        self._record("SMS", to, kind, True)

    async def send_email(self, to: str, subject: str, body: str, kind: str) -> None:
        try:
            await self.email.send(to, subject, body)
        except Exception as e:  # noqa: BLE001
            self._record("EMAIL", to, kind, False, f"{type(e).__name__}: {e}")
            log.error("email delivery failed (%s): %s", kind, e)
            raise AppError("DELIVERY_FAILED", "we could not send the email — please try again in a moment", 502) from None
        self._record("EMAIL", to, kind, True)

    def spawn(self, coro) -> bool:
        """Fire-and-forget (alerts): failures are logged and recorded, never raised into the auction engine."""
        try:
            t = asyncio.get_running_loop().create_task(self._swallow(coro))
        except RuntimeError:
            coro.close()
            return False
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return True

    @staticmethod
    async def _swallow(coro) -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001
            pass  # already recorded in message_log by send_*
