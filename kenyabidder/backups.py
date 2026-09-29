"""Scheduled, verified database backups with retention (DuckDB file copy; PostgreSQL uses pg_dump / managed backups)."""
from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

from .timeutil import eat

log = logging.getLogger("kenyabidder.backup")
NAME = re.compile(r"^kenyabidder-(\d{8}-\d{6})\.duckdb$")


class BackupService:
    def __init__(self, db, directory: str | os.PathLike, clock, keep: int = 14, every_hours: float = 24):
        self.db, self.dir, self.clock, self.keep, self.every_ms = db, Path(directory), clock, max(int(keep), 1), int(every_hours * 3_600_000)
        self.last_at = 0
        self.last_error: str | None = None

    def _stamp(self) -> str:
        import datetime as dt
        return dt.datetime.fromtimestamp(self.clock.now() / 1000, dt.timezone.utc).strftime("%Y%m%d-%H%M%S")

    def run(self) -> Path:
        """Take a backup now. It is opened and queried before it counts — an unreadable backup is worse than none."""
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        dest = self.dir / f"kenyabidder-{self._stamp()}.duckdb"
        tmp = dest.with_suffix(".partial")
        try:
            self.db.backup(str(tmp))
            from .db.duckdb_backend import DuckDatabase
            probe = DuckDatabase(str(tmp))
            try:
                probe.scalar("SELECT count(*) FROM balances")
                probe.scalar("SELECT max(version) FROM schema_version")
            finally:
                probe.close()
            os.replace(tmp, dest)
        except Exception as e:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            self.last_error = f"{type(e).__name__}: {e}"
            raise
        self.last_at, self.last_error = self.clock.now(), None
        self._prune()
        log.info("backup written: %s", dest)
        return dest

    def due(self) -> bool:
        return self.clock.now() - self.last_at >= self.every_ms

    def list(self) -> list[dict]:
        out = []
        if self.dir.exists():
            for p in sorted(self.dir.iterdir(), reverse=True):
                if NAME.match(p.name):
                    st = p.stat()
                    out.append({"name": p.name, "bytes": st.st_size, "at": int(st.st_mtime * 1000), "when": eat(int(st.st_mtime * 1000)).strftime("%d %b %Y %H:%M EAT")})
        return out

    def _prune(self) -> None:
        for old in self.list()[self.keep:]:
            (self.dir / old["name"]).unlink(missing_ok=True)

    def prime(self) -> None:
        """After a restart, don't back up again immediately if a recent backup exists."""
        files = self.list()
        if files:
            self.last_at = files[0]["at"]


def restore_backup(src: str | os.PathLike, dest: str | os.PathLike) -> None:
    """Offline restore: stop the app, then copy a backup over the database file (the old file is kept aside)."""
    src, dest = Path(src), Path(dest)
    if not src.exists():
        raise FileNotFoundError(src)
    import time
    stamp = time.strftime("%Y%m%d-%H%M%S")  # every restore keeps its own safety copy: a second restore must not destroy the first
    if dest.exists():
        shutil.move(dest, dest.with_name(f"{dest.name}.before-restore-{stamp}"))
    wal = dest.with_name(dest.name + ".wal")
    if wal.exists():  # a leftover write-ahead log belongs to the OLD file and must never be replayed onto the backup
        shutil.move(wal, dest.with_name(f"{dest.name}.wal.before-restore-{stamp}"))
    shutil.copy2(src, dest)
    os.chmod(dest, 0o600)
