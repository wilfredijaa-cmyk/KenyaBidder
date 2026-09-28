import json
import os
import tempfile
from pathlib import Path

MAP_KEYS = ["users", "agents", "auctions", "matches", "triggers", "approvals", "idempotency", "breakers",
            "llms", "mcps", "kbs"]
LIST_KEYS = ["audit", "notifications", "market_history", "outbox", "reveal_log"]


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

    def save(self, path: str | os.PathLike) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.snapshot(), f, default=str)
            os.chmod(tmp, 0o600)  # the snapshot may contain LLM/MCP credentials
            os.replace(tmp, p)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Store":
        p = Path(path)
        if not p.exists():
            return cls()
        return cls.restore(json.loads(p.read_text()))
