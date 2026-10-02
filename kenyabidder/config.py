"""Startup configuration checks. ``KENYABIDDER_ENV=production`` turns insecure combinations into hard errors (override only with
``KENYABIDDER_ALLOW_INSECURE=1``, which logs loudly); outside production they are warnings."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def is_production(env=os.environ) -> bool:
    return env.get("KENYABIDDER_ENV", "").lower() == "production"


def public_https(env=os.environ) -> bool:
    return env.get("KENYABIDDER_PUBLIC_URL", "").lower().startswith("https://")


def validate(env=os.environ, *, host: str = "127.0.0.1", durable: bool = True) -> Report:
    env = {k: v.strip() for k, v in dict(env).items() if isinstance(v, str) and v.strip()}  # blank values (an empty .env line) count as unset
    r = Report()
    prod = is_production(env)
    bad = r.errors if prod else r.warnings

    if env.get("KENYABIDDER_DEV_PAYMENTS") == "1":
        bad.append("KENYABIDDER_DEV_PAYMENTS=1 mints free tokens — never enable it in production")
    if env.get("KENYABIDDER_INSECURE_WEBHOOK") == "1":
        bad.append("KENYABIDDER_INSECURE_WEBHOOK=1 accepts unsigned WhatsApp messages (anyone could approve bids) — set WHATSAPP_APP_SECRET instead")
    if (env.get("KENYABIDDER_PASSWORD_POLICY") or "strong") == "basic":
        bad.append("KENYABIDDER_PASSWORD_POLICY=basic disables the weak-password checks")
    if prod and not public_https(env):
        r.errors.append("set KENYABIDDER_PUBLIC_URL=https://your.domain (the site must be served over TLS; cookies and HSTS depend on it)")
    if not durable:
        bad.append("running with an in-memory database: everything is lost on restart (set --data or KENYABIDDER_DATABASE_URL)")
    if prod and not env.get("KENYABIDDER_BOOTSTRAP_TOKEN"):
        r.errors.append("KENYABIDDER_BOOTSTRAP_TOKEN is not set: the first visitor to a fresh installation becomes the administrator. Set it (and enter it at "
                          "first sign-up); an existing installation that already has an administrator can set any random value")
    if prod and not env.get("KENYABIDDER_SECRET"):
        r.warnings.append("KENYABIDDER_SECRET is not set: a session secret is generated and stored in the database — set it from your secret manager so a leaked database alone cannot forge sessions")
    if prod and not env.get("KENYABIDDER_ENCRYPTION_KEY"):
        r.warnings.append("KENYABIDDER_ENCRYPTION_KEY is not set: the at-rest encryption key lives in data/.encryption_key — keep it out of backups, or set the variable")
    if host not in ("127.0.0.1", "localhost", "::1") and not env.get("KENYABIDDER_TRUSTED_PROXIES"):
        r.warnings.append("listening on a public interface without KENYABIDDER_TRUSTED_PROXIES: per-IP limits will see only your proxy's address behind a reverse proxy")
    if not env.get("KENYABIDDER_METRICS_TOKEN"):
        r.warnings.append("KENYABIDDER_METRICS_TOKEN is not set: /metrics answers loopback requests without forwarding headers only")
    if env.get("KENYABIDDER_ALLOW_STDIO") == "1":
        r.warnings.append("KENYABIDDER_ALLOW_STDIO=1 lets admins register MCP servers that run local commands — only on hosts where every admin is trusted")
    for group, keys in {"M-Pesa": ["MPESA_CONSUMER_KEY", "MPESA_CONSUMER_SECRET", "MPESA_SHORTCODE", "MPESA_PASSKEY", "MPESA_CALLBACK_BASE_URL"],
                        "Africa's Talking SMS": ["AT_USERNAME", "AT_API_KEY"], "WhatsApp": ["WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_APP_SECRET"]}.items():
        have = [k for k in keys if env.get(k)]
        if have and len(have) != len(keys):
            r.warnings.append(f"{group} is partially configured (missing {', '.join(k for k in keys if k not in have)})")
    if env.get("MPESA_CALLBACK_BASE_URL", "https://").lower().startswith("http://") and prod:
        r.errors.append("MPESA_CALLBACK_BASE_URL must be https")
    return r
