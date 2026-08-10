"""Dependency-injection runtime: single-flight scope store + resolution-plan execution.

`ScopeStore` constructs each cache-keyed fixture instance exactly once, hands the same instance to
every later requester of that key, and closes it when the last one releases it. `setup` walks a
`ResolutionPlan`'s steps forward to build one test's arguments; `teardown` walks the same steps
back to release them, which is what produces dependent-before-dependency ordering.

The plans themselves are built in `_fixtures.py`, already flattened over a test's entire
transitive fixture graph, so every fixture a test touches — however deep — is its own entry in
`steps`, acquired and released by that one loop.
"""

from __future__ import annotations

import asyncio
import inspect
import traceback
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, final

from velox._fixtures import BuiltinContext, Fixture, PlanStep, ResolutionPlan, Scope

__all__ = ["ScopeStore", "setup", "teardown"]

type CacheKey = tuple[object, ...]
type Closer = Callable[[], Awaitable[None]]

#: Monotonic tail of every `"call"`-scope key, so two `Depends(...)` sites on the same call-scope
#: fixture — or the same site resolved twice, once per test that reaches it — never collide and
#: never share a construction.
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

    `step_id` only matters for `"call"` scope; it's threaded through unconditionally rather than
    branched on so callers never need to know which scopes care.

    `param_key` is appended to every shape below and defaults to `None`, reserving the slot for a
    parametrized fixture's case value.
    """
    match fixture.scope:
        case "session":
            return ("session", id(fixture), param_key)
        case "module":
            return ("module", id(fixture), module_path, param_key)
        case "function":
            return ("function", id(fixture), test_id, param_key)
        case "call":
            # `next(_call_site_ids)` alone already guarantees uniqueness across every "call"-scope
            # key. `id(fixture)`, `test_id`, and `step_id` are kept anyway so a key stays readable
            # at a glance in a failure message, rather than an opaque counter value.
            return ("call", id(fixture), test_id, step_id, next(_call_site_ids), param_key)


@final
@dataclass(slots=True)
class _Entry:
    """One cached fixture instance inside a `ScopeStore`: its single-flight `future`, the `closer`
    that tears it down (`None` for a plain-return fixture with nothing to release), and the
    refcount `release` decrements to decide whether this is the last requester."""

    scope: Scope
    fixture: Fixture[Any]
    future: asyncio.Future[Any]
    closer: Closer | None = None
    refcount: int = 0


@final
class ScopeStore:
    """Owns every constructed fixture instance for one run: single-flight construction,
    refcounted teardown.

    One instance per run. `session`-scope entries are the one case `release` never tears down on
    its own — only `aclose`, at the very end of the run, does.
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

        First requester constructs, everyone else awaits: the dict lookup and `Future` creation
        below are one synchronous step with no `await` between them, so two concurrent askers can
        never both decide they're the constructor. A `build()` that raises leaves that exception
        permanently on the `Future` — every later requester of the same key replays the identical
        exception instead of re-running (and re-failing) `build()`, giving one comprehensible
        error instead of many identical ones. Refcounting only happens on the success path: a key
        nobody ever successfully acquired has nothing that needs releasing.
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
                # `.exception()` marks the exception retrieved (avoiding asyncio's "Future
                # exception was never retrieved" warning at GC time) without clearing it, so a
                # later concurrent or waiting requester still sees and raises the same exception.
                entry.future.exception()
                raise
            entry.closer = closer
            entry.future.set_result(value)
        # Refcount reserved before awaiting, not after — see docs/rationale.md ("single-flight
        # construction, refcounted teardown") for the race this ordering closes.
        entry.refcount += 1
        try:
            return await entry.future
        except BaseException:
            entry.refcount -= 1
            raise

    async def release(self, key: CacheKey) -> None:
        """Decrement `key`'s refcount; tear it down (and forget it) if that reached zero.

        A no-op for a `key` this store never *successfully* acquired — an unknown key, or one
        whose `build()` raised — so a caller can always release what it holds without first
        checking whether construction actually succeeded. A failed entry is left in place, not
        deleted here: a concurrent or later requester of the same key still needs to replay its
        cached exception (`aclose` leaves it alone for the same reason).

        `session` scope never tears down through this path, only through `aclose` at the very end
        of the run; its refcount still decrements, for symmetry with `acquire`.
        """
        entry = self._entries.get(key)
        if entry is None or not entry.future.done() or entry.future.exception() is not None:
            return
        entry.refcount -= 1
        if entry.refcount > 0 or entry.scope == "session":
            return
        # The entry stays in `self._entries` until the closer actually finishes, not before —
        # deleting it up front would let a concurrent `acquire` on the same key race a fresh
        # construction against this teardown, briefly producing two live instances of a scope
        # whose contract is "exactly one".
        try:
            if entry.closer is not None:
                await entry.closer()
        finally:
            del self._entries[key]

    async def aclose(self) -> None:
        """End-of-run: force-teardown every entry still remaining, regardless of scope.

        Iterated in reverse insertion order. `_di.setup` walks a plan's already-topologically-
        sorted steps forwards and only ever hands `build()` values it has already acquired, so
        every entry a fixture's construction depends on is already in `self._entries` before that
        fixture's own entry is — making reverse insertion order a valid teardown order even when
        entries from unrelated tests or modules are interleaved in it.
        """
        errors: list[Exception] = []
        for key in reversed(list(self._entries)):
            entry = self._entries.pop(key, None)
            if entry is None or entry.closer is None:
                continue
            try:
                await entry.closer()
            except Exception as exc:
                # `Exception`, not `BaseException`: a KeyboardInterrupt/SystemExit/CancelledError
                # raised by a closer must propagate immediately rather than being folded into the
                # group below as an ordinary teardown failure. The remaining keys are left
                # un-torn-down on that path.
                errors.append(exc)
        if errors:
            raise BaseExceptionGroup("session-scope teardown", errors)


