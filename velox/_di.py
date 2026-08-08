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
import traceback
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


def key_for(
    fixture: Fixture[Any],
    step_id: int,
    *,
    test_id: str,
    module_path: str,
    param_key: object = None,
) -> CacheKey:
    """The cache key one `PlanStep` resolves to, given the test/module it's being built for.

    `step_id` only matters for `"call"` scope (see module docstring); it's threaded through
    unconditionally rather than branched on so callers never need to know which scopes care.

    # AI: please do not reference spec in docstrings - the code should be self-contained.
    `param_key` is appended to every shape below and defaults to `None` — parametrized fixtures
    are still roadmap (spec/01 §10), but spec/04 §4 is explicit that the param slot belongs in the
    cache key "from day one... retrofitting a cache key is exactly the kind of change this spec
    exists to avoid." Every caller today passes the default, so this is inert until parametrize
    lands and then needs no shape change, just a real value flowing in.
    """
    match fixture.scope:
        case "session":
            return ("session", id(fixture), param_key)
        case "module":
            return ("module", id(fixture), module_path, param_key)
        case "function":
            return ("function", id(fixture), test_id, param_key)
        case "call":
            # AI: this seems like bullshit - isn't just ("call", next(_call_site_ids)) enough?
            return ("call", id(fixture), test_id, step_id, next(_call_site_ids), param_key)


