"""Dependency-injection runtime: single-flight scope store + resolution-plan execution.

The static half — `Fixture`, `Depends`, `ResolutionPlan` — lives in `_fixtures.py`. This module
is the dynamic half spec/04 §4-6 describes: constructing each cache-keyed fixture instance
exactly once, sharing it with every other requester that asks for the same key, and tearing it
down in dependent-before-dependency order once nothing needs it any more.

Design note load-bearing for the rest of this file: `ResolutionPlan.steps` already flattens a
test's *entire transitive* fixture graph into one deduplicated list (`_fixtures.plan_for`), not
just the test's direct dependencies. That means teardown never needs to *cascade* from a
dependent into its dependencies at run time — every fixture the test touches, however deep, is
already its own entry in `steps`, acquired once and released once by the same per-test loop.
Reversing that loop is what gives teardown inversion (spec/04 §5); nothing else has to track it.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, final

from velox._fixtures import Fixture, PlanStep, ResolutionPlan, Scope

__all__ = ["ScopeStore", "setup", "teardown"]

type CacheKey = tuple[object, ...]
type Closer = Callable[[], Awaitable[None]]

#: Monotonic tail of every `"call"`-scope key, so two `Depends(...)` sites on the same call-scope
#: fixture — or the same site resolved twice, once per test that reaches it — never collide and
#: never share a construction. `itertools.count` rather than `id(object())`: cheaper, and a `str`/
#: `int` key sorts and reprs better than an address in a test-failure message.
_call_site_ids = iter(range(2**63))


def key_for(fixture: Fixture[Any], step_id: int, *, test_id: str, module_path: str) -> CacheKey:
    """The cache key one `PlanStep` resolves to, given the test/module it's being built for.

    `step_id` only matters for `"call"` scope (see module docstring); it's threaded through
    unconditionally rather than branched on so callers never need to know which scopes care.
    """
    match fixture.scope:
        case "session":
            return ("session", id(fixture))
        case "module":
            return ("module", id(fixture), module_path)
        case "function":
            return ("function", id(fixture), test_id)
        case "call":
            return ("call", id(fixture), test_id, step_id, next(_call_site_ids))


@final
@dataclass(slots=True)
class _Entry:
    scope: Scope
    fixture: Fixture[Any]
    future: asyncio.Future[Any]
    closer: Closer | None = None
    refcount: int = 0


@final
class ScopeStore:
    """Owns every constructed fixture instance for one run.

    One instance per run (spec/04 §4's "single-flight Futures, refcounted teardown"). `session`-
    scope entries are the one case `release` never tears down on its own — only `aclose`, at the
    very end of the run, does (spec/04 §3's scope table: "End of the run").
    """

    def __init__(self) -> None:
        self._entries: dict[CacheKey, _Entry] = {}

    async def acquire(
        self,
        key: CacheKey,
        scope: Scope,
        fixture: Fixture[Any],
        build: Callable[[], Awaitable[tuple[Any, Closer | None]]],
    ) -> Any:
        """Return the value for `key`, constructing it via `build()` iff this is the first ask.

        "First requester constructs, everyone else awaits" (spec/04 §4): the dict lookup and
        `Future` creation below are one synchronous step with no `await` between them, so two
        concurrent askers can never both decide they're the constructor. A `build()` that raises
        leaves that exception permanently on the `Future` — every later requester of the same key
        replays the identical exception instead of re-running (and re-failing) `build()`, which
        is spec/04 §4's "one comprehensible error, not 400 identical ones." Refcounting only
        happens on the success path: a key nobody ever successfully acquired has nothing that
        needs releasing.
        """
        entry = self._entries.get(key)
        if entry is None:
            entry = self._entries[key] = _Entry(
                scope=scope, fixture=fixture, future=asyncio.get_running_loop().create_future()
            )
            try:
                value, closer = await build()
            except BaseException as exc:
                entry.future.set_exception(exc)
                # The `raise` below is how *this* requester actually observes `exc` — it never
                # goes through `entry.future` itself unless a concurrent or later requester also
                # awaits this key. Without the line below, a key that fails and is never asked
                # for again (the common case: `setup` propagates immediately and the caller
                # doesn't retry the same key) leaves the future's exception permanently
                # unretrieved from asyncio's point of view, which logs a spurious "Future
                # exception was never retrieved" at garbage-collection time. Calling `.exception()`
                # marks it retrieved without clearing it — a later `await entry.future` (the
                # concurrent-waiter and exception-caching cases this store exists for) still sees
                # and raises the same exception.
                entry.future.exception()
                raise
            entry.closer = closer
            entry.future.set_result(value)
        value = await entry.future
        entry.refcount += 1
        return value

    async def release(self, key: CacheKey) -> None:
        """Decrement `key`'s refcount; tear it down (and forget it) if that reached zero.

        A no-op for a `key` this store never successfully acquired — see `acquire`'s docstring on
        why a failed construction never reaches here. `session` scope never tears down through
        this path (only `aclose` does); its refcount still decrements, both for symmetry with
        `acquire` and because a future `--eager-teardown` mode (spec/04 §9 roadmap) needs it to
        already be accurate.
        """
        entry = self._entries[key]
        entry.refcount -= 1
        if entry.refcount > 0 or entry.scope == "session":
            return
        del self._entries[key]
        if entry.closer is not None:
            await entry.closer()

    async def aclose(self) -> None:
        """End-of-run: force-teardown every remaining (necessarily `session`-scope) entry.

        Iterated in *reverse insertion order*. `dict` preserves insertion order, and — because
        `acquire`'s dict-then-build sequencing means a fixture's entry can only ever be created
        after every entry its own construction awaited already exists — that insertion order is
        always a valid dependency-before-dependent topological order across the *whole* run, not
        just within one test's plan. Reversing it therefore gives correct teardown inversion here
        too, the same trick `steps`' own reversal gives `teardown` below.
        """
        errors: list[BaseException] = []
        for key in reversed(list(self._entries)):
            entry = self._entries.pop(key, None)
            if entry is None or entry.closer is None:
                continue
            try:
                await entry.closer()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise BaseExceptionGroup("session-scope teardown", errors)


async def setup(
    plan: ResolutionPlan, store: ScopeStore, *, test_id: str, module_path: str
) -> tuple[dict[str, Any], tuple[CacheKey, ...]]:
    """Construct every step `plan` needs, in order, and return the test function's own kwargs.

    On success, also returns the cache key each step resolved to, in plan (construction) order —
    the caller passes this straight to `teardown` once the test is done. On failure partway
    through, whatever this attempt *did* acquire is released again, in reverse, before the
    triggering exception propagates (spec/05 §3's setup-phase contract: "acquired fixtures are
    released"); a `PlanStep` whose own `acquire` raised was never added to that list; only the
    ones that succeeded are cleaned up here, exactly once each.
    """
    values: dict[int, Any] = {}
    keys: dict[int, CacheKey] = {}
    acquired: list[CacheKey] = []
    try:
        for step in plan.steps:
            key = key_for(step.fixture, step.step_id, test_id=test_id, module_path=module_path)

            async def build(step: PlanStep = step) -> tuple[Any, Closer | None]:
                kwargs = {name: values[source] for name, source, _ in step.args}
                return await _construct(step.fixture, kwargs)

            values[step.step_id] = await store.acquire(key, step.fixture.scope, step.fixture, build)
            keys[step.step_id] = key
            acquired.append(key)
    except BaseException:
        await _release_all(store, reversed(acquired))
        raise

    root_kwargs = {name: values[source] for name, source, _ in plan.root_args}
    ordered_keys = tuple(keys[step.step_id] for step in plan.steps)
    return root_kwargs, ordered_keys


async def teardown(store: ScopeStore, keys: Iterable[CacheKey]) -> None:
    """Release every key `setup` returned, in reverse order — spec/05 §3's teardown phase.

    `keys` is already plan (construction) order; reversing it here is what gives teardown
    inversion (module docstring). Every error is collected rather than short-circuiting the loop
    on the first one, so one broken fixture doesn't hide leaks in the others (spec/04 §5).
    """
    await _release_all(store, reversed(list(keys)))


async def _release_all(store: ScopeStore, keys: Iterable[CacheKey]) -> None:
    errors: list[BaseException] = []
    for key in keys:
        try:
            await store.release(key)
        except BaseException as exc:
            errors.append(exc)
    if errors:
        raise BaseExceptionGroup("fixture teardown", errors)


async def _construct(fixture: Fixture[Any], kwargs: Mapping[str, Any]) -> tuple[Any, Closer | None]:
    """Call `fixture.func`, adapting whichever of the four supported shapes it is.

    Sync callables (plain functions and sync generators) run inline rather than in an executor —
    spec/04 §5's answer to "is blocking teardown special": no, it's a blocking call like any
    other, and the watchdog (spec/11, not built yet) is what will eventually name it.
    """
    func = fixture.func
    if inspect.isasyncgenfunction(func):
        gen = func(**kwargs)
        try:
            value = await anext(gen)
        except StopAsyncIteration:
            raise RuntimeError(f"fixture {fixture.name!r} did not yield a value") from None

        async def closer() -> None:
            try:
                await anext(gen)
            except StopAsyncIteration:
                return
            else:
                raise RuntimeError(f"fixture {fixture.name!r} yielded more than once")
            finally:
                await gen.aclose()

        return value, closer

    if inspect.isgeneratorfunction(func):
        gen = func(**kwargs)
        try:
            value = next(gen)
        except StopIteration:
            raise RuntimeError(f"fixture {fixture.name!r} did not yield a value") from None

        async def closer() -> None:
            try:
                next(gen)
            except StopIteration:
                return
            else:
                raise RuntimeError(f"fixture {fixture.name!r} yielded more than once")
            finally:
                gen.close()

        return value, closer

    if inspect.iscoroutinefunction(func):
        return await func(**kwargs), None

    return func(**kwargs), None
