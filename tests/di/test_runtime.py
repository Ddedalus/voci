"""Tests for the DI runtime: single-flight construction, refcounting, and teardown order,
exercised directly against `ScopeStore`/`setup`/`teardown`/`_construct`.
"""

from __future__ import annotations

import asyncio
from typing import cast

from _support import run_async as run

import pytest
import velox
from velox import Depends
from velox._di.fixtures import BuiltinContext, Fixture, Scope, plan_for
from velox._di.runtime import ScopeStore, _construct, key_for, setup, teardown

#: `_construct`'s tests below exercise ordinary (non-provider-backed) fixtures directly, so the
#: `BuiltinContext` it requires is never actually consulted -- a fixed placeholder suffices.
_DUMMY_CTX = BuiltinContext(test_id="dummy::test", module_path="dummy.py")


# ScopeStore.acquire / release: single-flight construction, refcounting, per-scope teardown.
# ------------------------------------------------------------------------------------------


def test_acquire_single_flight_construction_runs_body_once_sequentially() -> None:
    """Two sequential `acquire`s for the same key: the second finds the entry already built and
    never calls `build` again."""
    calls: list[int] = []

    async def scenario() -> tuple[object, object]:
        async def build():
            calls.append(1)
            return "value", None

        store = ScopeStore()
        fx = velox.fixture(scope="session")(lambda: None)
        key = ("k",)
        v1 = await store.acquire(key, "session", fx, build)
        v2 = await store.acquire(key, "session", fx, build)
        return v1, v2

    v1, v2 = run(scenario())
    assert v1 == v2 == "value"
    assert len(calls) == 1


def test_acquire_single_flight_construction_runs_body_once_under_concurrency() -> None:
    """Two concurrent `acquire`s racing for the same key: only one of them actually builds,
    even though `build` itself yields control mid-construction via `asyncio.sleep(0)`."""
    calls: list[int] = []

    async def scenario() -> tuple[object, object]:
        async def build():
            calls.append(1)
            await asyncio.sleep(0)
            return "value", None

        store = ScopeStore()
        fx = velox.fixture(scope="session")(lambda: None)
        key = ("k",)
        return await asyncio.gather(
            store.acquire(key, "session", fx, build),
            store.acquire(key, "session", fx, build),
        )

    v1, v2 = run(scenario())
    assert v1 == v2 == "value"
    assert len(calls) == 1


def test_acquire_reserves_the_waiters_refcount_before_the_constructor_can_release() -> None:
    """A waiter parked on a still-pending future must already be counted in the refcount, so
    the constructing task cannot `acquire` -> `release` -> tear the instance down to zero while
    the waiter is still suspended and about to receive that same instance."""
    torn_down: list[str] = []
    still_alive_when_waiter_got_it: list[bool] = []
    build_started = asyncio.Event()
    finish_build = asyncio.Event()

    async def scenario() -> tuple[object, object]:
        async def build():
            build_started.set()
            await finish_build.wait()

            async def closer() -> None:
                torn_down.append("closed")

            return "value", closer

        store = ScopeStore()
        fx = velox.fixture(scope="module")(lambda: None)
        key: tuple[object, ...] = ("module", 1, "m")

        async def constructor() -> object:
            value = await store.acquire(key, "module", fx, build)
            await store.release(key)
            return value

        async def waiter() -> object:
            await build_started.wait()
            # `constructor`'s `build()` is now suspended on `finish_build.wait()`, so this
            # `acquire` finds the entry already present and parks on its still-pending future —
            # the "waiter" half of the race.
            value = await store.acquire(key, "module", fx, build)
            still_alive_when_waiter_got_it.append(len(torn_down) == 0)
            await store.release(key)  # must not raise
            return value

        constructor_task = asyncio.create_task(constructor())
        waiter_task = asyncio.create_task(waiter())
        await build_started.wait()
        # Give `waiter_task` a turn to actually reach and suspend on `await entry.future` before
        # letting `constructor`'s build finish — otherwise there is no race to test.
        await asyncio.sleep(0)
        finish_build.set()
        return await asyncio.gather(constructor_task, waiter_task)

    v1, v2 = run(scenario())
    assert v1 == v2 == "value"
    # The instance was still alive at the exact moment the waiter got hold of it -- with the
    # buggy ordering, the constructor's own `release` runs (and tears down) before the waiter's
    # refcount is ever counted, so this would observe `[False]` instead.
    assert still_alive_when_waiter_got_it == [True]
    # Torn down exactly once, and only after *both* releases.
    assert torn_down == ["closed"]