@final
@dataclass(slots=True)
class _Entry:
    # AI: please provide docstring
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
        # Reserve this caller's refcount *before* awaiting the (possibly already-resolved)
        # future, not after — spec/04 §4's own pseudocode has it in this order
        # (`self._refcounts[key] += 1; return await fut`), and the order is load-bearing the
        # moment two askers genuinely overlap: a waiter parked on a still-pending future
        # contributes nothing to the refcount until it wakes, so the constructing caller can
        # acquire -> release -> tear down to zero while the waiter is still suspended, handing it
        # the value of an instance whose closer already ran (and then raising `KeyError` out of
        # the waiter's own eventual `release`, since `release` deletes the entry at refcount
        # zero). Incrementing first closes that: the refcount this `acquire` is about to hand out
        # a reference for is counted before anything else gets a chance to drop it to zero.
        # Benign under M0/M1's sequential `run_suite` (nothing is ever actually in flight at
        # once) — this is exactly the kind of bug that stays invisible until concurrency lands.
        #
        # Undone in the `except` below rather than never taken in the first place: a *waiter*
        # (the branch above was skipped, `entry` already existed) has no synchronous way to know
        # whether the future it's about to await already failed without awaiting it — peeking
        # would just be a second `await`-shaped race. Reserving first and refunding on failure
        # keeps the docstring's "refcounting only happens on the success path" true as an
        # observable outcome while still closing the window for the pending-and-eventually-
        # successful case, which is the one the race above actually depends on.
        entry.refcount += 1
        try:
            return await entry.future
        except BaseException:
            entry.refcount -= 1
            raise

    async def release(self, key: CacheKey) -> None:
        """Decrement `key`'s refcount; tear it down (and forget it) if that reached zero.

        A genuine no-op for a `key` this store never *successfully* acquired: an unknown key
        (never seen, or already fully torn down) and a key whose own `build()` raised both return
        immediately, touching neither a refcount nor the cached entry. `acquire`'s own contract
        keeps a caller that got an exception from ever reaching here with that key (a failed
        `acquire` never increments the refcount `release` would otherwise be undoing — see its
        own `except` clause), so in the normal flow this guard is defense against a caller
        replaying a key by hand (as some of this file's own direct `ScopeStore` tests do) rather
        than something `setup`/`teardown` trigger day to day. It matters anyway: silently
        deleting a failed entry here would discard the cached exception a concurrent or later
        requester of the *same* key still needs to replay (spec/04 §4's "one comprehensible
        error, not 400 identical ones") — `aclose` leaves a failed entry alone for the same
        reason, via its own `entry.closer is None` check.

        Known, deliberate gap: a failed entry that nothing ever calls `release` on (the normal
        case) is never removed until `aclose` sweeps it at end of run. For `function`/`call`
        scope, whose key embeds `test_id`, a fixture failing for every test in a large suite
        leaves one dead entry per test sitting in `self._entries` for the run's duration — real,
        bounded by test count, and not chased further here.

        `session` scope never tears down through this path (only `aclose` does); its refcount
        still decrements, both for symmetry with `acquire` and because a future
        `--eager-teardown` mode (spec/04 §9 roadmap) needs it to already be accurate.
        """
        entry = self._entries.get(key)
        if entry is None or not entry.future.done() or entry.future.exception() is not None:
            return
        entry.refcount -= 1
        if entry.refcount > 0 or entry.scope == "session":
            return
        # The entry stays in `self._entries` until the closer has actually finished (or raised),
        # not before — deleting it up front would let a concurrent `acquire` on the same key race
        # a fresh construction against this teardown, briefly producing two live instances of a
        # scope whose entire contract is "exactly one" (observed in a scratch run of two gathered
        # tasks against a `module`-scope fixture). This narrows the window rather than closing it:
        # a concurrent `acquire` that lands *during* the `await` below still finds the entry,
        # still gets hold of an instance that is mid-teardown, and still hands it out again.
        # Closing that fully needs a third ("tearing down") state a waiter can block on — real
        # surgery belonging with the scheduler, not this slice.
        try:
            if entry.closer is not None:
                await entry.closer()
        finally:
            del self._entries[key]

    async def aclose(self) -> None:
        """End-of-run: force-teardown every remaining (necessarily `session`-scope) entry.

        Iterated in *reverse insertion order*. `dict` preserves insertion order, and the guarantee
        that reversing it gives a valid dependency-before-dependent teardown order is external to
        this class: `_di.setup` walks a `ResolutionPlan`'s already-flattened, already-topologically
        -sorted `steps` forwards and hands each `build()` a `kwargs` dict of values it has
        *already* acquired — `build()` never itself calls back into `acquire`, so every entry a
        fixture's own construction depends on is already in `self._entries` before that fixture's
        own entry is inserted. (It is *not*, notably, because `acquire` inserts the entry before
        awaiting `build()` — that ordering detail is about single-flight construction, not about
        which entries precede which in the dict.) The roadmap item that would break this
        precondition is named in spec/04 §9: `Depends(p, lazy=True)` yielding a factory that
        resolves on first *use* is precisely a `build`-time (or later) call back into `acquire`,
        and whoever adds it needs to know this method is relying on that not happening yet.
        """
        # Review (documentation, now stale): "every remaining (necessarily `session`-scope) entry"
        # stopped being true when `_run.run_suite` went concurrent. `run_suite`'s own docstring
        # already says so ("it no longer only ever finds session-scope entries once concurrency can
        # leave other scopes stranded there too") — a module whose tests were still in flight when a
        # `KeyboardInterrupt` landed never reaches `remaining_by_module == 0`, so its `module`-scope
        # entries are swept here instead. The behaviour is right; the parenthetical is not, and the
        # reverse-insertion-order justification below is now doing real cross-scope work rather than
        # ordering one flat set of session fixtures. Worth updating, since this docstring is what
        # anyone reasoning about end-of-run teardown reads first.
        errors: list[Exception] = []
        for key in reversed(list(self._entries)):
            entry = self._entries.pop(key, None)
            if entry is None or entry.closer is None:
                continue
            try:
                await entry.closer()
            except Exception as exc:
                # Deliberately `Exception`, not `BaseException`: a `KeyboardInterrupt`/`SystemExit`
                # (or, once cancellation exists, `CancelledError`) raised by a closer must propagate
                # as itself and stop this loop immediately, exactly like every other "stop the
                # process" boundary in this codebase (`_run.py`'s own `except (KeyboardInterrupt,
                # SystemExit): raise` guards) — collecting it into the group below would instead
                # convert it into an ordinary-looking teardown failure a caller could catch and
                # continue past. The remaining keys in this loop are left un-torn-down on that
                # path, the same "stop now, some things leak" trade-off already accepted everywhere
                # else Ctrl-C is handled here.
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
                # `step.args`' third element (`keyword_only`) is intentionally unused here:
                # everything is bound by keyword, and that only works because
                # `_fixtures.plan_of` now rejects `Depends(...)` on a positional-only parameter
                # with a `DIError` at decoration time (the one binding mode `**kwargs` cannot
                # express) — every injection that survives to a `PlanStep` is either
                # positional-or-keyword or keyword-only, both of which bind correctly via
                # `**kwargs` regardless of which one it was. `keyword_only` stays on `Injection`/
                # `PlanStep` as a documentation/diagnostic field (it is what a future `--graph`
                # dump or error message would want to say "this came from `*, param=...`"), not
                # because construction branches on it.
                kwargs = {name: values[source] for name, source, _ in step.args}
                return await _construct(step.fixture, kwargs)

            values[step.step_id] = await store.acquire(key, step.fixture.scope, step.fixture, build)
            keys[step.step_id] = key
            acquired.append(key)
    except BaseException as exc:
        # The exception that triggered this cleanup (`exc` — the actual fixture that broke) stays
        # the one the caller sees and any future reporter headlines, even if cleaning up what was
        # already acquired *also* fails. Without the inner `try`/`except`, `_release_all` raising
        # would replace `exc` as the propagating exception outright (the bare `raise` below would
        # never run), demoting the real cause to `__context__` and promoting "fixture teardown"
        # — a secondary, cleanup-time failure — to the headline. A fresh interrupt during cleanup
        # is the one thing allowed to override that: it means "stop now" and outranks even the
        # original setup failure, the same precedence every other interrupt boundary in this
        # codebase gives it.
        # Review (must fix, and the fix probably lives here rather than in `_run.py`): this cleanup
        # releases *every* scope it acquired, including `module` and `session`. That was invisible
        # under the sequential runner. It is not now: `_run.run_suite` builds its whole `module`-
        # scope lifetime on "the only `release` for a module key is the one I issue after every
        # test of that module has finished", and this line silently issues others. Reproduced —
        # a module fixture built and torn down twice across two sibling tests when the first one's
        # setup fails partway, and (with an async module fixture) a sibling handed the instance
        # mid-`await entry.closer()`. See the long note at `_run._run_one`'s call to `setup`.
        # Options: don't release non-`function`-scope keys here and return the partially-acquired
        # list to the caller, or return which keys were released so `run_suite` can keep its counts
        # honest. Either way `_run.py` cannot fix it alone.
        #
        # Review (separate, smaller): `except BaseException as cleanup_exc` also catches
        # `asyncio.CancelledError`, so a cancellation arriving *during* cleanup is demoted to an
        # `add_note` on the original exception and never re-raised — the run continues as if the
        # task had not been cancelled. Pre-existing shape, newly reachable now that a `TaskGroup`
        # cancels siblings for real.
        try:
            await _release_all(store, reversed(acquired))
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as cleanup_exc:
            exc.add_note(
                "Additionally, tearing down already-acquired fixtures failed:\n"
                + "".join(traceback.format_exception(cleanup_exc))
            )
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
    # `except Exception`, not `except BaseException`: a `KeyboardInterrupt`/`SystemExit` (or,
    # once cancellation exists, `asyncio.CancelledError` — none of the three are `Exception`
    # subclasses) raised by a fixture's teardown must propagate as itself and stop this loop
    # immediately, matching the "KeyboardInterrupt/SystemExit propagate immediately from every
    # phase" invariant `_run_one`'s own `except (KeyboardInterrupt, SystemExit): raise` guards
    # around `setup`/`teardown` are already relying on — collecting one into the
    # `BaseExceptionGroup` below would silently convert "stop now" into an ordinary-looking
    # teardown `ERROR` the run keeps going past. spec/04 §5's "errors during teardown are
    # collected into an `ExceptionGroup`" means errors, not control-flow exceptions. The
    # remaining keys in `keys` are left un-released on that path — the same "stop now, some
    # things leak" trade-off this codebase already accepts at every other interrupt boundary.
    errors: list[Exception] = []
    for key in keys:
        try:
            await store.release(key)
        except Exception as exc:
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

    # Neither `async def` nor a generator, by the three `inspect` predicates above — but that
    # only tells us how `func` was *declared*, not what calling it hands back. A class with
    # `async def __call__`, or a plain `def` that just returns a coroutine (`def fx(): return
    # client.connect()`), is still a sync callable by every predicate above and would otherwise
    # land here with the coroutine itself uninspected: cached and injected as-is, every attribute
    # access on it failing with an unrelated `AttributeError`, plus a `RuntimeWarning: coroutine
    # ... was never awaited` from a garbage-collection point nowhere near the fixture that
    # produced it. Awaiting whatever comes back, if it's awaitable, closes that regardless of
    # which callable shape produced it — cheaper than deciding `Kind` statically on `Fixture`
    # (spec/04 §1), which would need `@velox.fixture()` to run `inspect` at decoration time
    # instead of here, and wouldn't help the "class with an async `__call__`" case either, since
    # the callable itself still isn't a coroutine function.
    result = func(**kwargs)
    if inspect.isawaitable(result):
        return await result, None
    return result, None
