"""Token wallet: what users buy, and what their agents' LLM calls consume.

* **Durable**: balances, the append-only ledger and payment orders live in the SQL database (DuckDB, PostgreSQL later),
  each change in one transaction — so a crash can never lose a purchase or resurrect spent tokens.
* **Invariants enforced by the database**: a balance can never go negative (``CHECK``), and a credit for the same
  ``(kind, ref)`` can never be applied twice (unique index) — a replayed webhook is a no-op.
* **Per user, per LLM**: tokens are bought for a specific configured LLM and only spent on that LLM.
* **Gating** (:class:`TokenMeter`): an agent mapped to a *metered* LLM may only run that LLM when its owner holds enough
  tokens. Each provider call reserves a hold first, then settles the provider-reported usage.
"""
from __future__ import annotations

import json
import uuid

from .db import Database, IntegrityError, open_database
from .errors import AppError, bad

LEDGER_COLS = "seq, entry_id, user_id, llm_id, agent_id, kind, tokens, used, balance_after, ref, ts, meta"


class InsufficientTokens(AppError):
    def __init__(self, llm_name: str, needed: int, available: int):
        super().__init__("NO_TOKENS", f"not enough {llm_name} tokens: {available:,} available, about {needed:,} needed for one decision — top up in Wallet", 402)
        self.llm_name, self.needed, self.available = llm_name, needed, available


class AgentRateLimited(AppError):
    def __init__(self, decisions: int, limit: int):
        super().__init__("RATE_LIMITED", f"this agent already made {decisions} LLM decisions in the last hour (limit {limit}) — pausing to protect your tokens", 429)
        self.decisions, self.limit = decisions, limit


class AgentTokenCap(AppError):
    def __init__(self, used: int, cap: int):
        super().__init__("TOKEN_CAP", f"this agent reached its daily token cap ({used:,} of {cap:,} used in the last 24h)", 429)
        self.used, self.cap = used, cap


def _entry(row: dict) -> dict:
    d = dict(row)
    d["at"] = d.pop("ts")  # the column is `ts` (AT is an SQL keyword); the API keeps `at`
    d["meta"] = json.loads(d["meta"] or "{}")
    return d


