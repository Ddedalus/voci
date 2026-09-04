"""The cache — and what it looks like when the seam already exists.

Not one line of this file patches anything. `TTLCache` takes its clock as a constructor argument,
so "make time move" is a method call. Compare with `test_patching.py`, where the same effect on
`random.uniform` costs the whole suite a drain.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from relay.cache import FakeClock, TTLCache
from tests.fixtures import cache, clock

import velox
from velox import Depends


async def test_hit_before_expiry(
    c: Annotated[TTLCache, Depends(cache)],
    t: Annotated[FakeClock, Depends(clock)],
) -> None:
    c.put("a", b"1")
    t.advance(29.0)

    assert c.get("a") == b"1"


async def test_miss_after_expiry(
    c: Annotated[TTLCache, Depends(cache)],
    t: Annotated[FakeClock, Depends(clock)],
) -> None:
    c.put("a", b"1")
    t.advance(31.0)

    assert c.get("a") is None


async def _assert_expiry_at(
    elapsed: float, expected: bytes | None, c: TTLCache, t: FakeClock
) -> None:
    c.put("a", b"1")
    t.advance(elapsed)

    assert c.get("a") == expected


# Each boundary is its own test, sharing `_assert_expiry_at` above.
async def test_expiry_boundary_fresh(
    c: Annotated[TTLCache, Depends(cache)],
    t: Annotated[FakeClock, Depends(clock)],
) -> None:
    await _assert_expiry_at(0.0, b"1", c, t)


async def test_expiry_boundary_just_in_time(
    c: Annotated[TTLCache, Depends(cache)],
    t: Annotated[FakeClock, Depends(clock)],
) -> None:
    await _assert_expiry_at(29.999, b"1", c, t)


async def test_expiry_boundary_exactly_ttl(
    c: Annotated[TTLCache, Depends(cache)],
    t: Annotated[FakeClock, Depends(clock)],
) -> None:
    await _assert_expiry_at(30.0, None, c, t)


async def test_expiry_boundary_just_late(
    c: Annotated[TTLCache, Depends(cache)],
    t: Annotated[FakeClock, Depends(clock)],
) -> None:
    await _assert_expiry_at(30.001, None, c, t)


async def _assert_ttl_respected(key: str, ttl: float, t: FakeClock) -> None:
    c = TTLCache(ttl=ttl, clock=t)
    c.put(key, b"v")
    t.advance(ttl / 2)

    assert c.get(key) == b"v"


# The four combinations of key and ttl are spelled out by hand.
async def test_ttl_is_respected_for_key_a_short_ttl(
    t: Annotated[FakeClock, Depends(clock)],
) -> None:
    await _assert_ttl_respected("a", 1.0, t)


async def test_ttl_is_respected_for_key_b_short_ttl(
    t: Annotated[FakeClock, Depends(clock)],
) -> None:
    await _assert_ttl_respected("b", 1.0, t)


async def test_ttl_is_respected_for_key_a_long_ttl(t: Annotated[FakeClock, Depends(clock)]) -> None:
    await _assert_ttl_respected("a", 60.0, t)


async def test_ttl_is_respected_for_key_b_long_ttl(t: Annotated[FakeClock, Depends(clock)]) -> None:
    await _assert_ttl_respected("b", 60.0, t)


async def test_hit_rate(c: Annotated[TTLCache, Depends(cache)]) -> None:
    c.put("a", b"1")
    c.get("a")
    c.get("a")
    c.get("missing")

    assert c.hit_rate == velox.approx(2 / 3)


async def test_stats_can_be_dumped(
    c: Annotated[TTLCache, Depends(cache)],
    tmp: Annotated[Path, Depends(velox.tmp_path)],
) -> None:
    """`tmp_path` is `basetemp/<sanitized-test-id>` — unique by construction, no scan-and-retry."""
    c.put("a", b"1")
    c.get("a")

    target = tmp / "stats.json"
    target.write_text(json.dumps({"hits": c.hits, "misses": c.misses}))

    assert json.loads(target.read_text()) == {"hits": 1, "misses": 0}


@velox.skip("cache does not evict on size yet (unbounded growth, not an AssertionError)")
async def test_evicts_when_full(c: Annotated[TTLCache, Depends(cache)]) -> None:
    for i in range(10_000):
        c.put(f"k{i}", b"v")

    assert len(c._entries) <= 1_000
