"""
Adaptive concurrency gate for generate_batch_agentic's per-layer worker
pool: ramps the number of tasks allowed to run their LLM conversation at
once up by 1 after each success, and steps it down by 2 (floor 1) the
moment a rate-limit response is seen — rather than a fixed worker count
that either leaves throughput on the table or floods the provider.
"""
from __future__ import annotations

import threading


class AdaptiveConcurrencyLimiter:
    def __init__(self, start: int = 1, ceiling: int = 16, floor: int = 1):
        if start < floor:
            raise ValueError(f"start ({start}) must be >= floor ({floor})")
        self._cond = threading.Condition()
        self.limit = start
        self.ceiling = ceiling
        self.floor = floor
        self.active = 0
        # Monotonic counter a caller can snapshot before starting a unit of
        # work and compare after: if it moved, a rate limit was seen
        # *during* that work, so the caller should skip its own
        # record_success() rather than immediately ramping back up and
        # cancelling out the step-down that already happened.
        self.rate_limit_events = 0

    def acquire(self) -> None:
        with self._cond:
            while self.active >= self.limit:
                self._cond.wait()
            self.active += 1

    def release(self) -> None:
        with self._cond:
            self.active -= 1
            self._cond.notify_all()

    def record_success(self) -> None:
        """Ramp up by 1, bounded by ceiling. Called once per task that
        finishes without hitting a rate limit — deliberately task-grained,
        not per-LLM-turn, so a single multi-turn task can't itself blow
        through the ceiling before anything has actually proven it can
        sustain that load."""
        with self._cond:
            if self.limit < self.ceiling:
                self.limit += 1
                self._cond.notify_all()

    def record_rate_limited(self) -> None:
        """Step down by 2 (never below floor). Only lowers the ceiling on
        new acquisitions — already-running tasks are not interrupted."""
        with self._cond:
            self.limit = max(self.floor, self.limit - 2)
            self.rate_limit_events += 1
