"""Web-layer hardening: security headers, per-IP rate limits, body-size limits, connection caps, trusted-proxy aware client IPs,
password policy and secret redaction. Everything here is deterministic and dependency-free.

Threat model summary (details in SECURITY.md): the app is reachable from the open internet behind a TLS reverse proxy; users are
mutually distrusting businesses; the platform never holds funds but holds token balances, contact details and API keys.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import time
from collections import defaultdict, deque

from .errors import AppError, bad

# ------------------------------------------------------------------ client IP

def _networks(spec: str | None) -> list:
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if part:
            try:
                out.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                raise ValueError(f"KENYABIDDER_TRUSTED_PROXIES: bad CIDR {part!r}") from None
    return out


class ClientIP:
    """Resolve the real client address. ``X-Forwarded-For`` is honoured ONLY when the direct peer is a configured trusted proxy
    (otherwise any client could forge it and dodge every per-IP limit); the right-most untrusted hop wins."""

    def __init__(self, trusted: str | None = None):
        self.trusted = _networks(trusted if trusted is not None else os.environ.get("KENYABIDDER_TRUSTED_PROXIES"))

    def _is_trusted(self, ip: str) -> bool:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(a in n for n in self.trusted)

    def resolve(self, peer: str | None, xff: str | None) -> str:
        peer = peer or "0.0.0.0"  # nosec B104
        if not self.trusted or not self._is_trusted(peer) or not xff:
            return peer
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        for hop in reversed(hops):
            try:
                ipaddress.ip_address(hop)
            except ValueError:
                return peer  # garbage in the header: do not trust any of it
            if not self._is_trusted(hop):
                return hop
        return peer

    def from_scope(self, scope: dict) -> str:
        client = scope.get("client")
        # a proxy may append a SEPARATE X-Forwarded-For line instead of extending one: join them all, in order
        xff = ", ".join(v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"x-forwarded-for") or None
        return self.resolve(client[0] if client else None, xff)

    def from_request(self, request) -> str:
        xff = ", ".join(request.headers.getlist("x-forwarded-for")) or None
        return self.resolve(request.client.host if request.client else None, xff)

    @staticmethod
    def key(ip: str) -> str:
        """Bucket key: an IPv6 /64 is one subscriber (a client can mint 2^64 addresses), IPv4-mapped IPv6 collapses to IPv4."""
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return ip
        if a.version == 6:
            if a.ipv4_mapped:
                return str(a.ipv4_mapped)
            return str(ipaddress.ip_network(f"{a}/64", strict=False).network_address) + "/64"
        return str(a)


# ------------------------------------------------------------------ sliding-window throttles

class Throttle:
    """In-memory sliding-window counters keyed by (bucket, key). Bounded: stale keys are swept so an attacker cannot grow it."""

    def __init__(self, clock=None):
        self.clock = clock
        self._hits: dict[tuple[str, str], deque] = defaultdict(deque)
        self._last_sweep = 0.0

    def _now(self) -> float:
        return (self.clock.now() / 1000) if self.clock else time.monotonic()

    def _sweep(self, now: float) -> None:
        if now - self._last_sweep < 60 and len(self._hits) < 50_000:
            return
        self._last_sweep = now
        for k in [k for k, d in self._hits.items() if not d or now - d[-1] > 3600]:
            del self._hits[k]
        if len(self._hits) > 200_000:  # hard bound: drop the oldest half rather than grow without limit
            for k in list(self._hits)[: len(self._hits) // 2]:
                del self._hits[k]

    def count(self, bucket: str, key: str, window_s: float) -> int:
        now = self._now()
        d = self._hits.get((bucket, key))
        if not d:
            return 0
        while d and now - d[0] > window_s:
            d.popleft()
        return len(d)

    def hit(self, bucket: str, key: str, limit: int, window_s: float, *, message: str = "too many requests — slow down") -> None:
        """Record one event; raise 429 when the window already holds ``limit`` of them."""
        now = self._now()
        self._sweep(now)
        if self.count(bucket, key, window_s) >= limit:
            raise AppError("RATE_LIMITED", message, 429)
        self._hits[(bucket, key)].append(now)

    def check(self, bucket: str, key: str, limit: int, window_s: float, *, message: str = "too many attempts — try again later") -> None:
        """Raise if the window is full, WITHOUT recording (pair with :meth:`record` on failure)."""
        if self.count(bucket, key, window_s) >= limit:
            raise AppError("RATE_LIMITED", message, 429)

    def forgive(self, bucket: str, key: str) -> None:
        """Take back the most recent hit (a provisional failure that turned out to be a success)."""
        d = self._hits.get((bucket, key))
        if d:
            d.pop()

    def record(self, bucket: str, key: str) -> None:
        self._sweep(self._now())
        self._hits[(bucket, key)].append(self._now())


# ------------------------------------------------------------------ ASGI middleware

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "font-src 'self' data:; connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'")


def security_headers(https: bool) -> list[tuple[bytes, bytes]]:
    h = [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"), (b"referrer-policy", b"strict-origin-when-cross-origin"),
         (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"), (b"content-security-policy", CSP.encode()),
         (b"cross-origin-opener-policy", b"same-origin"), (b"cross-origin-resource-policy", b"same-origin"), (b"x-permitted-cross-domain-policies", b"none")]
    if https:
        h.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
    return h


class HardeningMiddleware:
    """One pure-ASGI layer: security headers on every response, per-IP request/connection limits, request body-size caps,
    and no server banner."""

    def __init__(self, app, *, https: bool = False, client_ip: ClientIP | None = None, throttle: Throttle | None = None,
                 http_per_minute: int | None = None, webhook_per_minute: int = 120, ws_per_ip: int | None = None,
                 max_body: int = 1_000_000, max_upload: int = 10_000_000, max_webhook: int = 262_144):
        self.app, self.https, self.ip, self.throttle = app, https, client_ip or ClientIP(), throttle or Throttle()
        self.http_per_minute = http_per_minute or int(os.environ.get("KENYABIDDER_HTTP_PER_MINUTE", 600))
        self.webhook_per_minute = webhook_per_minute
        self.ws_per_ip = ws_per_ip or int(os.environ.get("KENYABIDDER_WS_PER_IP", 40))
        self.max_body, self.max_upload, self.max_webhook = max_body, max_upload, max_webhook
        self.headers = security_headers(https)
        self._ws: dict[str, int] = defaultdict(int)

    def _limit_for(self, path: str) -> int:
        if path.startswith("/_nicegui/") and "/upload" in path:
            return self.max_upload
        return self.max_webhook if path.startswith("/webhooks/") else self.max_body

    async def __call__(self, scope, receive, send):
        t = scope["type"]
        if t == "websocket":
            ip = self.ip.key(self.ip.from_scope(scope))
            if self._ws[ip] >= self.ws_per_ip:
                await send({"type": "websocket.close", "code": 1013})
                return
            self._ws[ip] += 1
            try:
                await self.app(scope, receive, send)
            finally:
                self._ws[ip] -= 1
                if self._ws[ip] <= 0:
                    self._ws.pop(ip, None)
            return
        if t != "http":
            await self.app(scope, receive, send)
            return
        path, ip = scope.get("path", ""), self.ip.key(self.ip.from_scope(scope))
        scope["kb_client_ip"] = ip
        # the UI's own transport (static assets, socket.io long-polling) is not a request budget item: connection counts cap it instead
        static = (path.startswith(("/_nicegui/", "/static/")) and scope["method"] == "GET") or path.startswith("/_nicegui_ws/")
        if not static:
            bucket, limit = ("webhook", self.webhook_per_minute) if path.startswith("/webhooks/") else ("http", self.http_per_minute)
            try:
                self.throttle.hit(bucket, ip, limit, 60)
            except AppError:
                await self._reply(send, 429, b"too many requests", extra=[(b"retry-after", b"30")])
                return
        cap = self._limit_for(path)
        for k, v in scope.get("headers", []):
            if k == b"content-length":
                try:
                    if int(v) > cap:
                        await self._reply(send, 413, b"request too large")
                        return
                except ValueError:
                    await self._reply(send, 400, b"bad content-length")
                    return
        seen = 0

        async def limited_receive():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > cap:  # chunked uploads have no content-length: count what actually arrives
                    raise AppError("TOO_LARGE", "request too large", 413)
            return msg

        started = False

        async def hardened_send(msg):
            nonlocal started
            if msg["type"] == "http.response.start":
                started = True
                hdrs = [(k, v) for k, v in msg.get("headers", []) if k.lower() not in (b"server", b"x-powered-by")]
                have = {k.lower() for k, _ in hdrs}
                hdrs += [(k, v) for k, v in self.headers if k not in have]
                msg = {**msg, "headers": hdrs}
            await send(msg)

        try:
            await self.app(scope, limited_receive, hardened_send)
        except AppError as e:
            if not started:
                await self._reply(send, e.status, e.message.encode())
            else:
                raise

    async def _reply(self, send, status: int, body: bytes, extra=()):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode()), *self.headers, *extra]})
        await send({"type": "http.response.body", "body": body})


# ------------------------------------------------------------------ outbound URLs (admin-configured LLM / MCP endpoints)

_METADATA_HOSTS = {"metadata.google.internal", "metadata", "instance-data", "metadata.azure.com"}


def check_outbound_url(url: str, what: str = "URL") -> None:
    """Admins may point at internal services (Ollama on a private host is normal) but never at cloud-metadata or link-local addresses,
    which would let a compromised admin account steal the host's cloud credentials."""
    import socket
    from urllib.parse import urlparse
    u = urlparse(url or "")
    if u.scheme not in ("http", "https") or not u.hostname:
        raise bad("INVALID_URL", f"{what} must be an http(s) address")
    if u.username or u.password:
        raise bad("INVALID_URL", f"{what} must not contain credentials — use the key field")
    host = u.hostname.lower().rstrip(".")
    if host in _METADATA_HOSTS or host.endswith(".internal") and "metadata" in host:
        raise bad("URL_BLOCKED", f"{what} points at a cloud metadata service")
    try:
        infos = socket.getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return  # not resolvable from here (may resolve where it will be used): the connection attempt will fail visibly
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_link_local or ip.is_unspecified or ip.is_multicast or str(ip) in ("169.254.169.254", "fd00:ec2::254"):
            raise bad("URL_BLOCKED", f"{what} resolves to a link-local/metadata address")