class WalletDB:
    """Ledger + balances + orders on the portable :class:`~kenyabidder.db.Database` (DuckDB today, PostgreSQL later).

    The database itself enforces the money invariants: ``CHECK (balance >= 0)`` and ``UNIQUE (kind, ref)``."""

    def __init__(self, database: Database | None = None, clock=None):
        self.database = database or open_database(":memory:")
        self.clock = clock

    def close(self) -> None:
        self.database.close()

    def now(self) -> int:
        return self.clock.now() if self.clock else 0

    def tx(self):
        """One atomic write transaction (yields a :class:`~kenyabidder.db.Tx`)."""
        return self.database.tx()

    # ---------- balances ----------

    def balance(self, user_id: str, llm_id: str) -> int:
        return self.database.scalar("SELECT balance FROM balances WHERE user_id=? AND llm_id=?", (user_id, llm_id), 0)

    def balances(self, user_id: str) -> dict[str, int]:
        return {r["llm_id"]: r["balance"] for r in self.database.query("SELECT llm_id, balance FROM balances WHERE user_id=?", (user_id,))}

    @staticmethod
    def _bal(c, user_id: str, llm_id: str) -> int:
        return c.scalar("SELECT balance FROM balances WHERE user_id=? AND llm_id=?", (user_id, llm_id), 0)

    def _write(self, c, *, user_id, llm_id, kind, delta, used=0, agent_id=None, ref=None, meta=None) -> dict:
        if delta:
            # Relative, single-statement updates: concurrent writers on PostgreSQL serialise on the row instead of overwriting each
            # other's read-modify-write, and the CHECK constraint is the final judge of "never negative".
            try:
                row = c.query_one("UPDATE balances SET balance = balance + ? WHERE user_id=? AND llm_id=? RETURNING balance", (delta, user_id, llm_id))
                if row is None:
                    if delta < 0:
                        raise IntegrityError("nothing to debit")
                    row = c.query_one("INSERT INTO balances(user_id, llm_id, balance) VALUES(?,?,?) "
                                      "ON CONFLICT (user_id, llm_id) DO UPDATE SET balance = balances.balance + excluded.balance RETURNING balance",
                                      (user_id, llm_id, delta))
                bal = row["balance"]
            except IntegrityError:
                raise bad("NEGATIVE_BALANCE", "operation would make the balance negative") from None
        else:
            bal = self._bal(c, user_id, llm_id)
        entry_id, at = str(uuid.uuid4()), self.now()
        seq = c.query_one("INSERT INTO ledger(entry_id,user_id,llm_id,agent_id,kind,tokens,used,balance_after,ref,ts,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?) RETURNING seq",
                          (entry_id, user_id, llm_id, agent_id, kind, delta, used, bal, ref, at, json.dumps(meta or {})))["seq"]
        return {"seq": seq, "entry_id": entry_id, "user_id": user_id, "llm_id": llm_id, "agent_id": agent_id, "kind": kind,
                "tokens": delta, "used": used, "balance_after": bal, "ref": ref, "at": at, "meta": meta or {}}

    def credit(self, user_id: str, llm_id: str, tokens: int, kind: str, *, ref: str | None = None,
               meta: dict | None = None, c=None) -> dict:
        """Add tokens. Idempotent per (kind, ref): a repeat returns the original entry with ``duplicate=True``.

        Pass ``c`` to join an enclosing :meth:`tx` (e.g. marking an order paid and crediting it atomically).
        """
        if kind not in ("GRANT", "TOPUP", "REFUND", "ADJUST"):
            raise bad("INVALID_KIND", f"cannot credit with kind {kind}")
        if not (isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0):
            raise bad("INVALID_TOKENS", "tokens must be a positive integer")

        def run(conn):
            if ref is not None:
                row = conn.query_one(f"SELECT {LEDGER_COLS} FROM ledger WHERE kind=? AND ref=?", (kind, ref))  # nosec B608
                if row:
                    return {**_entry(row), "duplicate": True}
            return self._write(conn, user_id=user_id, llm_id=llm_id, kind=kind, delta=tokens, ref=ref, meta=meta)

        if c is not None:
            return run(c)
        with self.tx() as conn:
            return run(conn)

    def adjust(self, user_id: str, llm_id: str, delta: int, *, ref: str | None = None, meta: dict | None = None) -> dict:
        """Admin correction, positive or negative. Cannot push the balance below zero."""
        if not (isinstance(delta, int) and not isinstance(delta, bool)) or delta == 0:
            raise bad("INVALID_TOKENS", "delta must be a non-zero integer")
        with self.tx() as c:
            if ref is not None:
                row = c.query_one(f"SELECT {LEDGER_COLS} FROM ledger WHERE kind='ADJUST' AND ref=?", (ref,))  # nosec B608
                if row:
                    return {**_entry(row), "duplicate": True}
            return self._write(c, user_id=user_id, llm_id=llm_id, kind="ADJUST", delta=delta, ref=ref, meta=meta)

    def record_usage(self, user_id: str, llm_id: str, used: int, *, agent_id: str | None, metered: bool, meta: dict | None = None) -> dict:
        """Debit consumed tokens. Never overdraws: if the balance is short the balance is zeroed and the shortfall recorded."""
        used = max(0, int(used))
        for attempt in range(3):
            try:
                with self.tx() as c:
                    debit = min(self._bal(c, user_id, llm_id), used) if metered else 0
                    m = dict(meta or {})
                    if metered and debit < used:
                        m["shortfall"] = used - debit
                    return self._write(c, user_id=user_id, llm_id=llm_id, kind="USAGE", delta=-debit, used=used, agent_id=agent_id, meta=m)
            except AppError as e:
                if e.code != "NEGATIVE_BALANCE" or attempt == 2:
                    raise  # a concurrent debit drained the balance between our read and our write: re-read and try again
        raise AssertionError("unreachable")

    # ---------- reads ----------

    def ledger(self, *, user_id: str | None = None, agent_id: str | None = None, kind: str | None = None, limit: int = 100) -> list[dict]:
        where, args = [], []
        for col, v in (("user_id", user_id), ("agent_id", agent_id), ("kind", kind)):
            if v is not None:
                where.append(f"{col}=?")
                args.append(v)
        sql = f"SELECT {LEDGER_COLS} FROM ledger" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY seq DESC LIMIT ?"  # nosec B608
        return [_entry(r) for r in self.database.query(sql, (*args, max(1, min(limit, 100_000))))]

    def used_since(self, agent_id: str, since_ms: int) -> int:
        return int(self.database.scalar("SELECT COALESCE(SUM(used),0) FROM ledger WHERE agent_id=? AND kind='USAGE' AND ts>=?", (agent_id, since_ms), 0))

    def usage_by_agent(self, user_id: str, since_ms: int = 0) -> list[dict]:
        rows = self.database.query("SELECT agent_id, llm_id, COUNT(*) AS calls, SUM(used) AS used FROM ledger "
                                   "WHERE user_id=? AND kind='USAGE' AND ts>=? GROUP BY agent_id, llm_id ORDER BY used DESC", (user_id, since_ms))
        return [{**r, "calls": int(r["calls"]), "used": int(r["used"])} for r in rows]

    def has_signup_grant(self, user_id: str) -> bool:
        return self.database.scalar("SELECT 1 FROM ledger WHERE user_id=? AND kind='GRANT' AND ref LIKE 'signup:%' LIMIT 1", (user_id,)) is not None

    def signup_grants_since(self, since_ms: int) -> int:
        """How many distinct signup grants were issued recently (the platform-wide free-trial budget)."""
        return int(self.database.scalar("SELECT COUNT(DISTINCT user_id) FROM ledger WHERE kind='GRANT' AND ref LIKE 'signup:%' AND ts>=?", (since_ms,), 0))

    def recent_usage_avg(self, agent_id: str, n: int = 20) -> int | None:
        rows = self.database.query("SELECT used FROM ledger WHERE agent_id=? AND kind='USAGE' AND used>0 ORDER BY seq DESC LIMIT ?", (agent_id, n))
        return round(sum(r["used"] for r in rows) / len(rows)) if rows else None

    def token_totals(self, since_ms: int = 0) -> dict[str, dict[str, int]]:
        """Per LLM: tokens sold (TOPUP), granted, used and refunded — the inputs of the revenue/margin report."""
        rows = self.database.query("SELECT llm_id, kind, SUM(tokens) AS t, SUM(used) AS u FROM ledger WHERE ts>=? GROUP BY llm_id, kind", (since_ms,))
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            d = out.setdefault(r["llm_id"], {"sold": 0, "granted": 0, "used": 0, "adjusted": 0, "refunded": 0})
            t, u = int(r["t"]), int(r["u"])
            if r["kind"] == "TOPUP":
                d["sold"] += t
            elif r["kind"] == "GRANT":
                d["granted"] += t
            elif r["kind"] == "USAGE":
                d["used"] += u
            elif r["kind"] == "ADJUST":
                d["adjusted"] += t
            elif r["kind"] == "REFUND":
                d["refunded"] += t
        return out

    def outstanding_liability(self) -> dict[str, int]:
        """Tokens customers hold but have not used, per LLM (owed service)."""
        return {r["llm_id"]: int(r["total"]) for r in self.database.query("SELECT llm_id, SUM(balance) AS total FROM balances GROUP BY llm_id")}

    def verify_integrity(self) -> list[str]:
        """Recompute every balance from the ledger and report mismatches (run on startup and from the admin console)."""
        sums = {(r["user_id"], r["llm_id"]): int(r["total"]) for r in self.database.query("SELECT user_id, llm_id, SUM(tokens) AS total FROM ledger GROUP BY user_id, llm_id")}
        bals = {(r["user_id"], r["llm_id"]): int(r["balance"]) for r in self.database.query("SELECT user_id, llm_id, balance FROM balances")}
        problems: list[str] = []
        for k in sums.keys() | bals.keys():
            if sums.get(k, 0) != bals.get(k, 0):
                problems.append(f"{k[0][:8]}/{k[1][:8]}: ledger says {sums.get(k, 0)}, balance says {bals.get(k, 0)}")
        return problems


