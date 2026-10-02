import logging
from collections import defaultdict
from typing import Callable

log = logging.getLogger("kenyabidder.events")


class Events:
    """Tiny synchronous pub/sub. A failing listener must never break the emitter (e.g. the bid path)."""

    def __init__(self):
        self._h: dict[str, list[Callable]] = defaultdict(list)

    def on(self, name: str, fn: Callable) -> None:
        self._h[name].append(fn)

    def emit(self, name: str, **payload) -> None:
        for fn in list(self._h.get(name, ())):
            try:
                fn(**payload)
            except Exception:  # noqa: BLE001
                log.exception("listener for %s failed", name)
