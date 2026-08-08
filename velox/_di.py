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
    # Review: no `param_key` component, which spec/04 §4 calls out by name as the one thing to
    # get in on day one — "Param keys are part of the cache key from day one, even though
    # parametrized fixtures are roadmap — retrofitting a cache key is exactly the kind of change
    # this spec exists to avoid." Every key shape below is fixed-arity and positional, and
    # `setup`/`release`/`aclose` all thread the tuple around opaquely, so adding a slot later means
    # touching all four scope arms plus every test that spells a key literally (`("k",)`,
    # `("function", 1, "t")` in tests/test_di.py). A trailing `param_key: object = None` argument
    # appended to each tuple is inert today and free later.
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
        # Review: the refcount is incremented *after* `await entry.future`, but spec/04 §4's own
        # pseudocode — the block this docstring is quoting — increments *before* it
        # (`self._refcounts[key] += 1; return await fut`). That ordering is load-bearing and the
        # inversion is a use-after-teardown the moment tests run concurrently. Verified
        # interleaving, two tasks sharing one `module`-scope key:
        #   1. A calls `acquire`, creates the entry, suspends inside `build()` (any fixture that
        #      awaits — a DB connect — does this).
        #   2. B calls `acquire`, finds the entry, parks on the *pending* future. B's refcount
        #      contribution does not exist yet.
        #   3. A's build finishes; A resumes, `await entry.future` returns without yielding (a
        #      done Future never suspends), refcount = 1, A runs its test and its teardown. If the
        #      test body has no real suspension point — `async def test(): assert x == 1` — A gets
        #      all the way to `release` without ever handing control back, so B is still parked.
        #   4. `release` sees refcount 0, deletes the entry, and awaits the closer.
        #   5. B finally wakes, is handed the value of an instance whose teardown has already run,
        #      and increments the refcount on an orphaned `_Entry` no longer in `self._entries`.
        #      B's own `release` then raises `KeyError` (confirmed) — which `_release_all` turns
        #      into a teardown `ERROR` on an innocent test.
        # Moving `entry.refcount += 1` above the `await` (reserving before waiting, exactly as the
        # spec block has it) closes all of it: refcount can no longer hit zero while a waiter is
        # outstanding. Benign today only because `run_suite` awaits each test to completion.
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
        # Review: "A no-op for a `key` this store never successfully acquired" is false in both
        # readings, and nothing tests either (see tests/test_di.py's
        # `test_release_is_a_no_op_for_a_key_that_was_never_successfully_acquired`, which never
        # calls `release` at all).
        # (1) A key the store has genuinely never seen raises `KeyError` here, not a no-op —
        #     verified: `await ScopeStore().release(("nope",))` → `KeyError: ('nope',)`.
        # (2) A key whose `build()` *raised* is still sitting in `self._entries` (`acquire` inserts
        #     the entry before calling `build` and never removes it on the failure path), with
        #     `refcount == 0` and `closer is None`. `release` on it therefore drives the refcount
        #     to **-1**, falls through the guard below, and `del`s the entry — silently discarding
        #     the cached exception that the whole "one comprehensible error, not 400 identical
        #     ones" design depends on. The *next* test to ask for that same module/session key
        #     re-runs the broken fixture from scratch instead of replaying it.
        # The failed-entry leak is also unbounded in the other direction: for `function` scope the
        # key embeds `test_id`, so a suite where a fixture fails for 400 tests accumulates 400 dead
        # entries that only `aclose` ever walks.
        entry = self._entries[key]
        entry.refcount -= 1
        if entry.refcount > 0 or entry.scope == "session":
            return
        # Review: the entry is removed from the dict *before* the closer is awaited, so the whole
        # teardown runs with the key absent from the cache. Under concurrency that window is a
        # second live instance: any task that calls `acquire(key, ...)` while `entry.closer()` is
        # suspended (an async fixture doing `await conn.close()`) misses the cache, creates a fresh
        # entry, and constructs a *second* `module`/`session` instance overlapping the first one's
        # teardown — two engines, two temp schemas, two bound ports, for a scope whose entire
        # contract is "exactly one". Observed already in a scratch run of two gathered tasks. The
        # fix has to keep the key visible (or a tombstone) until the closer has finished.
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
        # Review: the conclusion (reverse insertion order is a valid teardown order) holds today,
        # but the reason the docstring gives for it is backwards, which matters because the stated
        # reason is what a future change will be checked against. `acquire` inserts the entry into
        # `self._entries` *before* awaiting `build()`, not after — so if a fixture's construction
        # ever did acquire its own dependencies, the dependent's entry would land in the dict
        # *first* and reversal would tear the dependency down before the dependent. The real
        # guarantee is external to this class: `setup` walks the already-flattened `plan.steps`
        # forwards and hands `build` a `kwargs` dict of values it has *already* acquired, so
        # `build()` never re-enters `acquire`. That is an invariant of `_di.setup`, and the
        # roadmap item that breaks it is named in spec/04 §9 — `Depends(p, lazy=True)` yielding a
        # factory that resolves on first use is precisely a `build`-time (or later) `acquire`.
        # Worth restating the justification here as "because `setup` pre-resolves every argument",
        # so whoever adds lazy fixtures sees what they are invalidating.
        errors: list[BaseException] = []
        for key in reversed(list(self._entries)):
            entry = self._entries.pop(key, None)
            if entry is None or entry.closer is None:
                continue
            try:
                await entry.closer()
            except BaseException as exc:
                # Review: same `BaseException`-into-a-group problem as `_release_all` below, with a
                # worse landing site — `run_suite` *swallows* whatever `aclose` raises. Verified:
                # a session fixture whose teardown raises `KeyboardInterrupt` produces a run that
                # returns normally, reports its test `PASSED`, prints the group to stderr, and
                # exits `0`. Ctrl-C landing in session teardown is not hypothetical; it is the
                # single most likely moment for it, since that is the last thing a run does.
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
                # Review: `step.args`' third element — `keyword_only`, carried all the way from
                # `_fixtures.plan_of` through `Injection` and `PlanStep` — is discarded here (`_`)
                # and everything is bound by keyword. Nothing in the package ever reads it (grep:
                # only written, never consumed), and it cannot express the case that actually
                # breaks: a *positional-only* injected parameter is recorded as
                # `keyword_only=False`, indistinguishable from an ordinary positional-or-keyword
                # one, and `func(**kwargs)` then always fails. Verified against a real fixture:
                # `def outer(x: int = Depends(inner), /)` plans cleanly, then dies at construction
                # with `TypeError: outer() got some positional-only arguments passed as keyword
                # arguments: 'x'`, surfacing as a setup `ERROR` on every dependent test with no
                # hint that the `/` is the cause. Same for a test function (see `root_kwargs`
                # below). Either `plan_of` should reject `Depends()` on a positional-only
                # parameter with a `DIError` naming the `/`, or the plan should record a third
                # "positional-only" state and `_construct` bind those positionally.
                kwargs = {name: values[source] for name, source, _ in step.args}
                return await _construct(step.fixture, kwargs)

            values[step.step_id] = await store.acquire(key, step.fixture.scope, step.fixture, build)
            keys[step.step_id] = key
            acquired.append(key)
    except BaseException:
        # Review: "before the triggering exception propagates" is not what happens when the
        # cleanup itself misbehaves. `_release_all` raises a `BaseExceptionGroup` if any closer
        # fails, and it raises it from *inside* this `except` block — so the group replaces the
        # original setup failure as the propagating exception and the `raise` below is never
        # reached. The setup traceback survives only as `__context__` (so `traceback.format_exc`
        # in `_run_one` still prints it, under "During handling of the above exception..."), but
        # the exception type a caller sees, and the first thing a future reporter would headline,
        # becomes "fixture teardown" rather than "the fixture that actually broke". Concretely:
        # `fx_a` yields then raises on teardown, `fx_c` raises on construction — the user is told
        # about `fx_a`. Wrapping the cleanup in its own `try`/`except` and attaching the group to
        # the original (or `raise ... from`) keeps the cause in front.
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
            # Review: this is where `_run_one`'s and `run_suite`'s "KeyboardInterrupt/SystemExit
            # propagate immediately from every phase" claim stops being true. A `BaseException`
            # raised by a fixture's teardown is collected and re-raised as a
            # `BaseExceptionGroup`, and a `BaseExceptionGroup` is not a `KeyboardInterrupt`, so
            # `_run_one`'s `except (KeyboardInterrupt, SystemExit): raise` guard around
            # `_di.teardown` never matches. Verified end to end: a fixture doing `yield 1` then
            # `raise KeyboardInterrupt` gives a normally-returning `run_suite`, the test reported
            # `ERROR`, and the run continuing to the next test — Ctrl-C during teardown is
            # swallowed into a test result. `SystemExit` behaves identically.
            # The same wrapping will eat `asyncio.CancelledError` once the scheduler exists: a
            # test cancelled by `--maxfail`/`asyncio.timeout` whose fixture teardown observes the
            # cancellation will have it converted into a group, so the task reports "teardown
            # error" and does *not* actually cancel — spec/05's `interrupted`/`timeout` outcomes
            # can't be built on top of this as written. Splitting the loop (re-raise
            # `KeyboardInterrupt`/`SystemExit`/`CancelledError` immediately, group only
            # `Exception`) is what spec/04 §5's "errors during teardown are collected into an
            # `ExceptionGroup`" actually means — errors, not control flow.
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

    # Review: the fall-through treats "not one of the three `inspect` predicates" as "plain sync
    # value", so any callable that *returns* an awaitable without being an `async def` is cached
    # as the un-awaited coroutine itself. The realistic shapes: a class with `async def
    # __call__` passed to `@velox.fixture()`, a fixture wrapped by a decorator that returns a
    # sync wrapper delegating to an async body, and `@velox.fixture() def fx(): return
    # client.connect()`. The test then receives a coroutine object where it expected a value —
    # every attribute access fails with an unrelated `AttributeError`, plus a `RuntimeWarning:
    # coroutine ... was never awaited` from a garbage-collection point nowhere near the fixture.
    # spec/04 §1 has `kind: Kind` decided *statically* on the `Fixture` for exactly this reason;
    # deciding it here by `inspect` at construction time (I5 aside) is also what makes it
    # un-diagnosable. Cheapest guard short of that: if the returned value is awaitable, either
    # await it or raise naming the fixture, rather than handing it to the test.
    return func(**kwargs), None
