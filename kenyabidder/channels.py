"""Channel Router (spec §7.3): every channel talks to the *agent*, never to a channel session."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import uuid

import httpx

from .errors import AppError, bad

log = logging.getLogger("kenyabidder.channels")


class WhatsAppAdapter:
    """Always records to the outbox; sends via the WhatsApp Cloud API when credentials are configured."""

    def __init__(self, store, clock, token: str | None = None, phone_number_id: str | None = None, client: httpx.AsyncClient | None = None):
        import os
        self.store, self.clock = store, clock
        self.token = token or os.environ.get("WHATSAPP_TOKEN")
        self.phone_number_id = phone_number_id or os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
        self.client = client

    def send_nowait(self, to: str, text: str) -> None:
        self.store.outbox.append({"channel": "WHATSAPP", "to": to, "text": text, "at": self.clock.now()})
        del self.store.outbox[:-1000]
        if self.token and self.phone_number_id:
            import asyncio
            try:
                asyncio.get_running_loop().create_task(self._post(to, text))
            except RuntimeError:
                pass

    async def _post(self, to: str, text: str) -> None:
        try:
            c = self.client or httpx.AsyncClient(timeout=10)
            try:
                await c.post(f"https://graph.facebook.com/v20.0/{self.phone_number_id}/messages",
                             headers={"authorization": f"Bearer {self.token}"},
                             json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}})
            finally:
                if self.client is None:
                    await c.aclose()
        except Exception:  # noqa: BLE001
            log.exception("whatsapp send failed")


ALERT_KINDS = {"approval_needed", "match", "outbid", "no_tokens", "low_tokens", "anomaly", "rfq", "dispute", "verification"}
SMS_ALERTS_PER_DAY, EMAIL_ALERTS_PER_DAY = 15, 40  # per user: a runaway agent must not turn into an SMS bill


class ChannelRouter:
    def __init__(self, store, clock, whatsapp: WhatsAppAdapter, messenger=None):
        self.store, self.clock, self.whatsapp, self.messenger = store, clock, whatsapp, messenger
        self.listeners: list = []  # web push subscribers: fn(notification)
        self.handlers = None

    def bind(self, handlers) -> None:
        self.handlers = handlers

    def notify(self, agent_id: str, kind: str, message: str, **data) -> dict:
        n = {"id": str(uuid.uuid4()), "agent_id": agent_id, "kind": kind, "message": message, "data": data, "at": self.clock.now()}
        self.store.notifications.append(n)
        if len(self.store.notifications) > 5000:
            del self.store.notifications[:1000]
        for fn in list(self.listeners):
            try:
                fn(n)
            except Exception:  # noqa: BLE001  a broken web client must never break execution
                pass
        agent = self.store.agents.get(agent_id)
        if agent and agent["durable_memory"].get("preferred_channel") == "WHATSAPP":
            link = next((c for c in agent["channel_identity_map"] if c["channel"] == "WHATSAPP"), None)
            if link:
                self.whatsapp.send_nowait(link["external_id"], message)
        if agent:
            self._alert(agent, kind, message)
        return n

    def _alert(self, agent: dict, kind: str, message: str) -> None:
        """SMS / email alerts for the events that need a human, only to VERIFIED contacts and within a per-user daily cap."""
        pref = agent["durable_memory"].get("preferred_channel")
        if pref not in ("SMS", "EMAIL") or kind not in ALERT_KINDS or not self.messenger:
            return
        u = self.store.users.get(agent["principal_user_id"])
        if not u or u.get("suspended"):
            return
        from .timeutil import eat
        day = eat(self.clock.now()).strftime("%Y-%m-%d")
        used = u.setdefault("alerts_sent", {"day": day, "SMS": 0, "EMAIL": 0})
        if used["day"] != day:
            used.update(day=day, SMS=0, EMAIL=0)
        if pref == "SMS" and u.get("phone") and u.get("phone_verified") and used["SMS"] < SMS_ALERTS_PER_DAY:
            used["SMS"] += 1
            self.messenger.spawn(self.messenger.send_sms(u["phone"], f"KenyaBidder: {message}"[:300], "alert"))
        elif pref == "EMAIL" and u.get("email") and u.get("email_verified") and used["EMAIL"] < EMAIL_ALERTS_PER_DAY:
            used["EMAIL"] += 1
            self.messenger.spawn(self.messenger.send_email(u["email"], f"KenyaBidder: {kind.replace('_', ' ')}", message, "alert"))

    def notifications_for(self, agent_id: str, limit: int = 50) -> list[dict]:
        out = []
        for n in reversed(self.store.notifications):
            if n["agent_id"] == agent_id:
                out.append(n)
                if len(out) >= limit:
                    break
        return out

    async def handle_inbound(self, channel: str, external_id: str, text: str) -> dict:
        """Resolve identity through the channel map to the canonical agent, then run the command."""
        agent = self.handlers.find_by_channel(channel, external_id)
        if not agent:
            return {"agent_id": None, "reply": "This number is not linked to a KenyaBidder agent. Link it from the web app first."}
        if self.handlers.owner_suspended(agent):
            return {"agent_id": agent["agent_id"], "reply": "This account is suspended. Please contact support."}
        agent["durable_memory"]["last_channel"] = channel
        return {"agent_id": agent["agent_id"], "reply": await self.converse(agent, channel, text)}

    async def converse(self, agent: dict, channel: str, text: str) -> str:
        conv = agent["durable_memory"]["conversation"]
        conv.append({"role": "user", "channel": channel, "text": str(text)[:500], "at": self.clock.now()})
        try:
            reply = await self.command(agent, str(text).strip())
        except AppError as e:
            reply = f"Sorry, that failed: {e.message}"
        except Exception as e:  # noqa: BLE001
            log.exception("command failed")
            reply = f"Sorry, that failed: {e}"
        conv.append({"role": "agent", "channel": channel, "text": reply, "at": self.clock.now()})
        del conv[:-100]
        return reply

    async def command(self, agent: dict, text: str) -> str:
        h = self.handlers
        parts = text.split()
        cmd, arg = (parts[0].lower() if parts else ""), " ".join(parts[1:])
        if cmd == "status":
            return h.status(agent)
        if cmd == "pause":
            h.set_status(agent["agent_id"], "PAUSED")
            return "Agent paused. All pending bid triggers were cancelled."
        if cmd == "resume":
            h.set_status(agent["agent_id"], "ACTIVE")
            return "Agent resumed."
        if cmd == "ceiling":
            digits = arg.replace(",", "").replace(" ", "")
            if not digits.isdigit() or int(digits) <= 0:
                raise bad("INVALID_CEILING", "usage: ceiling <positive whole amount>")
            h.update(agent["agent_id"], constraints={"budget_ceiling": int(digits)})
            return f"Budget ceiling is now {int(digits)} KES."
        if cmd in ("approve", "reject"):
            ap = h.find_approval(agent["agent_id"], arg) if arg else None
            if not ap:
                return "No pending approval matches that id."
            await h.resolve_approval(ap["id"], cmd == "approve")
            return "Approved." if cmd == "approve" else "Rejected."
        if cmd == "summary":
            return h.seller_summary(agent["agent_id"]) if agent["agent_type"] == "SELLER" else h.status(agent)
        return "Commands: status, pause, resume, ceiling <amount>, approve <id>, reject <id>, summary, help"


async def handle_whatsapp_webhook(router: ChannelRouter, raw_body: bytes, signature: str | None, *, secret: str | None = None,
                                  allow_unsigned: bool | None = None) -> dict:
    """Process an inbound WhatsApp Cloud API webhook.

    The endpoint is public, and a message "from" a linked number can approve bids — so the payload MUST be
    authenticated with Meta's ``X-Hub-Signature-256`` (HMAC-SHA256 of the raw body with the app secret).
    Without ``WHATSAPP_APP_SECRET`` the webhook is refused unless ``KENYABIDDER_INSECURE_WEBHOOK=1`` (local dev only).
    """
    secret = secret if secret is not None else os.environ.get("WHATSAPP_APP_SECRET")
    if allow_unsigned is None:
        allow_unsigned = os.environ.get("KENYABIDDER_INSECURE_WEBHOOK") == "1"
    if secret:
        expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(signature.encode(), expected.encode()):  # bytes: a non-ASCII header must be a 403, not a crash
            raise AppError("BAD_SIGNATURE", "invalid webhook signature", 403)
    elif not allow_unsigned:
        raise AppError("WEBHOOK_NOT_CONFIGURED", "set WHATSAPP_APP_SECRET to accept WhatsApp webhooks", 403)
    try:
        body = json.loads(raw_body)
    except ValueError:
        raise AppError("INVALID_JSON", "body is not valid JSON", 400) from None
    messages = _whatsapp_messages(body)
    if not messages:
        return {"ok": True, "ignored": True}
    seen = router.store.settings.setdefault("wa_seen", [])  # replay protection: Meta retries, and a captured valid request could be resent
    fresh = []
    for mid, sender, text in messages:
        if mid and mid in seen:
            continue
        if mid:
            seen.append(mid)
        fresh.append((sender, text))
    del seen[:-2000]
    if not fresh:
        return {"ok": True, "duplicate": True}
    messages = fresh
    reply = None
    for sender, text in messages:  # Meta batches deliveries: EVERY message must run (an 'approve' may not be the first)
        out = await router.handle_inbound("WHATSAPP", str(sender), text)
        reply = out["reply"]
        if out["agent_id"]:
            router.whatsapp.send_nowait(str(sender), out["reply"])
    return {"ok": True, "handled": len(messages), "reply": reply}


def _whatsapp_messages(body) -> list[tuple[str | None, str, str]]:
    """(message id, sender, text) from a Cloud API payload (all entries / changes / messages) or the simple {from, text} dev shape."""
    out: list[tuple[str | None, str, str]] = []
    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                for m in change.get("value", {}).get("messages", []):
                    text = (m.get("text") or {}).get("body")
                    if m.get("from") and isinstance(text, str):
                        out.append((str(m["id"])[:128] if m.get("id") else None, m["from"], text))
    except (AttributeError, TypeError):
        return []
    if not out and isinstance(body, dict) and body.get("from") and isinstance(body.get("text"), str):
        out.append((None, body["from"], body["text"]))
    return out