# ---------------------------------------------------------------------------------------------
# Metering
# ---------------------------------------------------------------------------------------------

def billing_of(entry: dict) -> dict:
    """LLM billing settings with defaults (entries saved before billing existed have none)."""
    return {"billing_mode": entry.get("billing_mode", "metered"), "output_multiplier": entry.get("output_multiplier", 1),
            "cost_per_1k_kes": entry.get("cost_per_1k_kes", 0.0)}


def estimate_tokens(text: str) -> int:
    """Deliberately pessimistic (3 chars/token) — used for holds and when a provider omits usage."""
    return len(text) // 3 + 1


class Decision:
    """One LLM decision (possibly several provider calls). Spends hourly allowance on its first successful reservation."""

    def __init__(self, meter: "TokenMeter", agent_id: str):
        self.meter, self.agent_id, self.committed, self.closed = meter, agent_id, False, False

    def commit(self) -> None:
        if not self.committed and not self.closed:
            self.committed = True
            self.meter._inflight[self.agent_id] = max(0, self.meter._inflight.get(self.agent_id, 0) - 1)
            self.meter._decisions.setdefault(self.agent_id, []).append(self.meter.clock.now())

    def finish(self) -> None:
        if not self.closed:
            self.closed = True
            if not self.committed:
                self.meter._inflight[self.agent_id] = max(0, self.meter._inflight.get(self.agent_id, 0) - 1)