@pytest.mark.parametrize("scope", ["function", "module", "call"])
def test_non_session_scope_tears_down_when_refcount_reaches_zero(scope: str) -> None:
    """Function/module/call scope all tear down through `release` once nothing holds them."""
    scope_: Scope = cast("Scope", scope)
    torn_down: list[str] = []

    async def scenario() -> None:
        async def build():
            async def closer() -> None:
                torn_down.append("closed")

            return "v", closer

        store = ScopeStore()
        fx = velox.fixture()(lambda: None)
        key = (scope_, 1, "test1")
        await store.acquire(key, scope_, fx, build)
        await store.acquire(key, scope_, fx, build)  # refcount -> 2
        await store.release(key)
        assert torn_down == []  # still one holder
        await store.release(key)
        assert torn_down == ["closed"]

    run(scenario())


def test_session_scope_never_tears_down_through_release_only_aclose() -> None:
    torn_down: list[str] = []

    async def scenario() -> None:
        async def build():
            async def closer() -> None:
                torn_down.append("closed")

            return "v", closer

        store = ScopeStore()
        fx = velox.fixture(scope="session")(lambda: None)
        key = ("session", 1)
        await store.acquire(key, "session", fx, build)
        await store.release(key)
        assert torn_down == []  # release() is a documented no-op for session scope
        await store.aclose()
        assert torn_down == ["closed"]

    run(scenario())


def test_release_on_a_never_acquired_key_is_a_no_op() -> None:
    """A key the store has genuinely never seen: `release` returns quietly rather than raising
    `KeyError`."""

    async def scenario() -> None:
        store = ScopeStore()
        await store.release(("nope", "never", "seen"))  # must not raise

    run(scenario())


def test_release_after_a_failed_build_is_a_no_op_and_the_exception_stays_cached() -> None:
    """A key whose `build()` raised must survive a stray `release()` call on it: a later
    `acquire` for the same key replays the identical exception object rather than re-running
    `build`."""
    calls: list[int] = []

    async def scenario() -> tuple[BaseException, BaseException]:
        async def failing_build():
            calls.append(1)
            raise RuntimeError("never built")

        store = ScopeStore()
        fx = velox.fixture()(lambda: None)
        key = ("function", 1, "t")

        with pytest.raises(RuntimeError) as first:
            await store.acquire(key, "function", fx, failing_build)

        await store.release(key)  # must not raise, must not corrupt or discard the cached entry

        with pytest.raises(RuntimeError) as second:
            await store.acquire(key, "function", fx, failing_build)

        return first.value, second.value

    first_exc, second_exc = run(scenario())
    assert first_exc is second_exc  # the cached exception object, not a freshly-raised one
    assert len(calls) == 1  # `failing_build` only ever actually ran once


# Exception caching: a broken session fixture fails every dependent with the same exception.
# ------------------------------------------------------------------------------------------


def test_broken_session_fixture_fails_every_dependent_with_the_same_exception_built_once() -> None:
    calls: list[int] = []

    @velox.fixture(scope="session")
    def broken() -> int:
        calls.append(1)
        raise RuntimeError("boom")

    async def dependent_one(x: int = Depends(broken)) -> None:
        pass

    async def dependent_two(x: int = Depends(broken)) -> None:
        pass

    async def scenario() -> None:
        store = ScopeStore()
        plan_one = plan_for(dependent_one)
        plan_two = plan_for(dependent_two)

        with pytest.raises(RuntimeError, match="boom"):
            await setup(plan_one, store, test_id="t1", module_path="m")
        with pytest.raises(RuntimeError, match="boom"):
            await setup(plan_two, store, test_id="t2", module_path="m")

    run(scenario())
    assert len(calls) == 1  # `broken`'s body only ever actually ran once


# ------------------------------------------------------------------------------------------
# Teardown ordering: dependents before dependencies.
# ------------------------------------------------------------------------------------------


