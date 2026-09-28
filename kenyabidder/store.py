import json
import os
import tempfile
from pathlib import Path

MAP_KEYS = ["users", "agents", "auctions", "matches", "triggers", "approvals", "idempotency", "breakers",
            "llms", "mcps", "kbs", "packs"]
LIST_KEYS = ["audit", "notifications", "market_history", "outbox", "reveal_log", "admin_log"]


class Store:
    """In-memory state with atomic JSON snapshots (stand-in for Postgres + Redis, spec §15.1)."""

    def __init__(self):
        for k in MAP_KEYS:
            setattr(self, k, {})
        for k in LIST_KEYS:
            setattr(self, k, [])
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
                setattr(s, k, data[k])
        s.considered = set(data.get("considered", []))
        s.settings = data.get("settings", {})
        return s

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
