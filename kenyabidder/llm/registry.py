"""LLM registry: admins add models; agents are mapped to them (spec §7.2: LLM = strategy only)."""
from __future__ import annotations

import os
import uuid
from typing import Callable

from ..errors import bad, conflict, not_found
from ..security import check_env_name, check_outbound_url
from .providers import AnthropicProvider, OpenAICompatProvider, Provider

PROVIDERS = {
    "anthropic": {"label": "Anthropic (Claude)", "default_model": "claude-haiku-4-5-20251001", "default_key_env": "ANTHROPIC_API_KEY", "base_url": ""},
    "openai_compatible": {"label": "OpenAI-compatible (OpenAI, Ollama, vLLM, OpenRouter…)", "default_model": "gpt-4o-mini",
                          "default_key_env": "OPENAI_API_KEY", "base_url": "https://api.openai.com/v1"},
}
ROLES = ["BIDDER", "SELLER"]


class LlmRegistry:
    def __init__(self, store, clock, provider_factory: Callable[[dict], Provider] | None = None):
        self.store, self.clock = store, clock
        self._factory = provider_factory  # tests inject fakes
        self._overrides: dict[str, Provider] = {}
        self._cache: dict[str, tuple[tuple, Provider]] = {}
        self.deletion_blockers: list = []  # callables(llm_id) -> list[str] of reasons (wallet balances, packs, open orders)

    # ---------- CRUD ----------

    def add(self, *, name: str, provider: str, model: str, base_url: str = "", api_key: str = "", api_key_env: str = "",
            max_tokens: int = 1024, temperature: float | None = None, timeout_s: float = 30.0,
            roles: list[str] | None = None, notes: str = "", enabled: bool = True,
            billing_mode: str = "metered", output_multiplier: int = 1, cost_per_1k_kes: float = 0.0) -> dict:
        entry = self._validate({"name": name, "provider": provider, "model": model, "base_url": base_url,
                                "api_key": api_key, "api_key_env": api_key_env, "max_tokens": max_tokens,
                                "temperature": temperature, "timeout_s": timeout_s, "roles": roles or list(ROLES),
                                "notes": notes, "enabled": enabled, "billing_mode": billing_mode,
                                "output_multiplier": output_multiplier, "cost_per_1k_kes": cost_per_1k_kes})
        if any(e["name"].lower() == entry["name"].lower() for e in self.store.llms.values()):
            raise conflict("NAME_TAKEN", f"an LLM named {entry['name']!r} already exists")
        entry.update(id=str(uuid.uuid4()), created_at=self.clock.now())
        self.store.llms[entry["id"]] = entry
        return entry

    def update(self, llm_id: str, **patch) -> dict:
        e = self.get(llm_id)
        if not patch.get("api_key"):
            patch.pop("api_key", None)  # blank = keep the stored secret
        merged = self._validate({**e, **{k: v for k, v in patch.items() if v is not None or k == "temperature"}})
        if any(o["id"] != llm_id and o["name"].lower() == merged["name"].lower() for o in self.store.llms.values()):
            raise conflict("NAME_TAKEN", f"an LLM named {merged['name']!r} already exists")
        e.update(merged)
        self._overrides.pop(llm_id, None)
        return e

    def remove(self, llm_id: str) -> None:
        self.get(llm_id)
        users = [a["agent_id"][:6] for a in self.store.agents.values() if llm_id in _llm_refs(a)]
        if users:
            raise conflict("IN_USE", f"LLM is assigned to agent(s): {', '.join(users)} — unassign it first")
        reasons = [r for check in self.deletion_blockers for r in check(llm_id)]
        if reasons:
            raise conflict("IN_USE", "cannot delete this LLM — " + "; ".join(reasons) + ". Disable it instead.")
        del self.store.llms[llm_id]
        self._overrides.pop(llm_id, None)
        self._cache.pop(llm_id, None)

    def get(self, llm_id: str) -> dict:
        e = self.store.llms.get(llm_id)
        if not e:
            raise not_found("LLM_NOT_FOUND", "LLM not found")
        return e

    def list(self, *, enabled_only: bool = False, role: str | None = None) -> list[dict]:
        return [e for e in sorted(self.store.llms.values(), key=lambda x: x["name"].lower())
                if (not enabled_only or e["enabled"]) and (not role or role in e["roles"])]

    @staticmethod
    def masked(e: dict) -> dict:
        """Never send stored secrets back to the UI."""
        out = {k: v for k, v in e.items() if k != "api_key"}
        out["has_api_key"] = bool(e.get("api_key"))
        return out

    def _validate(self, e: dict) -> dict:
        if not str(e.get("name", "")).strip():
            raise bad("INVALID_LLM", "name is required")
        if e["provider"] not in PROVIDERS:
            raise bad("INVALID_LLM", f"provider must be one of {', '.join(PROVIDERS)}")
        if not str(e.get("model", "")).strip():
            raise bad("INVALID_LLM", "model is required")
        if e["provider"] == "openai_compatible":
            if not str(e.get("base_url", "")).startswith(("http://", "https://")):
                raise bad("INVALID_LLM", "base_url (http/https) is required for OpenAI-compatible endpoints")
        elif e.get("base_url") and not str(e["base_url"]).startswith(("http://", "https://")):
            raise bad("INVALID_LLM", "base_url must be http(s)")
        if e.get("base_url"):
            check_outbound_url(e["base_url"], "base_url")
        e["api_key_env"] = check_env_name(e.get("api_key_env", ""), "api_key_env")
        if not (isinstance(e["max_tokens"], int) and 16 <= e["max_tokens"] <= 64_000):
            raise bad("INVALID_LLM", "max_tokens must be between 16 and 64000")
        if e.get("temperature") is not None and not (0 <= e["temperature"] <= 2):
            raise bad("INVALID_LLM", "temperature must be between 0 and 2")
        if not (0 < e["timeout_s"] <= 300):
            raise bad("INVALID_LLM", "timeout_s must be between 0 and 300")
        if not e["roles"] or any(r not in ROLES for r in e["roles"]):
            raise bad("INVALID_LLM", "roles must be a non-empty subset of BIDDER, SELLER")
        e.setdefault("billing_mode", "metered")
        e.setdefault("output_multiplier", 1)
        e.setdefault("cost_per_1k_kes", 0.0)
        if e["billing_mode"] not in ("metered", "free"):
            raise bad("INVALID_LLM", "billing_mode must be 'metered' (users buy tokens) or 'free' (platform-funded)")
        m = e["output_multiplier"]
        if not (isinstance(m, int) and not isinstance(m, bool) and 1 <= m <= 50):
            raise bad("INVALID_LLM", "output_multiplier must be a whole number from 1 to 50 (output tokens count this many times)")
        c = e["cost_per_1k_kes"]
        if not (isinstance(c, (int, float)) and not isinstance(c, bool) and 0 <= c <= 1_000_000):
            raise bad("INVALID_LLM", "cost_per_1k_kes must be a non-negative number")
        return {**e, "name": e["name"].strip(), "model": e["model"].strip()}

    # ---------- runtime ----------

    def resolve_key(self, e: dict) -> str | None:
        if e.get("api_key"):
            return e["api_key"]
        env = e.get("api_key_env") or PROVIDERS[e["provider"]]["default_key_env"]
        return os.environ.get(env) or None

    def provider(self, llm_id: str) -> Provider:
        if llm_id in self._overrides:
            return self._overrides[llm_id]
        e = self.get(llm_id)
        if not e["enabled"]:
            raise bad("LLM_DISABLED", f"LLM {e['name']!r} is disabled")
        if self._factory:
            return self._factory(e)
        key = self.resolve_key(e)
        fingerprint = (e["provider"], e["model"], e.get("base_url"), key, e["timeout_s"])
        hit = self._cache.get(llm_id)
        if hit and hit[0] == fingerprint:  # reuse the HTTP client/connection pool across decisions
            return hit[1]
        if e["provider"] == "anthropic":
            if not key:
                raise bad("NO_API_KEY", f"no API key for {e['name']!r} (set it here or via the {e.get('api_key_env') or 'ANTHROPIC_API_KEY'} env var)")
            prov: Provider = AnthropicProvider(key, e["model"], e.get("base_url") or None, e["timeout_s"])
        else:
            prov = OpenAICompatProvider(key, e["model"], e["base_url"], e["timeout_s"])
        self._cache[llm_id] = (fingerprint, prov)
        return prov

    def set_provider_override(self, llm_id: str, provider: Provider) -> None:
        """Test hook / offline demos."""
        self._overrides[llm_id] = provider

    async def test(self, llm_id: str) -> dict:
        """Live connectivity check used by the admin UI."""
        try:
            r = await self.provider(llm_id).complete("You are a connectivity probe.", [{"role": "user", "content": "Reply with the single word: ok"}], [], max_tokens=16)
            return {"ok": True, "reply": r.text.strip()[:80], "usage": r.usage}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:300]}


def _llm_refs(agent: dict) -> set[str]:
    cfg = agent.get("config", {})
    ids = {cfg.get("llm_id")} - {None}
    ids |= {v.get("llm_id") for v in cfg.get("algorithms", {}).values() if v.get("llm_id")}
    ids |= {(cfg.get("advisor") or {}).get("llm_id")} - {None}
    return ids