def test_teardown_order_is_dependent_before_dependency() -> None:
    order: list[str] = []

    @velox.fixture()
    def fx_a():
        yield "a"
        order.append("teardown_a")

    @velox.fixture()
    def fx_b(a: str = Depends(fx_a)):
        yield "b"
        order.append("teardown_b")

    async def test_func(b: str = Depends(fx_b)) -> None:
        pass

    async def scenario() -> None:
        store = ScopeStore()
        plan = plan_for(test_func)
        _kwargs, keys = await setup(plan, store, test_id="t", module_path="m")
        await teardown(store, keys)

    run(scenario())
    assert order == ["teardown_b", "teardown_a"]


def test_partial_setup_failure_releases_what_was_already_acquired_in_reverse_order() -> None:
    """A plan with three independent steps where the third's construction raises: the first two,
    already acquired, are released again before the triggering exception propagates — and in
    reverse (b before a), the same inversion `teardown` gives on the success path."""
    released: list[str] = []

    @velox.fixture()
    def fx_a():
        yield "a"
        released.append("a")

    @velox.fixture()
    def fx_b():
        yield "b"
        released.append("b")

    @velox.fixture()
    def fx_c():
        raise RuntimeError("c broke")

    async def test_func(
        a: str = Depends(fx_a), b: str = Depends(fx_b), c: str = Depends(fx_c)
    ) -> None:
        pass

    async def scenario() -> None:
        store = ScopeStore()
        plan = plan_for(test_func)
        with pytest.raises(RuntimeError, match="c broke"):
            await setup(plan, store, test_id="t", module_path="m")

    run(scenario())
    assert released == ["b", "a"]


# ------------------------------------------------------------------------------------------
# Multi-failure teardown: every broken closer surfaces, grouped, not just the first.
# ------------------------------------------------------------------------------------------


async def _build_a():
    async def closer() -> None:
        raise RuntimeError("a boom")

    return "a", closer


async def _build_b():
    async def closer() -> None:
        raise RuntimeError("b boom")

    return "b", closer


def test_teardown_raises_a_group_when_multiple_releases_fail() -> None:
    """`teardown` collects every release failure rather than stopping at the first, so one
    broken fixture doesn't hide leaks in the others."""

    async def scenario() -> BaseExceptionGroup:
        store = ScopeStore()
        fx = velox.fixture()(lambda: None)
        key_a, key_b = ("function", 1, "a"), ("function", 1, "b")
        await store.acquire(key_a, "function", fx, _build_a)
        await store.acquire(key_b, "function", fx, _build_b)

        with pytest.raises(BaseExceptionGroup) as excinfo:
            await teardown(store, [key_a, key_b])
        return excinfo.value

    group = run(scenario())
    assert {str(exc) for exc in group.exceptions} == {"a boom", "b boom"}


def test_aclose_raises_a_group_when_multiple_session_closers_fail() -> None:
    """`aclose` force-tears-down every remaining entry regardless of scope; two closers failing
    at once must both surface, not just the first."""

    async def scenario() -> BaseExceptionGroup:
        store = ScopeStore()
        fx = velox.fixture(scope="session")(lambda: None)
        await store.acquire(("session", 1), "session", fx, _build_a)
        await store.acquire(("session", 2), "session", fx, _build_b)

        with pytest.raises(BaseExceptionGroup) as excinfo:
            await store.aclose()
        return excinfo.value

    group = run(scenario())
    assert {str(exc) for exc in group.exceptions} == {"a boom", "b boom"}


# ------------------------------------------------------------------------------------------
# Generator arity violations: yielding twice raises, naming the offending fixture.
# ------------------------------------------------------------------------------------------


def test_sync_generator_yielding_twice_raises_naming_the_fixture() -> None:
    @velox.fixture(name="bad_sync_gen")
    def bad():
        yield 1
        yield 2

    async def scenario() -> None:
        value, closer = await _construct(bad, {}, _DUMMY_CTX)
        assert value == 1
        assert closer is not None
        with pytest.raises(RuntimeError, match="bad_sync_gen"):
            await closer()

    run(scenario())