async def setup(
    plan: ResolutionPlan,
    store: ScopeStore,
    *,
    test_id: str,
    module_path: str,
    partial_module_keys: list[CacheKey] | None = None,
) -> tuple[dict[str, Any], tuple[CacheKey, ...]]:
    """Construct every step `plan` needs, in order, and return the test function's own kwargs.

    On success, also returns the cache key each step resolved to, in plan (construction) order —
    the caller passes this straight to `teardown` once the test is done. On failure partway
    through, whatever this attempt already acquired is released again, in reverse, before the
    triggering exception propagates; a `PlanStep` whose own `acquire` raised was never added to
    that list.

    `partial_module_keys`, if given, is where already-acquired `module`-scope keys go instead of
    being released immediately on failure — see `docs/rationale.md` ("partial module-key
    handoff") for why `module` scope needs this and the other three don't.
    """
    # One `BuiltinContext` per `setup()` call, not per step: every step of the same test's plan
    # sees the same `test_id`/`module_path`, so there is nothing step-specific to recompute.
    ctx = BuiltinContext(test_id=test_id, module_path=module_path)

    values: dict[int, Any] = {}
    keys: dict[int, CacheKey] = {}
    acquired: list[CacheKey] = []
    try:
        for step in plan.steps:
            key = key_for(step.fixture, step.step_id, test_id=test_id, module_path=module_path)

            async def build(step: PlanStep = step) -> tuple[Any, Closer | None]:
                # `step.args`' `keyword_only` element is unused here: everything binds by keyword,
                # which only works because `_fixtures.plan_of` already rejects `Depends(...)` on a
                # positional-only parameter at decoration time. `keyword_only` stays on
                # `Injection`/`PlanStep` as a diagnostic field, not because construction branches
                # on it.
                kwargs = {name: values[source] for name, source, _ in step.args}
                return await _construct(step.fixture, kwargs, ctx)

            values[step.step_id] = await store.acquire(key, step.fixture.scope, step.fixture, build)
            keys[step.step_id] = key
            acquired.append(key)
    except BaseException as exc:
        # `exc` — the fixture that actually broke — stays the exception the caller sees even if
        # cleaning up what was already acquired also fails; a fresh interrupt during cleanup is
        # the one thing allowed to override that. Module-scope keys are set aside for the caller
        # instead of released here, iff `partial_module_keys` was given.
        releasable = (
            acquired
            if partial_module_keys is None
            else [key for key in acquired if key[0] != "module"]
        )
        surviving_module_keys = (
            [] if partial_module_keys is None else [key for key in acquired if key[0] == "module"]
        )
        try:
            await _release_all(store, reversed(releasable))
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            # A fresh interrupt during cleanup means "stop now" and must propagate as itself, not
            # be demoted to a note on the original exception.
            raise
        except BaseException as cleanup_exc:
            exc.add_note(
                "Additionally, tearing down already-acquired fixtures failed:\n"
                + "".join(traceback.format_exception(cleanup_exc))
            )
        finally:
            # Populated regardless of whether the cleanup above succeeded, failed, or was itself
            # interrupted — a shared `module` key this attempt built still needs to reach the
            # caller's own bookkeeping no matter how the rest of this cleanup went.
            if partial_module_keys is not None:
                partial_module_keys.extend(surviving_module_keys)
        raise

    root_kwargs = {name: values[source] for name, source, _ in plan.root_args}
    ordered_keys = tuple(keys[step.step_id] for step in plan.steps)
    return root_kwargs, ordered_keys


