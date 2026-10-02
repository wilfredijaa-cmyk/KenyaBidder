"""Encryption at rest for the few secrets the database holds (LLM/MCP API keys, 2FA seeds, signing secrets).

Fernet (AES-128-CBC + HMAC-SHA256, authenticated). The key comes from ``KENYABIDDER_ENCRYPTION_KEY`` (urlsafe-base64 32 bytes — put it in
your secret manager) or, failing that, is generated once into a ``0600`` key file kept OUTSIDE the database file, so a stolen database
or backup alone reveals none of them. Ciphertext carries a version prefix so keys can be rotated later.
"""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc:v1:"
_EPH: list = []


def _ephemeral_key() -> bytes:
    if not _EPH:
        _EPH.append(Fernet.generate_key())
    return _EPH[0]


class SecretBox:
    def __init__(self, key: str | bytes | None = None, key_file: str | os.PathLike | None = None):
        key = key or os.environ.get("KENYABIDDER_ENCRYPTION_KEY")
        if not key and key_file:
            p = Path(key_file)
            if p.exists():
                key = p.read_text().strip()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                key = Fernet.generate_key().decode()
                fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(key)
        if not key:
            key = _ephemeral_key()  # in-memory runs / tests: one key per process, nothing durable to protect
            self.ephemeral = True
        else:
            self.ephemeral = False
        try:
            self._f = Fernet(key if isinstance(key, bytes) else key.encode())
        except (ValueError, TypeError) as e:
            raise ValueError("KENYABIDDER_ENCRYPTION_KEY must be 32 url-safe base64-encoded bytes (generate: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')") from e

    def encrypt(self, value: str) -> str:
        if not isinstance(value, str) or value.startswith(PREFIX) or value == "":
            return value
        return PREFIX + self._f.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        if not isinstance(value, str) or not value.startswith(PREFIX):
            return value  # legacy plaintext (written before encryption existed): readable, re-encrypted on the next write
        try:
            return self._f.decrypt(value[len(PREFIX):].encode()).decode()
        except InvalidToken:
            raise ValueError("cannot decrypt a stored secret: wrong KENYABIDDER_ENCRYPTION_KEY (or the key file was lost)") from None


# which fields of which collection are secret: dotted paths inside each document
SENSITIVE = {"llms": ["api_key"], "mcps": ["bearer_token", "env"], "users": ["totp.secret"]}
SENSITIVE_SETTINGS = ["otp_secret", "mcp_key", "storage_secret", "mpesa_callback_secret"]


def _walk(obj, path: list[str], fn) -> None:
    if not isinstance(obj, dict) or not path:
        return
    k = path[0]
    if len(path) == 1:
        if k in obj:
            v = obj[k]
            if isinstance(v, str):
                obj[k] = fn(v)
            elif isinstance(v, dict):  # e.g. headers / env maps: every value is a secret
                for kk, vv in list(v.items()):
                    if isinstance(vv, str):
                        v[kk] = fn(vv)
    else:
        _walk(obj.get(k), path[1:], fn)


def _copy_paths(ent: dict, paths: list[str]) -> dict:
    """Shallow-copy only the dicts along the sensitive paths (cheap: the rest of the entity is shared, never mutated)."""
    out = dict(ent)
    for p in paths:
        parts = p.split(".")
        cur_out, cur_src = out, ent
        for k in parts[:-1]:
            if not isinstance(cur_src.get(k), dict):
                break
            cur_out[k] = dict(cur_src[k])
            cur_out, cur_src = cur_out[k], cur_src[k]
        else:
            last = parts[-1]
            if isinstance(cur_src.get(last), dict):
                cur_out[last] = dict(cur_src[last])
    return out


def seal(box: SecretBox, collection: str, ent):
    paths = SENSITIVE.get(collection)
    if not paths or not isinstance(ent, dict):
        return ent
    out = _copy_paths(ent, paths)
    for p in paths:
        _walk(out, p.split("."), box.encrypt)
    return out


def unseal(box: SecretBox, collection: str, ent):
    paths = SENSITIVE.get(collection)
    if not paths or not isinstance(ent, dict):
        return ent
    for p in paths:
        _walk(ent, p.split("."), box.decrypt)
    return ent


def seal_settings(box: SecretBox, settings: dict) -> dict:
    out = dict(settings)
    for k in SENSITIVE_SETTINGS:
        if isinstance(out.get(k), str):
            out[k] = box.encrypt(out[k])
    return out


def unseal_settings(box: SecretBox, settings: dict) -> dict:
    for k in SENSITIVE_SETTINGS:
        if isinstance(settings.get(k), str):
            settings[k] = box.decrypt(settings[k])
    return settings
