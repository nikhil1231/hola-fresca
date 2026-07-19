"""Adaptive throttle for the fetch stage.

The strategy is "start cheeky, back off on resistance": a fixed pool of workers
runs concurrently, and throughput is governed by a single shared inter-request
delay that starts at zero. Every worker waits ``delay`` seconds before issuing a
request. On a rate-limit/server signal (HTTP 429/5xx or a timeout) the delay is
raised multiplicatively; sustained success decays it back toward zero. Resizing
one shared delay is simpler and smoother than juggling a dynamic semaphore.

IP bans are out of scope by design (the operator runs behind a VPN), so the
floor is genuinely 0 — we lean on the server's own signals rather than a fixed
politeness delay.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class AdaptiveThrottle:
    #: Concurrent workers pulling from the fetch queue.
    workers: int = 24
    #: Current shared delay (seconds) applied before each request.
    delay: float = 0.0
    #: Delay never grows beyond this.
    max_delay: float = 8.0
    #: Multiplier applied to the delay on each failure signal.
    backoff_factor: float = 1.7
    #: Additive kick so the delay can climb from exactly zero.
    backoff_floor: float = 0.25
    #: Multiplier applied to the delay after each success.
    recover_factor: float = 0.92

    def __post_init__(self) -> None:
        self._lock = asyncio.Lock()

    async def before_request(self) -> None:
        """Pace a request by sleeping for the current shared delay."""
        delay = self.delay
        if delay > 0:
            await asyncio.sleep(delay)

    async def on_success(self) -> None:
        async with self._lock:
            if self.delay > 0:
                self.delay = max(0.0, self.delay * self.recover_factor - 0.01)

    async def on_throttle(self) -> None:
        """Register a rate-limit/server-error signal and slow down."""
        async with self._lock:
            self.delay = min(self.max_delay, self.delay * self.backoff_factor + self.backoff_floor)
