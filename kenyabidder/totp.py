"""RFC 6238 time-based one-time passwords (Google Authenticator / Authy / 1Password compatible) and recovery codes. Stdlib only."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import urllib.parse

STEP, DIGITS, WINDOW = 30, 6, 1


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    return f"{(struct.unpack('>I', mac[off:off + 4])[0] & 0x7FFFFFFF) % 10 ** DIGITS:0{DIGITS}d}"


def verify(secret: str, code: str, now_s: float, last_counter: int = -1) -> int | None:
    """Return the matched time-step counter (store it: a code can never be used twice), or None."""
    code = (code or "").strip().replace(" ", "")
    if not (len(code) == DIGITS and code.isdigit()):
        return None
    base = int(now_s // STEP)
    for off in range(-WINDOW, WINDOW + 1):
        c = base + off
        if c > last_counter and hmac.compare_digest(_code(secret, c), code):
            return c
    return None


def provisioning_uri(secret: str, account: str, issuer: str = "KenyaBidder") -> str:
    label = urllib.parse.quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={urllib.parse.quote(issuer)}&digits={DIGITS}&period={STEP}"


def new_recovery_codes(n: int = 8) -> list[str]:
    return [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(n)]


def hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().lower().encode()).hexdigest()