async def teardown(store: ScopeStore, keys: Iterable[CacheKey]) -> None:
    """Release every key `setup` returned, in reverse order — giving teardown inversion.

    Every error is collected rather than short-circuiting the loop on the first one, so one
    broken fixture doesn't hide leaks in the others.
    """
    await _release_all(store, reversed(list(keys)))


async def _release_all(store: ScopeStore, keys: Iterable[CacheKey]) -> None:
    # `Exception`, not `BaseException`: a KeyboardInterrupt/SystemExit/CancelledError raised by a
    # fixture's teardown must propagate immediately rather than being folded into the
    # BaseExceptionGroup below as an ordinary teardown failure. The remaining keys are left
    # un-released on that path.
    errors: list[Exception] = []
    for key in keys:
        try:
            await store.release(key)
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise BaseExceptionGroup("fixture teardown", errors)


async def _construct(
    fixture: Fixture[Any], kwargs: Mapping[str, Any], ctx: BuiltinContext
) -> tuple[Any, Closer | None]:
    """Call `fixture.func`, adapting whichever of the four supported shapes it is — or, for a
    provider-backed fixture, call `fixture.provider` instead and never touch `func` at all. A
    provider-backed fixture's `func` body is `raise NotImplementedError(...)` by design, so it is
    checked first and unconditionally.

    Sync callables (plain functions and sync generators) run inline rather than in an executor —
    a blocking call is treated like any other call, not specially isolated.

    A provider-backed fixture does not bypass any of `ScopeStore`'s guarantees by taking this
    branch: the dispatch is still *inside* `build()`, which `setup` hands to `store.acquire(...)`,
    so it is single-flight cached, refcounted, refunded on failure, released by `_release_all`, and
    swept by `aclose` exactly like a fixture that calls `func`.
    """
    if fixture.provider is not None:
        return await fixture.provider(kwargs, ctx)

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

    # Neither `async def` nor a generator by the three `inspect` predicates above — but that only
    # tells us how `func` was declared, not what calling it hands back. A class with an async
    # `__call__`, or a plain `def` that returns a coroutine, is a sync callable by every predicate
    # yet still needs awaiting; checking `isawaitable` on the result catches both.
    result = func(**kwargs)
    if inspect.isawaitable(result):
        return await result, None
    return result, None