def test_async_generator_yielding_twice_raises_naming_the_fixture() -> None:
    @velox.fixture(name="bad_async_gen")
    async def bad():
        yield 1
        yield 2

    async def scenario() -> None:
        value, closer = await _construct(bad, {}, _DUMMY_CTX)
        assert value == 1
        assert closer is not None
        with pytest.raises(RuntimeError, match="bad_async_gen"):
            await closer()

    run(scenario())


# All four fixture shapes (sync/async function, sync/async generator) construct and tear down.
# ------------------------------------------------------------------------------------------


def test_all_four_fixture_shapes_construct_and_tear_down() -> None:
    torn_down: list[str] = []

    @velox.fixture()
    def sync_fn() -> str:
        return "sync_fn"

    @velox.fixture()
    async def async_fn() -> str:
        return "async_fn"

    @velox.fixture()
    def sync_gen():
        yield "sync_gen"
        torn_down.append("sync_gen")

    @velox.fixture()
    async def async_gen():
        yield "async_gen"
        torn_down.append("async_gen")

    async def test_func(
        a: str = Depends(sync_fn),
        b: str = Depends(async_fn),
        c: str = Depends(sync_gen),
        d: str = Depends(async_gen),
    ) -> None:
        pass

    async def scenario() -> dict[str, str]:
        store = ScopeStore()
        plan = plan_for(test_func)
        kwargs, keys = await setup(plan, store, test_id="t", module_path="m")
        await teardown(store, keys)
        return kwargs

    kwargs = run(scenario())
    assert kwargs == {"a": "sync_fn", "b": "async_fn", "c": "sync_gen", "d": "async_gen"}
    assert set(torn_down) == {"sync_gen", "async_gen"}


# "call" scope: never shared, even across two `Depends()` sites on the identical fixture.
# ------------------------------------------------------------------------------------------


def test_call_scope_fixture_never_shares_an_instance_across_two_depends_sites() -> None:
    built: list[object] = []

    @velox.fixture(scope="call")
    def token() -> object:
        obj = object()
        built.append(obj)
        return obj

    async def test_func(x: object = Depends(token), y: object = Depends(token)) -> None:
        pass

    async def scenario() -> dict[str, object]:
        store = ScopeStore()
        plan = plan_for(test_func)
        assert len(plan.steps) == 2  # never deduplicated into one step, unlike other scopes
        kwargs, keys = await setup(plan, store, test_id="t", module_path="m")
        await teardown(store, keys)
        return kwargs

    kwargs = run(scenario())
    assert kwargs["x"] is not kwargs["y"]
    assert len(built) == 2


# Edge cases beyond the four "happy path" fixture shapes above.
# ------------------------------------------------------------------------------------------


def test_generator_teardown_failure_is_the_users_exception_not_masked_by_gen_close() -> None:
    """A generator fixture whose *post-`yield`* code raises: `_construct`'s closer runs
    `finally: gen.close()` unconditionally, and that must not swallow or replace the user's own
    exception with something from the close path."""

    @velox.fixture(name="flaky_teardown")
    def flaky():
        yield 1
        raise RuntimeError("teardown boom")

    async def scenario() -> None:
        value, closer = await _construct(flaky, {}, _DUMMY_CTX)
        assert value == 1
        assert closer is not None
        with pytest.raises(RuntimeError, match="teardown boom"):
            await closer()

    run(scenario())


def test_sync_function_fixture_returning_an_awaitable_is_awaited() -> None:
    """A fixture whose body is a plain `def` but returns an awaitable: the value handed to the
    test must be the *resolved* value, not the un-awaited coroutine/awaitable object."""

    async def _resolve() -> str:
        return "resolved"

    @velox.fixture()
    def returns_awaitable():
        return _resolve()

    async def scenario() -> None:
        value, closer = await _construct(returns_awaitable, {}, _DUMMY_CTX)
        assert value == "resolved"
        assert closer is None

    run(scenario())


def test_key_for_call_scope_is_unique_per_resolution_even_for_the_same_step() -> None:
    """Two `key_for` calls for the same `(fixture, step_id)` never collide."""
    fx: Fixture[object] = velox.fixture(scope="call")(lambda: object())
    k1 = key_for(fx, 0, test_id="t", module_path="m")
    k2 = key_for(fx, 0, test_id="t", module_path="m")
    assert k1 != k2
