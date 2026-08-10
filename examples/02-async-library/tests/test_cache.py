"""The cache — and what it looks like when the seam already exists.

Not one line of this file patches anything. `TTLCache` takes its clock as a constructor argument,
so "make time move" is a method call. Compare with `test_patching.py`, where the same effect on
`random.uniform` costs the whole suite a drain.
"""

from __future__ import annotations

import json
from pathlib import Path

import velox
from velox import Depends

from relay.cache import FakeClock, TTLCache
from tests.fixtures import cache, clock


async def test_hit_before_expiry(
    c: TTLCache = Depends(cache),
    t: FakeClock = Depends(clock),
) -> None:
    c.put("a", b"1")
    t.advance(29.0)

    assert c.get("a") == b"1"


async def test_miss_after_expiry(
    c: TTLCache = Depends(cache),
    t: FakeClock = Depends(clock),
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


# `@velox.parametrize("elapsed,expected", [...], ids=[...])` would collapse the four cases below
# into one test with explicit ids (`fresh`, `just-in-time`, `exactly-ttl`, `just-late`, none
# derived from `hash()` since ids are part of the public, CI-selector surface) -- declared public
# API (spec/01 §9), not yet expanded by the collector into records (M1-PLAN.md), so they're
# separate tests, named the same way the ids would have read, sharing `_assert_expiry_at` above.
async def test_expiry_boundary_fresh(
    c: TTLCache = Depends(cache),
    t: FakeClock = Depends(clock),
) -> None:
    await _assert_expiry_at(0.0, b"1", c, t)


async def test_expiry_boundary_just_in_time(
    c: TTLCache = Depends(cache),
    t: FakeClock = Depends(clock),
) -> None:
    await _assert_expiry_at(29.999, b"1", c, t)


async def test_expiry_boundary_exactly_ttl(
    c: TTLCache = Depends(cache),
    t: FakeClock = Depends(clock),
) -> None:
    await _assert_expiry_at(30.0, None, c, t)


async def test_expiry_boundary_just_late(
    c: TTLCache = Depends(cache),
    t: FakeClock = Depends(clock),
) -> None:
    await _assert_expiry_at(30.001, None, c, t)


async def _assert_ttl_respected(key: str, ttl: float, t: FakeClock) -> None:
    c = TTLCache(ttl=ttl, clock=t)
    c.put(key, b"v")
    t.advance(ttl / 2)

    assert c.get(key) == b"v"


# Stacked `@velox.parametrize("ttl", [1.0, 60.0])` / `@velox.parametrize("key", ["a", "b"])` would
# be the cartesian product, outermost varying slowest: `[1.0-a]`, `[1.0-b]`, `[60.0-a]`, `[60.0-b]`.
# Same not-expanded-yet gap as above; the four combinations are spelled out by hand instead.
async def test_ttl_is_respected_for_key_a_short_ttl(t: FakeClock = Depends(clock)) -> None:
    await _assert_ttl_respected("a", 1.0, t)


async def test_ttl_is_respected_for_key_b_short_ttl(t: FakeClock = Depends(clock)) -> None:
    await _assert_ttl_respected("b", 1.0, t)


async def test_ttl_is_respected_for_key_a_long_ttl(t: FakeClock = Depends(clock)) -> None:
    await _assert_ttl_respected("a", 60.0, t)


async def test_ttl_is_respected_for_key_b_long_ttl(t: FakeClock = Depends(clock)) -> None:
    await _assert_ttl_respected("b", 60.0, t)


async def test_hit_rate(c: TTLCache = Depends(cache)) -> None:
    c.put("a", b"1")
    c.get("a")
    c.get("a")
    c.get("missing")

    assert c.hit_rate == velox.approx(2 / 3)


async def test_stats_can_be_dumped(
    c: TTLCache = Depends(cache),
    tmp: Path = Depends(velox.tmp_path),
) -> None:
    """`tmp_path` is `basetemp/<sanitized-test-id>` — unique by construction, no scan-and-retry."""
    c.put("a", b"1")
    c.get("a")

    target = tmp / "stats.json"
    target.write_text(json.dumps({"hits": c.hits, "misses": c.misses}))

    assert json.loads(target.read_text()) == {"hits": 1, "misses": 0}


# `@velox.xfail("...", strict=True, raises=AssertionError)` is the honest mark here -- `raises=`
# would narrow which exception counts as expected (an AssertionError; a TypeError would still fail
# the run, the difference between xfail as a to-do list and xfail as a place bugs go to hide) --
# but it's declared, not enacted: `_run.py`'s `Outcome` enum has no `XFAILED` yet (M1-PLAN.md), so
# it would just report plain `FAILED`. `skip` is wired end to end; remove this once eviction is
# implemented, don't wait for xfail to flip it red automatically.
@velox.skip("cache does not evict on size yet (unbounded growth, not an AssertionError)")
async def test_evicts_when_full(c: TTLCache = Depends(cache)) -> None:
    for i in range(10_000):
        c.put(f"k{i}", b"v")

    assert len(c._entries) <= 1_000
