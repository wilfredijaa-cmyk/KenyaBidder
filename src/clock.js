export class SystemClock {
  now() {
    return Date.now();
  }
}

/** Deterministic clock for tests and simulations. */
export class FakeClock {
  constructor(t = 1_700_000_000_000) {
    this.t = t;
  }
  now() {
    return this.t;
  }
  advance(ms) {
    this.t += ms;
    return this.t;
  }
}
