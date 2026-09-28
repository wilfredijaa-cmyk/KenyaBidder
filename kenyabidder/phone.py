"""Phone number handling for a Kenya-first product.

Users type 0712 345 678, +254712345678 or 254712345678 — WhatsApp delivers 254712345678, M-Pesa needs 254712345678.
Everything is normalized to E.164 (``+254712345678``) so the same person always matches the same identity.
"""
from __future__ import annotations

import re

_KE = re.compile(r"^254([17]\d{8})$")


def normalize_phone(raw: str | None) -> str | None:
    """Return E.164, or None if it is not a plausible number. Kenyan formats are accepted in every common spelling."""
    if not raw or not isinstance(raw, str):
        return None
    s = re.sub(r"[\s\-().]", "", raw.strip())
    plus = s.startswith("+")
    digits = s[1:] if plus else s
    if not digits.isdigit():
        return None
    if digits.startswith("254") and _KE.match(digits):
        return "+" + digits
    if not plus and re.fullmatch(r"0[17]\d{8}", digits):  # 0712345678 / 0112345678
        return "+254" + digits[1:]
    if not plus and re.fullmatch(r"[17]\d{8}", digits):  # 712345678
        return "+254" + digits
    if plus and not digits.startswith("254") and 8 <= len(digits) <= 15 and digits[0] != "0":  # other countries, E.164
        return "+" + digits
    return None


def whatsapp_id(phone: str) -> str:
    """WhatsApp Cloud API identifies senders by digits only."""
    return re.sub(r"\D", "", phone)


def mpesa_msisdn(phone: str | None) -> str | None:
    """M-Pesa (Daraja) needs a Kenyan MSISDN as 2547XXXXXXXX / 2541XXXXXXXX."""
    p = normalize_phone(phone)
    return p[1:] if p and _KE.match(p[1:]) else None