# ------------------------------------------------------------------ admin-named environment variables

_ENV_DENY = ("KENYABIDDER_", "MPESA_", "WHATSAPP_", "SMTP_", "AT_", "POSTGRES", "PG", "DATABASE", "DB_", "AWS_", "AZURE_", "GCP_", "GOOGLE_APPLICATION", "SSH_",
             "SECRET", "PRIVATE", "ENCRYPTION", "SESSION", "COOKIE", "STORAGE", "NICEGUI", "HOME", "PATH", "PWD", "USER")


def check_env_name(name: str, what: str = "environment variable") -> str:
    """Admins may reference a provider key by env-var NAME (so it stays out of the database) but never the platform's own secrets: otherwise
    'api_key_env=KENYABIDDER_ENCRYPTION_KEY' plus an attacker-controlled base URL would send that secret out as a bearer token.
    Extra names can be allowed with KENYABIDDER_ALLOWED_KEY_ENVS (comma separated)."""
    n = (name or "").strip()
    if not n:
        return ""
    allowed = {x.strip() for x in os.environ.get("KENYABIDDER_ALLOWED_KEY_ENVS", "").split(",") if x.strip()}
    if n in allowed:
        return n
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", n):
        raise bad("INVALID_ENV_NAME", f"{what} must look like PROVIDER_API_KEY (capital letters, digits, underscores)")
    if any(n.startswith(p) or n == p for p in _ENV_DENY) or n.endswith(("_SECRET", "_PASSWORD", "_PASSKEY", "_ENCRYPTION_KEY")) and not n.endswith("_API_SECRET"):
        raise bad("ENV_NAME_BLOCKED", f"{what} {n!r} is reserved for the platform's own configuration — use a provider key variable (e.g. ANTHROPIC_API_KEY)")
    if not (n.endswith(("_KEY", "_TOKEN", "_API_KEY", "_API_TOKEN")) or "API" in n):
        raise bad("ENV_NAME_BLOCKED", f"{what} {n!r} does not look like an API key variable (expected …_API_KEY or …_TOKEN); ask the operator to allow it via KENYABIDDER_ALLOWED_KEY_ENVS")
    return n


