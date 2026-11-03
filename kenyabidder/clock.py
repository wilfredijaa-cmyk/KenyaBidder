import time


class SystemClock:
    def now(self) -> int:
        return int(time.time() * 1000)


class FakeClock:
    """Deterministic clock for tests and simulations."""

    def __init__(self, t: int = 1_700_000_000_000):
        self.t = t

    def now(self) -> int:
        return self.t

    def advance(self, ms: int) -> int:
        self.t += ms
        return self.t