class Reservation:
    """A hold on some of a user's tokens for one in-flight provider call."""

    def __init__(self, meter: "TokenMeter", user_id: str, llm_id: str, hold: int, agent_id: str | None, metered: bool):
        self.meter, self.user_id, self.llm_id, self.hold, self.agent_id, self.metered = meter, user_id, llm_id, hold, agent_id, metered
        self.done = False

    def release(self) -> None:
        if not self.done:
            self.done = True
            self.meter._drop_hold(self.user_id, self.llm_id, self.hold)

    def settle(self, used: int, meta: dict | None = None) -> dict | None:
        if self.done:
            return None
        self.release()
        entry = self.meter.db.record_usage(self.user_id, self.llm_id, used, agent_id=self.agent_id, metered=self.metered, meta=meta)
        self.meter._after_usage(self.user_id, self.llm_id, self.agent_id, entry)
        return entry


class TokenMeter:
    """Gate + meter every LLM call made on behalf of an agent."""

    LOW_BALANCE_INTERVAL_MS = 12 * 3600_000
    INPUT_ALLOWANCE = 2_000  # tokens assumed for the prompt when computing the minimum balance to operate

    def __init__(self, store, clock, db: WalletDB, llms, notify=None):
        self.store, self.clock, self.db, self.llms = store, clock, db, llms
        self.notify = notify or (lambda *a, **k: None)
        self.limit_boost = None  # fn(user_id) -> int | None: hourly decision allowance granted by a subscription plan
        self._holds: dict[tuple[str, str], int] = {}
        self._low_notified: dict[tuple[str, str], int] = {}
        self._decisions: dict[str, list[int]] = {}
        self._inflight: dict[str, int] = {}

    # ----- policy / config -----

    @property
    def policy(self) -> str:
        """``block``: an agent without tokens stops making LLM decisions. ``fallback``: it uses the deterministic heuristic."""
        return self.store.settings.get("token_policy", "block")

    def is_metered(self, entry: dict) -> bool:
        return billing_of(entry)["billing_mode"] == "metered"

    def min_balance(self, entry: dict) -> int:
        """Smallest balance at which one more provider call is allowed."""
        return self.INPUT_ALLOWANCE + entry["max_tokens"] * billing_of(entry)["output_multiplier"]

    def _drop_hold(self, user_id: str, llm_id: str, hold: int) -> None:
        k = (user_id, llm_id)
        left = self._holds.get(k, 0) - hold
        if left > 0:
            self._holds[k] = left
        else:
            self._holds.pop(k, None)

    def available(self, user_id: str, llm_id: str) -> int:
        return max(0, self.db.balance(user_id, llm_id) - self._holds.get((user_id, llm_id), 0))

    # ----- reservation -----

    def reserve(self, user_id: str, entry: dict, hold: int, agent_id: str | None = None) -> Reservation:
        llm_id = entry["id"]
        metered = self.is_metered(entry)
        if agent_id:
            agent = self.store.agents.get(agent_id) or {}
            cap = agent.get("config", {}).get("max_tokens_per_day")
            if cap:
                used = self.db.used_since(agent_id, self.clock.now() - 24 * 3600_000)
                if used >= cap:
                    raise AgentTokenCap(used, cap)
        if metered:
            avail = self.available(user_id, llm_id)
            if avail < hold:
                raise InsufficientTokens(entry["name"], hold, avail)
            self._holds[(user_id, llm_id)] = self._holds.get((user_id, llm_id), 0) + hold
        return Reservation(self, user_id, llm_id, hold if metered else 0, agent_id, metered)

    def decisions_last_hour(self, agent_id: str) -> int:
        """Decisions that reached the LLM in the last hour, plus those currently in flight."""
        now = self.clock.now()
        w = [t for t in self._decisions.get(agent_id, []) if now - t < 3600_000]
        self._decisions[agent_id] = w
        return len(w) + self._inflight.get(agent_id, 0)

    def hourly_limit(self, agent_id: str) -> int:
        """Platform limit, raised by the owner's subscription plan (0 = unlimited stays unlimited)."""
        base = self.store.settings.get("max_llm_decisions_per_hour", 60)
        agent = self.store.agents.get(agent_id)
        boost = self.limit_boost(agent["principal_user_id"]) if (self.limit_boost and agent) else None
        return max(base, boost) if (base and boost) else base

    def begin_decision(self, agent_id: str) -> "Decision":
        """Call ONCE per LLM decision, before any provider call. In-flight decisions are counted immediately (so a burst of
        concurrent listings cannot slip past), but a decision only *spends* allowance when it actually reaches the LLM — being
        blocked for lack of tokens must not burn the hourly budget. Always call ``finish()`` on the returned Decision."""
        limit = self.hourly_limit(agent_id)
        n = self.decisions_last_hour(agent_id)
        if limit and n >= limit:
            raise AgentRateLimited(n, limit)
        self._inflight[agent_id] = self._inflight.get(agent_id, 0) + 1
        return Decision(self, agent_id)

    def wrap(self, provider, entry: dict, agent: dict, purpose: str = "decision", decision: "Decision | None" = None) -> "MeteredProvider":
        return MeteredProvider(provider, self, entry, agent["principal_user_id"], agent["agent_id"], purpose, decision)

    # ----- status (for UI and pre-checks) -----

    def llm_ids_for(self, agent: dict) -> list[str]:
        cfg = agent["config"]
        ids: list[str] = []
        for spec in cfg["algorithms"].values():  # bidders: per auction type; suppliers: per RFQ type
            if spec["strategy"] == "llm":
                ids.append(spec.get("llm_id") or cfg.get("llm_id"))
        if agent["agent_type"] == "SELLER":
            adv = cfg.get("advisor") or {}
            if adv.get("strategy") == "llm":
                ids.append(adv.get("llm_id") or cfg.get("llm_id"))
        return list(dict.fromkeys(i for i in ids if i))

    def status_for_agent(self, agent: dict) -> dict:
        """Can this agent currently run its LLM strategies? Deterministic-only agents are always fine."""
        uid, rows, ok = agent["principal_user_id"], [], True
        for llm_id in self.llm_ids_for(agent):
            entry = self.store.llms.get(llm_id)
            if not entry:
                continue
            metered = self.is_metered(entry)
            need = self.min_balance(entry) if metered else 0
            have = self.available(uid, llm_id) if metered else 0
            row_ok = (not metered) or have >= need
            ok = ok and row_ok
            rows.append({"llm_id": llm_id, "name": entry["name"], "metered": metered, "balance": self.db.balance(uid, llm_id) if metered else None,
                         "min_needed": need, "ok": row_ok})
        cap = agent["config"].get("max_tokens_per_day")
        used24 = self.db.used_since(agent["agent_id"], self.clock.now() - 24 * 3600_000) if cap else 0
        cap_hit = bool(cap) and used24 >= cap
        limit = self.hourly_limit(agent["agent_id"])
        rate_hit = bool(limit) and self.decisions_last_hour(agent["agent_id"]) >= limit
        return {"ok": ok and not cap_hit and not rate_hit, "rate_limited": rate_hit, "llms": rows, "cap": cap, "used_24h": used24, "cap_reached": cap_hit,
                "avg_decision_tokens": self.db.recent_usage_avg(agent["agent_id"])}

    # ----- after usage -----

    def _after_usage(self, user_id: str, llm_id: str, agent_id: str | None, entry: dict) -> None:
        llm = self.store.llms.get(llm_id)
        if not llm or not self.is_metered(llm) or not agent_id:
            return
        threshold = self.min_balance(llm) * 3
        bal = entry["balance_after"]
        if bal < threshold:
            k, now = (user_id, llm_id), self.clock.now()
            if now - self._low_notified.get(k, -10**18) >= self.LOW_BALANCE_INTERVAL_MS:
                self._low_notified[k] = now
                self.notify(agent_id, "low_tokens", f"Low token balance: {bal:,} {llm['name']} tokens left. Top up in Wallet so your agent keeps deciding.", llm_id=llm_id)

    def reset_low_balance_alert(self, user_id: str, llm_id: str) -> None:
        self._low_notified.pop((user_id, llm_id), None)