# ------------------------------------------------------------------ passwords

_COMMON = frozenset("""password password1 password12 password123 password1234 passw0rd p@ssw0rd p@ssword 12345678 123456789 1234567890 12345678910
qwertyui qwerty123 qwertyuiop 1q2w3e4r 1q2w3e4r5t iloveyou iloveyou1 admin123 admin1234 administrator welcome1 welcome123 letmein123 letmein1
changeme changeme1 changeme123 abc12345 abcd1234 monkey123 dragon123 football1 baseball1 master123 sunshine1 princess1 trustno1 000000000
111111111 11111111 00000000 87654321 987654321 asdfghjk asdf1234 zxcvbnm1 zaq12wsx kenya123 nairobi123 kenyabidder mombasa123 harambee1 safaricom1
mpesa1234 mpesa123 kenya2020 kenya2021 kenya2022 kenya2023 kenya2024 kenya2025 kenya2026 pass1234 test1234 testtest guest123 default123""".split())


def check_password(password: str, *, name: str = "", phone: str = "", email: str = "", policy: str = "strong") -> None:
    """Raise a friendly 400 if the password is weak. ``policy="basic"`` (tests/dev only) enforces just the length."""
    p = password or ""
    if len(p) < 8:
        raise bad("WEAK_PASSWORD", "password must be at least 8 characters")
    if len(p) > 256:
        raise bad("WEAK_PASSWORD", "password is too long (256 characters max)")
    if policy == "basic":
        return
    if len(p) < 10:
        raise bad("WEAK_PASSWORD", "use at least 10 characters (a few random words make a strong, memorable password)")
    low = p.lower()
    if low in _COMMON or re.sub(r"[^a-z0-9]", "", low) in _COMMON:
        raise bad("WEAK_PASSWORD", "that password is on the list of passwords attackers try first — choose another")
    if len(set(low)) < 5:
        raise bad("WEAK_PASSWORD", "that password is too repetitive")
    for ident in (name, email.split("@")[0] if email else "", re.sub(r"\D", "", phone or "")):
        ident = (ident or "").strip().lower()
        if len(ident) >= 4 and ident in low:
            raise bad("WEAK_PASSWORD", "the password must not contain your name, email or phone number")
    if re.fullmatch(r"[a-z]+|\d+", low):
        raise bad("WEAK_PASSWORD", "mix letters with digits or symbols, or use several words")


