# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""One token bucket, used by every service here.

Both the book and the oracle ration unauthenticated requests, and they must:
neither can require an account, because the people who need them most -- a
borrower recovering their own collateral, an auditor checking a seizure -- have
none. So the limit is the whole of the defence, and it lives in one file rather
than as a copy in each daemon, where one copy grows a fix the other never gets.

Two properties are what make it a defence rather than a gesture:

  * it FORGETS. A bucket keyed by client address that is never removed is a map
    an attacker can grow without bound by varying the address, which is the
    denial of service the limiter was put there to prevent.

  * it can be paired with a bucket for everyone TOGETHER. A per-client limit
    alone is defeated by spreading a flood over many source addresses, which
    costs an attacker nothing and is the ordinary shape of one.
"""

import threading


class RateLimiter:
    """A token bucket per client: `burst` requests at once, refilling at
    `rate` a second."""

    def __init__(self, rate=1.0, burst=20, max_keys=10_000):
        self.rate, self.burst = float(rate), float(burst)
        self.max_keys = int(max_keys)
        self._buckets = {}
        self._lock = threading.Lock()
        self._calls = 0

    def allow(self, key, now):
        """True if this client may make one request now, spending a token."""
        with self._lock:
            self._calls += 1
            tokens, last = self._buckets.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            allowed = tokens >= 1.0
            self._buckets[key] = ((tokens - 1.0) if allowed else tokens, now)
            # AFTER the write, so the cap holds at every moment a caller could
            # observe rather than everywhere except just after an insert.
            # Sweeping on a call count rather than on a timer keeps this free
            # of a background thread, and the size test is what catches a burst
            # that arrives faster than a thousand calls' worth of sweeps.
            if self._calls % 1000 == 0 or len(self._buckets) > self.max_keys:
                self._sweep(now)
            return allowed

    def _sweep(self, now):
        """Bound the map, whatever the traffic looks like.

        The first pass forgets buckets that have refilled to full, which is
        free: a full bucket says exactly what a client never seen before says.

        That alone is not a bound, and the case it misses is the attack. A
        flood arriving FASTER than the refill interval -- which is what a flood
        is -- leaves nothing idle, so the pass frees nothing while the map
        keeps growing, and the size test then re-runs this scan on every
        request. Sweeping for nothing turns a flood of memory into a flood of
        CPU as well, so the map is capped outright: over the cap, the buckets
        seen longest ago go, oldest first.

        Evicting a bucket forgives that client, and there is no way around
        that; but a client whose address is new gets a full bucket anyway, so
        an attacker varying addresses gains nothing they did not already have.
        What stops that flood is the second bucket, the one for everyone
        together. This map is a courtesy to honest clients, and the point here
        is that it must not itself become the way in.
        """
        idle = self.burst / self.rate
        for k in [k for k, (_t, last) in self._buckets.items()
                  if now - last >= idle]:
            del self._buckets[k]
        if len(self._buckets) <= self.max_keys:
            return
        oldest = sorted(self._buckets, key=lambda k: self._buckets[k][1])
        for k in oldest[:len(self._buckets) - self.max_keys]:
            del self._buckets[k]