class MeteredProvider:
    """Wraps an LLM provider: reserve → call → settle the provider-reported usage (or a pessimistic estimate)."""

    def __init__(self, inner, meter: TokenMeter, entry: dict, user_id: str, agent_id: str, purpose: str, decision: "Decision | None" = None):
        self.inner, self.meter, self.entry, self.user_id, self.agent_id, self.purpose, self.decision = inner, meter, entry, user_id, agent_id, purpose, decision

    async def complete(self, system, messages, tools, *, max_tokens=1024, temperature=None, force_tool=None):
        mult = billing_of(self.entry)["output_multiplier"]
        prompt = system + json.dumps(messages, default=str) + json.dumps([{"n": t.name, "d": t.description, "s": t.input_schema} for t in tools])
        est_in = estimate_tokens(prompt)
        res = self.meter.reserve(self.user_id, self.entry, est_in + max_tokens * mult, self.agent_id)
        if self.decision:
            self.decision.commit()  # tokens were reserved: this decision is really going to the LLM
        try:
            comp = await self.inner.complete(system, messages, tools, max_tokens=max_tokens, temperature=temperature, force_tool=force_tool)
        except BaseException:  # provider error / timeout / cancellation: the user is not charged
            res.release()
            raise
        u_in, u_out = int(comp.usage.get("input") or 0), int(comp.usage.get("output") or 0)
        if u_in + u_out <= 0:  # provider did not report usage — charge the pessimistic estimate, never zero
            u_in = est_in
            u_out = estimate_tokens(comp.text + json.dumps([c.args for c in comp.tool_calls], default=str))
        res.settle(u_in + u_out * mult, {"purpose": self.purpose, "model": self.entry["model"], "in": u_in, "out": u_out, "mult": mult})
        return comp