# ------------------------------------------------------------------ log redaction

_REDACT = [
    (re.compile(r"(/webhooks/mpesa/)[^\s/?\"']+"), r"\1***"),
    (re.compile(r"(?i)\b(bearer|authorization:?)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 ***"),
    # NAME=value, NAME: value, "NAME": "value", 'NAME': 'value' — where NAME merely CONTAINS key/secret/token/password/passkey (SMTP_PASSWORD, access_token…)
    (re.compile(r"(?i)([A-Za-z0-9_.-]*(?:api[_-]?key|apikey|secret|token|password|passwd|passkey|credential)[A-Za-z0-9_.-]*[\"']?\s*[:=]\s*[\"']?)[^\s\"',})&]{3,}"), r"\1***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "sk-***"),
    (re.compile(r"(?<!\d)(?:\+?254|0)[\s-]?[17](?:[\s-]?\d){8}(?!\d)"), "+254*******"),
]


def redact(text: str) -> str:
    for rx, rep in _REDACT:
        text = rx.sub(rep, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Redacts the FINAL line — including tracebacks and exception text, which a record filter never sees."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class RedactingFilter(logging.Filter):
    """Secrets and phone numbers must never reach log files (they outlive the database and travel to log vendors)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        red = redact(msg)
        if red != msg:
            record.msg, record.args = red, ()
        return True
