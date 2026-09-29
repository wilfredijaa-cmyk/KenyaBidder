import json
import os
import tempfile
from pathlib import Path

MAP_KEYS = ["users", "agents", "auctions", "matches", "triggers", "approvals", "idempotency", "breakers",
            "llms", "mcps", "kbs", "packs", "verifications", "businesses", "disputes", "subscriptions"]
LIST_KEYS = ["audit", "notifications", "market_history", "outbox", "reveal_log", "admin_log", "message_log"]


class Log(list):
    """Append-mostly list. Front trims are counted (``dropped``) so the durable copy stays aligned; every OTHER kind of change
    (in-place edit far from the end, insert, sort, slice assignment…) bumps ``version`` so the durable copy is rewritten wholesale."""

    dropped = 0
    version = 0
    TAIL = 100  # edits to the newest rows are handled by the incremental tail re-check

    def _dirty(self) -> None:
        self.version += 1

    def __delitem__(self, key):
        if isinstance(key, slice):
            n = len(range(*key.indices(len(self))))
            if key.start in (None, 0) and key.step in (None, 1):
                self.dropped += n
            elif n:
                self._dirty()
        elif key in (0, -len(self)):
            self.dropped += 1
        else:
            self._dirty()
        super().__delitem__(key)

    def __setitem__(self, key, value):
        if isinstance(key, slice) or not (-self.TAIL <= (key if key < 0 else key - len(self)) < 0):
            self._dirty()
        super().__setitem__(key, value)

    def clear(self):
        self.dropped += len(self)
        super().clear()

    def pop(self, index=-1):
        if index in (0, -len(self)) and len(self) > 1:
            self.dropped += 1
        elif index not in (-1, len(self) - 1):
            self._dirty()
        return super().pop(index)

    def insert(self, index, obj):
        if index < len(self):
            self._dirty()
        super().insert(index, obj)

    def remove(self, value):
        self._dirty()
        super().remove(value)

    def sort(self, *a, **k):
        self._dirty()
        super().sort(*a, **k)

    def reverse(self):
        self._dirty()
        super().reverse()

    def __iadd__(self, other):
        self.extend(other)
        return self


class Store:
    """In-memory state with atomic JSON snapshots (stand-in for Postgres + Redis, spec §15.1)."""

    def __init__(self):
        for k in MAP_KEYS:
            setattr(self, k, {})
        for k in LIST_KEYS:
            setattr(self, k, Log())
        self.considered: set[str] = set()
        self.rate_windows: dict[str, list[int]] = {}
        self.settings: dict = {}

    def snapshot(self) -> dict:
        out = {k: getattr(self, k) for k in MAP_KEYS + LIST_KEYS}
        out["considered"] = sorted(self.considered)
        out["settings"] = self.settings
        return out

    @classmethod
    def restore(cls, data: dict) -> "Store":
        s = cls()
        for k in MAP_KEYS + LIST_KEYS:
            if k in data:
                setattr(s, k, Log(data[k]) if k in LIST_KEYS else data[k])
        s.considered = set(data.get("considered", []))
        s.settings = data.get("settings", {})
        return s.upgrade()

    def upgrade(self) -> "Store":
        """Bring records written by older versions up to the current shape (idempotent; runs after every load)."""
        for a in self.auctions.values():
            a.setdefault("direction", "FORWARD")
            a.setdefault("poster_agent_id", a.get("seller_agent_id"))
            a.setdefault("verified_only", False)
        for m in self.matches.values():
            m.setdefault("direction", "FORWARD")
        for u in self.users.values():
            u.setdefault("session_version", 0)
        return self

    def prune(self, now: int) -> dict:
        """Bound everything that only ever grew. The scaling path is a real database; until then, nothing may grow forever."""
        n = {}
        if len(self.idempotency) > 20_000:
            for k in list(self.idempotency)[: len(self.idempotency) - 20_000]:  # dicts keep insertion order: oldest first
                del self.idempotency[k]
            n["idempotency"] = True
        for name, cap in (("audit", 50_000), ("notifications", 5_000), ("market_history", 20_000), ("outbox", 1_000), ("admin_log", 5_000), ("reveal_log", 20_000), ("message_log", 5_000)):
            lst = getattr(self, name)
            if len(lst) > cap:
                del lst[: len(lst) - cap]
                n[name] = True
        week = 7 * 24 * 3600_000
        for tid in [t["id"] for t in self.triggers.values() if t["status"] in ("DONE", "CANCELLED", "EXHAUSTED", "BLOCKED") and now - t["created_at"] > week]:
            del self.triggers[tid]
        for aid in [a["id"] for a in self.approvals.values() if a["status"] != "PENDING" and now - (a.get("resolved_at") or a["created_at"]) > 30 * 24 * 3600_000]:
            del self.approvals[aid]
        for vid in [v["id"] for v in self.verifications.values() if now - v["created_at"] > 24 * 3600_000]:
            del self.verifications[vid]  # one-time codes are worthless after their 10 minutes; keep a day for support questions
        fails = self.settings.get("login_failures", {})
        for k in [k for k, ts in fails.items() if not [t for t in ts if now - t < 5 * 60_000]]:
            del fails[k]  # expired lockout entries (incl. names that never existed) must not accumulate
        if len(fails) > 10_000:
            for k in list(fails)[: len(fails) - 10_000]:
                del fails[k]
        return n

    def serialize(self) -> str:
        """Consistent point-in-time JSON (call on the event-loop thread that owns the state)."""
        return json.dumps(self.snapshot(), default=str)

    @staticmethod
    def write_atomic(path: str | os.PathLike, payload: str) -> None:
        """Durable atomic replace; safe to run in a worker thread."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)  # the snapshot may contain LLM/MCP credentials
            os.replace(tmp, p)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def save(self, path: str | os.PathLike) -> None:
        self.write_atomic(path, self.serialize())

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Store":
        p = Path(path)
        if not p.exists():
            return cls()
        return cls.restore(json.loads(p.read_text()))
