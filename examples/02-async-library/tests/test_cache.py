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


@velox.parametrize(
    "elapsed,expected",
    [(0.0, b"1"), (29.999, b"1"), (30.0, None), (30.001, None)],
    ids=["fresh", "just-in-time", "exactly-ttl", "just-late"],
)
async def test_expiry_boundary(
    elapsed: float,
    expected: bytes | None,
    c: TTLCache = Depends(cache),
    t: FakeClock = Depends(clock),
) -> None:
    """Explicit `ids=` when the generated ones would not read well.

    Generated ids follow pytest's rules (literals for str/int/bool/None/enum, `argname0`/`argname1`
    for everything else), so `0.0-b'1'` is what you would otherwise get. Ids are part of the public
    surface — they end up in CI selectors — so they are stable and never derived from `hash()`.
    """
    c.put("a", b"1")
    t.advance(elapsed)

    assert c.get("a") == expected


@velox.parametrize("ttl", [1.0, 60.0])
@velox.parametrize("key", ["a", "b"])
async def test_ttl_is_respected_per_instance(
    key: str,
    ttl: float,
    t: FakeClock = Depends(clock),
) -> None:
    """Stacked parametrize is the cartesian product, outermost varying slowest.

    Four tests: `[1.0-a]`, `[1.0-b]`, `[60.0-a]`, `[60.0-b]`. The order is defined, not incidental.
    """
    c = TTLCache(ttl=ttl, clock=t)
    c.put(key, b"v")
    t.advance(ttl / 2)

    assert c.get(key) == b"v"


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


@velox.xfail("cache does not evict on size yet", strict=True, raises=AssertionError)
async def test_evicts_when_full(c: TTLCache = Depends(cache)) -> None:
    """`raises=` narrows which exception counts as expected.

    An `AssertionError` here is the expected failure. A `TypeError` is a real bug and still fails
    the run, which is the difference between xfail as a to-do list and xfail as a place bugs go to
    hide.
    """
    for i in range(10_000):
        c.put(f"k{i}", b"v")

    assert len(c._entries) <= 1_000
