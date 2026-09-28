"""Users, agents, per-agent configuration and the channel identity map (spec §7.1, §10).

Each agent carries a ``config`` describing what it is *allowed to use*:

    config = {
      "llm_id":         default LLM for this agent (or None),
      "algorithms":     {AUCTION_TYPE: {"strategy": baseline|heuristic|llm, "llm_id": optional override}},   # bidders
      "advisor":        {"strategy": rules|llm, "llm_id": optional},                                       # sellers
      "tools":          ["<mcp_id>:<tool_name>", ...]   # the only tools its LLM may call
      "kb_ids":         [knowledge-base ids its LLM may search],
      "max_tool_steps": cap on tool-call rounds per decision,
    }
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import os
import re
import uuid

from .engine import AUCTION_TYPES
from .errors import AppError, bad, conflict, forbidden, is_nonneg_int, not_found
from .phone import normalize_phone

TERMS_VERSION = "2026-09"
STRATEGIES = ["baseline", "heuristic", "llm"]
ADVISORS = ["rules", "llm"]


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(got.hex(), h)
    except Exception:  # noqa: BLE001
        return False


def default_config(agent_type: str) -> dict:
    return {
        "llm_id": None,
        "algorithms": {t: {"strategy": "heuristic", "llm_id": None} for t in AUCTION_TYPES} if agent_type == "BIDDER" else {},
        "advisor": {"strategy": "rules", "llm_id": None} if agent_type == "SELLER" else None,
        "tools": [], "kb_ids": [], "max_tool_steps": 4, "max_tokens_per_day": None,
    }


class AgentService:
    def __init__(self, store, clock, execution=None, llms=None, mcps=None, kbs=None):
        self.store, self.clock = store, clock
        self.execution, self.llms, self.mcps, self.kbs = execution, llms, mcps, kbs
        self.on_identity_change = None  # set by the composition root: claims the once-per-phone signup token grant

    # ---------- users ----------

    def create_user(self, *, name: str, password: str, phone: str | None = None, email: str | None = None,
                    role: str | None = None, accepted_terms: bool = True) -> dict:
        """Register an account. Programmatic callers consent implicitly (default); the sign-up form passes the checkbox."""
        if not accepted_terms:
            raise bad("TERMS_REQUIRED", "you must accept the Terms and Privacy Notice to create an account")
        name = (name or "").strip()
        if not re.fullmatch(r"[\w .'-]{2,40}", name):
            raise bad("INVALID_USER", "name must be 2-40 characters (letters, digits, space, . ' -)")
        if len(password or "") < 8:
            raise bad("INVALID_USER", "password must be at least 8 characters")
        if any(u["name"].lower() == name.lower() for u in self.store.users.values()):
            raise conflict("NAME_TAKEN", "that name is already registered")
        phone_n = self._phone(phone)
        email = (email or "").strip() or None
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise bad("INVALID_EMAIL", "that email address does not look right")
        first = not self.store.users
        u = {"id": str(uuid.uuid4()), "name": name, "password_hash": hash_password(password), "phone": phone_n, "email": email,
             "role": "admin" if first else (role if role in ("admin", "user") else "user"), "suspended": False,
             "terms_accepted_at": self.clock.now(), "terms_version": TERMS_VERSION, "created_at": self.clock.now()}
        self.store.users[u["id"]] = u
        if self.on_identity_change:
            self.on_identity_change(u)
        return u

    @staticmethod
    def _phone(raw: str | None) -> str | None:
        if not (raw or "").strip():
            return None
        p = normalize_phone(raw)
        if not p:
            raise bad("INVALID_PHONE", "enter a phone number such as 0712 345 678 or +254 712 345 678")
        return p

    def set_email(self, user_id: str, email: str) -> dict:
        u = self.store.users.get(user_id)
        if not u:
            raise not_found("USER_NOT_FOUND", "user not found")
        email = (email or "").strip() or None
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise bad("INVALID_EMAIL", "that email address does not look right")
        u["email"] = email
        return u

    def set_phone(self, user_id: str, phone: str) -> dict:
        """Add/replace the contact phone (also claims the once-per-phone signup grant if there is one)."""
        u = self.store.users.get(user_id)
        if not u:
            raise not_found("USER_NOT_FOUND", "user not found")
        if any(o["id"] != user_id and o["phone"] and o["phone"] == self._phone(phone) for o in self.store.users.values()):
            raise conflict("PHONE_TAKEN", "that phone number is already registered to another account")
        u["phone"] = self._phone(phone)
        if self.on_identity_change:
            self.on_identity_change(u)
        return u

    MAX_FAILURES, LOCKOUT_MS = 5, 5 * 60_000

    def authenticate(self, name: str, password: str) -> dict | None:
        """Verify credentials. After 5 failures for a name (existing or not) further attempts are refused for 5 minutes."""
        key = (name or "").strip().lower()
        now = self.clock.now()
        fails = [t for t in self.store.settings.setdefault("login_failures", {}).get(key, []) if now - t < self.LOCKOUT_MS]
        if len(fails) >= self.MAX_FAILURES:
            raise AppError("TOO_MANY_ATTEMPTS", "too many failed sign-in attempts — try again in a few minutes", 429)
        u = self._authenticate(name, password)
        if u and u.get("suspended"):  # only revealed to someone who knows the password
            raise AppError("ACCOUNT_SUSPENDED", "this account has been suspended — contact support", 403)
        if u:
            self.store.settings["login_failures"].pop(key, None)
        else:
            self.store.settings["login_failures"][key] = [*fails, now]
        return u

    def _authenticate(self, name: str, password: str) -> dict | None:
        u = next((u for u in self.store.users.values() if u["name"].lower() == (name or "").strip().lower()), None)
        # verify against a dummy hash when the user is unknown so timing does not reveal which names exist
        if not u:
            verify_password(password or "", "scrypt$" + "00" * 16 + "$" + "00" * 32)
            return None
        return u if verify_password(password or "", u["password_hash"]) else None

    def change_password(self, user_id: str, old: str, new: str) -> None:
        u = self.store.users.get(user_id)
        if not u or not verify_password(old or "", u["password_hash"]):
            raise forbidden("WRONG_PASSWORD", "current password is incorrect")
        if len(new or "") < 8:
            raise bad("INVALID_USER", "password must be at least 8 characters")
        u["password_hash"] = hash_password(new)

    def admin_reset_password(self, user_id: str, new: str) -> None:
        """Support tool: there is no email reset flow yet, so an admin sets a temporary password and tells the user."""
        u = self.store.users.get(user_id)
        if not u:
            raise not_found("USER_NOT_FOUND", "user not found")
        if len(new or "") < 8:
            raise bad("INVALID_USER", "password must be at least 8 characters")
        u["password_hash"] = hash_password(new)
        self.store.settings.setdefault("login_failures", {}).pop(u["name"].lower(), None)

    def set_suspended(self, user_id: str, suspended: bool, *, by: str | None = None) -> dict:
        """Suspend/reinstate an account. Suspension pauses every agent immediately (bid triggers cancelled)."""
        u = self.store.users.get(user_id)
        if not u:
            raise not_found("USER_NOT_FOUND", "user not found")
        if suspended and user_id == by:
            raise bad("SELF_SUSPEND", "you cannot suspend your own account")
        if suspended and u["role"] == "admin" and sum(1 for x in self.store.users.values() if x["role"] == "admin" and not x.get("suspended")) <= 1:
            raise bad("LAST_ADMIN", "there must always be at least one active administrator")
        u["suspended"] = bool(suspended)
        if not suspended:
            for a in self.agents_for(user_id):
                if a["status"] == "SUSPENDED":
                    a["status"] = "PAUSED"  # reinstated accounts restart deliberately, never automatically
        if suspended:
            for a in self.agents_for(user_id):
                self.set_status(a["agent_id"], "SUSPENDED")
        return u

    def set_role(self, user_id: str, role: str) -> dict:
        u = self.store.users.get(user_id)
        if not u:
            raise not_found("USER_NOT_FOUND", "user not found")
        if role not in ("admin", "user"):
            raise bad("INVALID_ROLE", "role must be admin or user")
        if u["role"] == "admin" and role != "admin" and sum(1 for x in self.store.users.values() if x["role"] == "admin") <= 1:
            raise bad("LAST_ADMIN", "there must always be at least one administrator")
        u["role"] = role
        return u

    # ---------- agents ----------

    def create_agent(self, *, user_id: str, type: str, constraints: dict | None = None, memory: dict | None = None,
                     config: dict | None = None) -> dict:
        if user_id not in self.store.users:
            raise not_found("USER_NOT_FOUND", "user not found")
        if type not in ("SELLER", "BIDDER"):
            raise bad("INVALID_AGENT_TYPE", "type must be SELLER or BIDDER")
        agent = {
            "agent_id": str(uuid.uuid4()), "agent_type": type, "principal_user_id": user_id, "channel_identity_map": [],
            "constraints": self._constraints(type, constraints or {}),
            "reputation": {"completed_matches": 0, "fell_through_count": 0, "avg_response_time_seconds": 0, "tier": "NEW", "score": 100},
            "durable_memory": {"conversation": [], "auto_bid": True, "watch": None, "auto_relist": None,
                               "preferred_channel": "WEB", "last_channel": "WEB", "one_off_authorizations": [], "notes": "",
                               "daily_summary": True, "last_summary_day": None},
            "config": default_config(type), "status": "ACTIVE", "created_at": self.clock.now(),
        }
        self.store.agents[agent["agent_id"]] = agent
        try:
            if memory:
                self._apply_memory(agent, memory)
            if config:
                agent["config"] = self._config(agent, config)
        except Exception:
            del self.store.agents[agent["agent_id"]]
            raise
        return agent

    def _constraints(self, type: str, c: dict) -> dict:
        out = {"budget_ceiling": c.get("budget_ceiling", 0), "reserve_floor": c.get("reserve_floor", 0),
               "authorized_auction_types": c.get("authorized_auction_types", list(AUCTION_TYPES)),
               "escalation_threshold_pct": c.get("escalation_threshold_pct", 100)}
        if not is_nonneg_int(out["budget_ceiling"]):
            raise bad("INVALID_CONSTRAINT", "budget_ceiling must be a non-negative integer")
        if type == "BIDDER" and out["budget_ceiling"] <= 0:
            raise bad("INVALID_CONSTRAINT", "budget_ceiling is required for bidder agents")
        if not is_nonneg_int(out["reserve_floor"]):
            raise bad("INVALID_CONSTRAINT", "reserve_floor must be a non-negative integer")
        t = out["authorized_auction_types"]
        if not isinstance(t, list) or not t or any(x not in AUCTION_TYPES for x in t):
            raise bad("INVALID_CONSTRAINT", f"authorized_auction_types must be a non-empty subset of {', '.join(AUCTION_TYPES)}")
        out["authorized_auction_types"] = list(dict.fromkeys(t))
        p = out["escalation_threshold_pct"]
        if not (isinstance(p, (int, float)) and not isinstance(p, bool) and 0 < p <= 100):
            raise bad("INVALID_CONSTRAINT", "escalation_threshold_pct must be in (0, 100]")
        return out

    def _watch(self, w: dict) -> dict:
        if not isinstance(w, dict):
            raise bad("INVALID_WATCH", "watch must be an object")
        cat = w.get("category")
        if cat is not None and (not isinstance(cat, str)):
            raise bad("INVALID_WATCH", "watch.category must be a string")
        mq = w.get("min_quantity", 1)
        if not (isinstance(mq, int) and mq > 0):
            raise bad("INVALID_WATCH", "watch.min_quantity must be a positive integer")
        dl = w.get("deadline_at")
        if dl is not None and not isinstance(dl, int):
            raise bad("INVALID_WATCH", "watch.deadline_at must be epoch ms")
        return {"category": (cat or "").strip() or None, "keywords": [str(k).strip() for k in w.get("keywords", []) if str(k).strip()],
                "min_quantity": mq, "deadline_at": dl}

    def _apply_memory(self, agent: dict, m: dict) -> None:
        m = dict(m)
        m.pop("conversation", None)
        m.pop("one_off_authorizations", None)
        if "watch" in m:
            m["watch"] = None if m["watch"] is None else self._watch(m["watch"])
        if "daily_summary" in m:
            m["daily_summary"] = bool(m["daily_summary"])
        m.pop("last_summary_day", None)
        if "preferred_channel" in m and m["preferred_channel"] not in ("WEB", "WHATSAPP"):
            raise bad("INVALID_MEMORY", "preferred_channel must be WEB or WHATSAPP")
        if m.get("auto_relist") is not None:
            r = m["auto_relist"]
            if not (isinstance(r.get("max_relists", 3), int) and 0 <= r.get("max_relists", 3) <= 20
                    and 0 <= r.get("discount_pct", 10) < 100):
                raise bad("INVALID_MEMORY", "auto_relist needs max_relists 0-20 and discount_pct 0-99")
        agent["durable_memory"].update(m)

    def _config(self, agent: dict, c: dict) -> dict:
        """Validate a config against the LLM / MCP / KB registries and the agent's role."""
        role = agent["agent_type"]
        out = copy.deepcopy(default_config(role))
        base = agent.get("config") or default_config(role)
        merged = {**base, **{k: v for k, v in c.items() if k in out}}

        already = {base.get("llm_id"), (base.get("advisor") or {}).get("llm_id"), *(v.get("llm_id") for v in (base.get("algorithms") or {}).values())} - {None}

        def check_llm(llm_id, what):
            if llm_id in (None, ""):
                return None
            if not self.llms:
                raise bad("INVALID_LLM", "no LLM registry available")
            e = self.llms.get(llm_id)  # raises if unknown
            if not e["enabled"] and llm_id not in already:  # an LLM disabled *after* assignment must not stop other edits
                raise bad("INVALID_LLM", f"{what}: LLM {e['name']!r} is disabled")
            if role not in e["roles"]:
                raise bad("INVALID_LLM", f"{what}: LLM {e['name']!r} is not enabled for {role} agents")
            return llm_id

        out["llm_id"] = check_llm(merged.get("llm_id"), "default LLM")
        if role == "BIDDER":
            algos = merged.get("algorithms") or {}
            for t, spec in algos.items():
                if t not in AUCTION_TYPES:
                    raise bad("INVALID_ALGORITHM", f"unknown auction type {t}")
                s = (spec or {}).get("strategy", "heuristic")
                if s not in STRATEGIES:
                    raise bad("INVALID_ALGORITHM", f"strategy for {t} must be one of {', '.join(STRATEGIES)}")
                lid = check_llm((spec or {}).get("llm_id"), f"{t} LLM")
                if s == "llm" and not (lid or out["llm_id"]):
                    raise bad("INVALID_ALGORITHM", f"{t} uses the llm strategy but no LLM is assigned (set a default LLM or a per-algorithm LLM)")
                out["algorithms"][t] = {"strategy": s, "llm_id": lid}
        else:
            adv = merged.get("advisor") or {"strategy": "rules", "llm_id": None}
            if adv.get("strategy", "rules") not in ADVISORS:
                raise bad("INVALID_ALGORITHM", "advisor strategy must be rules or llm")
            lid = check_llm(adv.get("llm_id"), "advisor LLM")
            if adv["strategy"] == "llm" and not (lid or out["llm_id"]):
                raise bad("INVALID_ALGORITHM", "the llm advisor needs an LLM (set a default LLM or an advisor LLM)")
            out["advisor"] = {"strategy": adv["strategy"], "llm_id": lid}
        tools = merged.get("tools") or []
        out["tools"] = self.mcps.validate_refs(role, tools) if tools else []
        kb_ids = merged.get("kb_ids") or []
        for k in kb_ids:
            kb = self.kbs.get(k)
            if not kb["enabled"] or role not in kb["roles"]:
                raise bad("INVALID_KB", f"knowledge base {kb['name']!r} is not available to {role} agents")
        out["kb_ids"] = list(dict.fromkeys(kb_ids))
        steps = merged.get("max_tool_steps", 4)
        if not (isinstance(steps, int) and 0 <= steps <= 10):
            raise bad("INVALID_CONFIG", "max_tool_steps must be 0-10")
        out["max_tool_steps"] = steps
        cap = merged.get("max_tokens_per_day")
        if cap in ("", 0):
            cap = None
        if cap is not None and not (isinstance(cap, int) and not isinstance(cap, bool) and 1_000 <= cap <= 100_000_000):
            raise bad("INVALID_CONFIG", "max_tokens_per_day must be blank (no cap) or a whole number between 1,000 and 100,000,000")
        out["max_tokens_per_day"] = cap
        if (out["tools"] or out["kb_ids"]) and not self._uses_llm(role, out):
            raise bad("INVALID_CONFIG", "tools and knowledge bases are only used by LLM strategies — switch at least one algorithm to 'llm' (or the advisor) first")
        return out

    @staticmethod
    def _uses_llm(role: str, cfg: dict) -> bool:
        if role == "BIDDER":
            return any(a["strategy"] == "llm" for a in cfg["algorithms"].values())
        return (cfg.get("advisor") or {}).get("strategy") == "llm"

    def get_agent(self, agent_id: str) -> dict:
        a = self.store.agents.get(agent_id)
        if not a:
            raise not_found("AGENT_NOT_FOUND", "agent not found")
        return a

    def owned_agent(self, agent_id: str, user_id: str) -> dict:
        a = self.get_agent(agent_id)
        if a["principal_user_id"] != user_id:
            raise forbidden("NOT_YOUR_AGENT", "agent belongs to another user")
        return a

    def agents_for(self, user_id: str) -> list[dict]:
        return [a for a in self.store.agents.values() if a["principal_user_id"] == user_id]

    def update(self, agent_id: str, *, constraints: dict | None = None, memory: dict | None = None,
               config: dict | None = None, status: str | None = None) -> dict:
        agent = self.get_agent(agent_id)
        if constraints:
            agent["constraints"] = self._constraints(agent["agent_type"], {**agent["constraints"], **constraints})
        if memory:
            self._apply_memory(agent, memory)
        if config is not None:
            agent["config"] = self._config(agent, config)
        if status is not None:
            self.set_status(agent_id, status)
        return agent

    def set_status(self, agent_id: str, status: str) -> dict:
        if status not in ("ACTIVE", "PAUSED", "SUSPENDED"):
            raise bad("INVALID_STATUS", "status must be ACTIVE|PAUSED|SUSPENDED")
        agent = self.get_agent(agent_id)
        if status == "ACTIVE" and self.store.users.get(agent["principal_user_id"], {}).get("suspended"):
            raise forbidden("ACCOUNT_SUSPENDED", "this account is suspended — an administrator must reinstate it first")
        agent["status"] = status
        if status != "ACTIVE" and self.execution:
            self.execution.cancel_agent_triggers(agent_id)  # revocation is immediate (spec §12)
        return agent

    def link_channel(self, agent_id: str, channel: str, external_id: str) -> dict:
        if channel not in ("WEB", "WHATSAPP"):
            raise bad("INVALID_CHANNEL", "channel must be WEB or WHATSAPP")
        if not isinstance(external_id, str) or not external_id.strip():
            raise bad("INVALID_CHANNEL", "external_id required")
        external_id = external_id.strip()
        agent = self.get_agent(agent_id)
        for other in self.store.agents.values():
            if other["agent_id"] != agent_id and any(c["channel"] == channel and c["external_id"] == external_id for c in other["channel_identity_map"]):
                raise conflict("CHANNEL_TAKEN", "that channel identity is already linked to another agent")
        if not any(c["channel"] == channel and c["external_id"] == external_id for c in agent["channel_identity_map"]):
            agent["channel_identity_map"].append({"channel": channel, "external_id": external_id})
        return agent

    def find_by_channel(self, channel: str, external_id: str) -> dict | None:
        for a in self.store.agents.values():
            if any(c["channel"] == channel and c["external_id"] == external_id for c in a["channel_identity_map"]):
                return a
        return None

    def references(self, kind: str, ident: str) -> list[dict]:
        """Agents that reference an LLM / MCP / KB — used by the admin UI before deleting."""
        out = []
        for a in self.store.agents.values():
            c = a["config"]
            if kind == "llm" and (c.get("llm_id") == ident or any(x.get("llm_id") == ident for x in c["algorithms"].values())
                                  or (c.get("advisor") or {}).get("llm_id") == ident):
                out.append(a)
            elif kind == "mcp" and any(r.split(":", 1)[0] == ident for r in c["tools"]):
                out.append(a)
            elif kind == "kb" and ident in c["kb_ids"]:
                out.append(a)
        return out
