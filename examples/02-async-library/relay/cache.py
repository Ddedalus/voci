"""A TTL cache whose clock is a constructor argument.

`clock` defaults to `time.monotonic` and is injectable. That one parameter is the difference
between a test that runs concurrently with everything else and a test that patches
`time.monotonic` globally, drains the entire suite, and runs alone.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(slots=True)
class _Entry:
    value: bytes
    expires_at: float


@dataclass(slots=True)
class TTLCache:
    ttl: float
    clock: Callable[[], float] = time.monotonic
    _entries: dict[str, _Entry] = field(default_factory=dict, repr=False)
    hits: int = 0
    misses: int = 0

    def get(self, key: str) -> bytes | None:
        entry = self._entries.get(key)
        if entry is None or entry.expires_at <= self.clock():
            self.misses += 1
            self._entries.pop(key, None)
            return None
        self.hits += 1
        return entry.value

    def put(self, key: str, value: bytes) -> None:
        self._entries[key] = _Entry(value, self.clock() + self.ttl)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return 0.0 if total == 0 else self.hits / total


@dataclass(slots=True)
class FakeClock:
    """A clock you can drive. No patching, because the seam already exists."""

    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
