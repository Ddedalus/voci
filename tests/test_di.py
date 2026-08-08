"""Regression tests for velox._di (spec/04 §4-6): the DI runtime, exercised directly against
`ScopeStore`/`setup`/`teardown`/`_construct` rather than through the whole CLI/collect pipeline.

There is no pytest-asyncio dependency in this project (see pyproject.toml) — every test here is a
plain sync `def test_*` that drives its own async body through `asyncio.run(...)`, the same
pattern `test_collect.py`'s rewrite-hook test already uses.
"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
import velox
from velox import Depends
from velox._di import ScopeStore, _construct, key_for, setup, teardown
from velox._fixtures import Fixture, Scope, plan_for


def run(coro):  # small helper: every test body is `run(scenario())`
    return asyncio.run(coro)


# ------------------------------------------------------------------------------------------
# ScopeStore.acquire / release: single-flight construction, refcounting, per-scope teardown
# timing.
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
    """Two concurrent `acquire`s racing for the same key: the dict-check-then-Future-creation
    step is synchronous (module docstring's load-bearing invariant), so only one of them actually
    builds — even though `build` itself yields control mid-construction via `asyncio.sleep(0)`,
    giving the second waiter a real chance to race it."""
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


@pytest.mark.parametrize("scope", ["function", "module", "call"])
def test_non_session_scope_tears_down_when_refcount_reaches_zero(scope: str) -> None:
    """Function/module/call scope all tear down through `release` once nothing holds them —
    `_SCOPE_RANK` ranks `call` with `function` for exactly this lifetime reason (module docstring
    of `_fixtures.py`)."""
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


def test_release_is_a_no_op_for_a_key_that_was_never_successfully_acquired() -> None:
    """`acquire`'s docstring: a failed construction never reaches `release` — proven here by
    calling `release` on a key whose `build` raised, and confirming nothing blows up and the
    closer (which would have raised, had it been reachable) is never invoked."""

    async def scenario() -> None:
        store = ScopeStore()
        fx = velox.fixture()(lambda: None)
        key = ("function", 1, "t")

        async def failing_build():
            raise RuntimeError("never built")

        with pytest.raises(RuntimeError, match="never built"):
            await store.acquire(key, "function", fx, failing_build)

    run(scenario())


# ------------------------------------------------------------------------------------------
# Exception caching (spec/04 §4): a broken session fixture fails every dependent with the same
# exception, and its body only runs once.
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
# Teardown ordering: dependents before dependencies, observed via an order list (not just "no
# error").
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
# Generator arity violations: yielding twice raises, naming the offending fixture.
# ------------------------------------------------------------------------------------------


def test_sync_generator_yielding_twice_raises_naming_the_fixture() -> None:
    @velox.fixture(name="bad_sync_gen")
    def bad():
        yield 1
        yield 2

    async def scenario() -> None:
        value, closer = await _construct(bad, {})
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
        value, closer = await _construct(bad, {})
        assert value == 1
        assert closer is not None
        with pytest.raises(RuntimeError, match="bad_async_gen"):
            await closer()

    run(scenario())


# ------------------------------------------------------------------------------------------
# All four fixture shapes (sync/async function, sync/async generator) construct and tear down
# correctly through the full setup/teardown pipeline.
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


# ------------------------------------------------------------------------------------------
# "call" scope: never shared, even across two `Depends()` sites on the identical fixture in one
# test.
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


def test_key_for_call_scope_is_unique_per_resolution_even_for_the_same_step() -> None:
    """`key_for`'s own contract, isolated from the rest of the pipeline: two calls for the same
    `(fixture, step_id)` never collide, which is what lets the same call-scope step be resolved
    more than once (e.g. once per parametrized run of the same test) without colliding either."""
    fx: Fixture[object] = velox.fixture(scope="call")(lambda: object())
    k1 = key_for(fx, 0, test_id="t", module_path="m")
    k2 = key_for(fx, 0, test_id="t", module_path="m")
    assert k1 != k2
