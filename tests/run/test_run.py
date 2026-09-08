"""Tests for voci._run.run: dispatch, setup/call/teardown, concurrency, timeouts, reporting."""

from __future__ import annotations

import asyncio
import os
import signal
import threading
import time
import warnings
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from unittest import mock

import pytest
from _support import make_record as _record
from _support import run_async

import voci
from voci._collection.collect import CollectionError
from voci._di.fixtures import expand_cases, plan_for
from voci._marks import marks_of
from voci._run.run import (
    AdmissionGate,
    Outcome,
    exit_code_for,
    run_suite,
    solo_for_patching,
)
from voci._run.run import TestResult as Result


async def _passes() -> None:
    pass


async def _fails() -> None:
    raise AssertionError("nope")


async def _cancels_itself() -> None:
    task = asyncio.current_task()
    assert task is not None
    task.cancel()
    await asyncio.sleep(10)


def test_passing_test_produces_passed() -> None:
    (result,) = run_suite([_record(0, _passes, "test_passes")])
    assert result.outcome is Outcome.PASSED
    assert result.failure is None


def test_failing_test_produces_failed_with_traceback() -> None:
    (result,) = run_suite([_record(0, _fails, "test_fails")])
    assert result.outcome is Outcome.FAILED
    assert result.failure is not None
    assert "AssertionError" in result.failure
    assert "nope" in result.failure


def test_run_suite_preserves_record_order() -> None:
    records = [
        _record(0, _passes, "test_a"),
        _record(1, _fails, "test_b"),
        _record(2, _passes, "test_c"),
    ]

    results = run_suite(records)

    assert [result.id for result in results] == [r.id for r in records]
    assert [result.index for result in results] == [0, 1, 2]
    assert [result.outcome for result in results] == [
        Outcome.PASSED,
        Outcome.FAILED,
        Outcome.PASSED,
    ]


# A sync `def test_*`'s call phase runs on the loop's default executor, not awaited directly.
# ------------------------------------------------------------------------------------------


def _sync_passes() -> None:
    pass


def _sync_fails() -> None:
    raise AssertionError("nope")


def test_sync_test_produces_passed() -> None:
    (result,) = run_suite([_record(0, _sync_passes, "test_sync_passes")])
    assert result.outcome is Outcome.PASSED
    assert result.failure is None


def test_sync_test_failure_produces_failed_with_traceback() -> None:
    (result,) = run_suite([_record(0, _sync_fails, "test_sync_fails")])
    assert result.outcome is Outcome.FAILED
    assert result.failure is not None
    assert "AssertionError" in result.failure
    assert "nope" in result.failure


def test_sync_test_with_a_fixture_is_injected_with_a_working_value() -> None:
    @voci.fixture()
    def answer() -> int:
        return 42

    def test_func(x: int = voci.Depends(answer)) -> None:
        assert x == 42

    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert result.outcome is Outcome.PASSED
    assert result.failure is None


def test_parametrized_case_receives_its_own_values_as_kwargs() -> None:
    """`record.params` -- the values `@voci.parametrize` expansion assigned this case -- are
    passed to `func` as extra kwargs, alongside `plan`'s own (empty, here)."""

    async def test_func(n: int, expected: int) -> None:
        assert n * 2 == expected

    records = [
        _record(0, test_func, "test_func[1-2]", params={"n": 1, "expected": 2}),
        _record(1, test_func, "test_func[2-5]", params={"n": 2, "expected": 5}),
    ]

    results = run_suite(records)

    assert [result.outcome for result in results] == [Outcome.PASSED, Outcome.FAILED]


def test_parametrized_case_kwargs_combine_with_an_actual_dependency() -> None:
    @voci.fixture()
    def answer() -> int:
        return 42

    def test_func(n: int, x: int = voci.Depends(answer)) -> None:
        assert n == 1
        assert x == 42

    plan = plan_for(test_func, known_params=frozenset({"n"}))
    record = _record(0, test_func, "test_func[1]", plan=plan, params={"n": 1})

    (result,) = run_suite([record])

    assert result.outcome is Outcome.PASSED
    assert result.failure is None


def test_sync_test_does_not_stall_a_concurrently_dispatched_async_test() -> None:
    """A blocking `time.sleep` in a sync test's body must not hold up the shared event loop
    that a sibling async test's own `asyncio.sleep` is scheduled on. Serial execution of the
    two would take >= 0.2s."""

    def _sync_sleeper() -> None:
        time.sleep(0.1)

    async def _async_sleeper() -> None:
        await asyncio.sleep(0.1)

    start = time.monotonic()
    results = run_suite(
        [_record(0, _sync_sleeper, "test_sync"), _record(1, _async_sleeper, "test_async")],
        concurrency=2,
    )
    elapsed = time.monotonic() - start

    assert [r.outcome for r in results] == [Outcome.PASSED, Outcome.PASSED]
    assert elapsed < 0.18


def test_duration_is_timed() -> None:
    """A bare `duration >= 0.0` would pass even with `duration` hard-coded to `0` — sleep a
    known interval and assert the measured duration brackets it, which is what "is timed"
    actually claims."""

    async def _sleeps() -> None:
        await asyncio.sleep(0.05)

    (result,) = run_suite([_record(0, _sleeps, "test_sleeps")])
    assert result.duration >= 0.05


def test_cancelled_error_is_reported_as_failed_not_propagated() -> None:
    """`asyncio.CancelledError` is a `BaseException`, not an `Exception` — a test whose own task
    cancels itself must not abort every remaining test and discard every result already
    collected."""
    records = [
        _record(0, _passes, "test_before"),
        _record(1, _cancels_itself, "test_cancels"),
        _record(2, _passes, "test_after"),
    ]

    results = run_suite(records)

    assert [result.outcome for result in results] == [
        Outcome.PASSED,
        Outcome.FAILED,
        Outcome.PASSED,
    ]
    assert results[1].failure is not None
    assert "CancelledError" in results[1].failure


def test_keyboard_interrupt_propagates_instead_of_being_reported_as_a_failure() -> None:
    """The one `BaseException` still meant to blow past this boundary: it means "stop the
    process", not "this test misbehaved", so it must not be folded into a `FAILED` result."""

    async def _raises_keyboard_interrupt() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_suite([_record(0, _raises_keyboard_interrupt, "test_interrupt")])


def test_empty_selection_still_builds_and_closes_a_runner() -> None:
    assert run_suite([]) == []


# Setup -> call -> teardown, driven by real `ResolutionPlan`s.
# ------------------------------------------------------------------------------------------


def test_function_scope_fixture_is_injected_with_a_working_value() -> None:
    @voci.fixture()
    def answer() -> int:
        return 42

    async def test_func(x: int = voci.Depends(answer)) -> None:
        assert x == 42

    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert result.outcome is Outcome.PASSED
    assert result.failure is None


def test_session_scope_fixture_is_shared_and_built_exactly_once_across_tests() -> None:
    """Two independently-`plan_for`-built plans referencing the same `Fixture` object still
    resolve to the same session cache key (`_di.key_for` keys session scope by `id(fixture)`,
    not by which test's plan asked for it) — so the second test observes the first's instance
    rather than triggering a second construction."""
    builds: list[int] = []

    @voci.fixture(scope="session")
    def counted() -> int:
        builds.append(1)
        return len(builds)

    async def test_a(x: int = voci.Depends(counted)) -> None:
        assert x == 1

    async def test_b(x: int = voci.Depends(counted)) -> None:
        assert x == 1  # same shared instance, not rebuilt for this test

    results = run_suite(
        [
            _record(0, test_a, "test_a", plan=plan_for(test_a)),
            _record(1, test_b, "test_b", plan=plan_for(test_b)),
        ]
    )

    assert [r.outcome for r in results] == [Outcome.PASSED, Outcome.PASSED]
    assert len(builds) == 1


def test_module_scope_fixture_is_shared_and_torn_down_once_across_tests_in_one_module() -> None:
    """The `module`-scope mirror of the `session` test above: `_run_one` holds `module`-scope
    keys open instead of releasing them per test, and `run_suite` releases them together once it
    reaches the last test sharing that `path` — proven here by both a build count and a teardown
    count of exactly one across two tests, not two of each."""
    builds: list[int] = []
    torn_down: list[str] = []

    @voci.fixture(scope="module")
    def per_module():
        builds.append(1)
        yield len(builds)
        torn_down.append("closed")

    async def test_a(x: int = voci.Depends(per_module)) -> None:
        assert x == 1

    async def test_b(x: int = voci.Depends(per_module)) -> None:
        assert x == 1  # same module-scope instance, not rebuilt for this test

    results = run_suite(
        [
            _record(0, test_a, "test_a", plan=plan_for(test_a)),
            _record(1, test_b, "test_b", plan=plan_for(test_b)),
        ]
    )

    assert [r.outcome for r in results] == [Outcome.PASSED, Outcome.PASSED]
    # One build, one teardown -- not two of each. If each test's own teardown released its
    # `module`-scope keys immediately instead of `run_suite` holding them open across the file,
    # `test_b` would rebuild `per_module` from scratch (`builds == [1, 1]`) and tear it down
    # twice.
    assert len(builds) == 1
    assert torn_down == ["closed"]


def test_run_suite_end_to_end_with_a_parametrized_fixture() -> None:
    """Full pipeline proof, one level above `_di`'s own tests: `expand_cases`'s specialized plans
    dispatched through `run_suite` each pass their own case's value through to the test, and a
    `module`-scope fixture still builds only once per case."""
    builds: list[str] = []
    seen: list[str] = []

    @voci.fixture(scope="module", params=["sqlite", "postgres"])
    def backend(param: str) -> str:
        builds.append(param)
        return param

    async def test_uses_backend(value: str = voci.Depends(backend)) -> None:
        seen.append(value)

    expansions = expand_cases(plan_for(test_uses_backend))
    records = [
        _record(i, test_uses_backend, f"test_uses_backend[{e.case_id}]", plan=e.plan)
        for i, e in enumerate(expansions)
    ]

    results = run_suite(records)

    assert [r.outcome for r in results] == [Outcome.PASSED, Outcome.PASSED]
    assert sorted(seen) == ["postgres", "sqlite"]
    assert sorted(builds) == ["postgres", "sqlite"]  # one construction per case, not two


def test_module_scope_fixture_is_not_shared_across_different_modules() -> None:
    """The `module_path` half of `_di.key_for`'s cache key: two tests in *different* files
    must each get their own instance."""
    builds: list[int] = []

    @voci.fixture(scope="module")
    def per_module() -> int:
        builds.append(1)
        return len(builds)

    async def test_a(x: int = voci.Depends(per_module)) -> None:
        pass

    async def test_b(x: int = voci.Depends(per_module)) -> None:
        pass

    results = run_suite(
        [
            _record(0, test_a, "test_a", plan=plan_for(test_a), path=Path("mod_a.py")),
            _record(1, test_b, "test_b", plan=plan_for(test_b), path=Path("mod_b.py")),
        ]
    )

    assert [r.outcome for r in results] == [Outcome.PASSED, Outcome.PASSED]
    assert len(builds) == 2


def test_session_scope_fixture_is_torn_down_at_end_of_run() -> None:
    """`store.aclose()` runs after the loop: the generator's teardown side effect appears only
    after `run_suite` has returned."""
    torn_down: list[str] = []

    @voci.fixture(scope="session")
    def db():
        yield "db"
        torn_down.append("db")

    async def test_func(x: str = voci.Depends(db)) -> None:
        assert x == "db"

    run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert torn_down == ["db"]


def test_fixture_setup_failure_produces_error_not_failed() -> None:
    @voci.fixture()
    def broken() -> int:
        raise RuntimeError("setup boom")

    async def test_func(x: int = voci.Depends(broken)) -> None:
        raise AssertionError("must never run: setup already failed")

    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert result.outcome is Outcome.ERROR
    assert result.failure is not None
    assert "setup boom" in result.failure


def test_fixture_teardown_failure_after_a_passing_call_produces_error() -> None:
    """A teardown failure surfaces as `error`, even though the call phase itself passed."""

    @voci.fixture()
    def flaky_teardown():
        yield 1
        raise RuntimeError("teardown boom")

    async def test_func(x: int = voci.Depends(flaky_teardown)) -> None:
        assert x == 1  # the call phase genuinely passes

    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert result.outcome is Outcome.ERROR
    assert result.failure is not None
    assert "teardown boom" in result.failure


def test_call_and_teardown_both_failing_still_reports_error_with_both_tracebacks() -> None:
    @voci.fixture()
    def flaky_teardown():
        yield 1
        raise RuntimeError("teardown boom")

    async def test_func(x: int = voci.Depends(flaky_teardown)) -> None:
        raise AssertionError("call boom")

    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert result.outcome is Outcome.ERROR
    assert result.failure is not None
    assert "call boom" in result.failure
    assert "teardown boom" in result.failure


# Fixtures a container declared with `voci.use(...)`: constructed and torn down like any
# other, with nothing passed to the test.
# ------------------------------------------------------------------------------------------


def test_a_declared_fixtures_side_effect_runs_and_its_value_is_discarded() -> None:
    events: list[str] = []

    @voci.fixture()
    def declared():
        events.append("setup")
        yield "never seen"
        events.append("teardown")

    async def test_func() -> None:
        events.append("call")

    plan = plan_for(test_func, implicit=[declared])
    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan)])

    assert result.outcome is Outcome.PASSED
    assert events == ["setup", "call", "teardown"]


def test_a_declared_fixture_wraps_the_tests_own_dependency() -> None:
    """Declared fixtures set up first and tear down last, so an outer resource is live for the
    whole of an inner one's lifetime."""
    events: list[str] = []

    @voci.fixture()
    def declared():
        events.append("declared setup")
        yield None
        events.append("declared teardown")

    @voci.fixture()
    def asked_for():
        events.append("asked_for setup")
        yield None
        events.append("asked_for teardown")

    async def test_func(x: object = voci.Depends(asked_for)) -> None:
        events.append("call")

    plan = plan_for(test_func, implicit=[declared])
    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan)])

    assert result.outcome is Outcome.PASSED
    assert events == [
        "declared setup",
        "asked_for setup",
        "call",
        "asked_for teardown",
        "declared teardown",
    ]


def test_a_failing_declared_fixture_errors_the_test_and_names_itself() -> None:
    @voci.fixture()
    def declared():
        raise RuntimeError("declared boom")

    async def test_func() -> None:
        raise AssertionError("must never run: setup already failed")

    plan = plan_for(test_func, implicit=[declared])
    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan)])

    assert result.outcome is Outcome.ERROR
    assert result.failure is not None
    assert "declared boom" in result.failure
    assert "declared" in result.failure


def test_a_declared_exclusive_fixture_serializes_every_test_that_holds_it() -> None:
    """`exclusive_tokens_of` reads the whole plan, so a declared fixture's token gates admission
    exactly as a directly-depended one's does."""
    in_flight = 0
    peak = 0

    @voci.fixture(exclusive="database")
    def declared():
        return None

    async def test_func() -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1

    plan = plan_for(test_func, implicit=[declared])
    records = [_record(i, test_func, f"test_{i}", plan=plan) for i in range(4)]

    results = run_suite(records, concurrency=4)

    assert [r.outcome for r in results] == [Outcome.PASSED] * 4
    assert peak == 1


# TestResult.failure_summary: a short, exception-object-based summary captured directly at
# each of `_run_one`'s catch sites, not parsed back out of `failure`'s rendered traceback text.
# ------------------------------------------------------------------------------------------


def test_failure_summary_for_a_failed_call_is_exception_type_and_message() -> None:
    (result,) = run_suite([_record(0, _fails, "test_fails")])
    assert result.outcome is Outcome.FAILED
    assert result.failure_summary == "AssertionError: nope"


def test_failure_summary_for_a_setup_error_is_exception_type_and_message() -> None:
    @voci.fixture()
    def broken() -> int:
        raise RuntimeError("setup boom")

    async def test_func(x: int = voci.Depends(broken)) -> None:
        raise AssertionError("must never run: setup already failed")

    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert result.outcome is Outcome.ERROR
    assert result.failure_summary == "RuntimeError: setup boom"


def test_failure_summary_for_a_teardown_error_is_the_exception_groups_own_summary() -> None:
    """`_di._release_all` raises an `ExceptionGroup`/`BaseExceptionGroup` on every teardown
    failure; its own `str()` is a sensible one-line summary, not the box-drawing rule its
    rendered traceback ends in."""

    @voci.fixture()
    def flaky_teardown():
        yield 1
        raise RuntimeError("teardown boom")

    async def test_func(x: int = voci.Depends(flaky_teardown)) -> None:
        assert x == 1

    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert result.outcome is Outcome.ERROR
    assert result.failure_summary is not None
    assert result.failure_summary.startswith("ExceptionGroup:")
    assert "fixture teardown" in result.failure_summary
    assert "+--" not in result.failure_summary  # the old heuristic's box-drawing rule


def test_failure_summary_for_call_and_teardown_both_failing_leads_with_the_call() -> None:
    """When both the call and teardown fail, `failure_summary` is the call's summary, not
    teardown's."""

    @voci.fixture()
    def flaky_teardown():
        yield 1
        raise RuntimeError("teardown boom")

    async def test_func(x: int = voci.Depends(flaky_teardown)) -> None:
        raise AssertionError("call boom")

    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert result.outcome is Outcome.ERROR
    assert result.failure_summary == "AssertionError: call boom"


def test_failure_summary_for_timeout_is_the_budget_message() -> None:
    async def _hangs() -> None:
        await asyncio.sleep(10)

    (result,) = run_suite([_record(0, _hangs, "test_hangs")], timeout=0.05)

    assert result.outcome is Outcome.TIMEOUT
    assert result.failure_summary == "test exceeded its 0.05s timeout budget"


def test_failure_summary_takes_only_the_first_line_of_a_multiline_exception_message() -> None:
    """Mirrors the two-line shape the vendored assertion rewriter produces for
    `assert x == y, "message"`: the user's own message first, the rewriter's own explanation on
    a second line."""

    async def _raises() -> None:
        raise AssertionError("expected four widgets\n>assert 3 == 4")

    (result,) = run_suite([_record(0, _raises, "test_multiline")])

    assert result.outcome is Outcome.FAILED
    assert result.failure_summary == "AssertionError: expected four widgets"


def test_passing_test_has_no_failure_summary() -> None:
    (result,) = run_suite([_record(0, _passes, "test_passes")])
    assert result.failure_summary is None


# KeyboardInterrupt/SystemExit from every phase, including from inside a fixture's teardown.
# ------------------------------------------------------------------------------------------


def test_keyboard_interrupt_from_a_fixture_setup_propagates_immediately() -> None:
    @voci.fixture()
    def broken():
        raise KeyboardInterrupt

    async def test_func(x: int = voci.Depends(broken)) -> None:
        pass

    with pytest.raises(KeyboardInterrupt):
        run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])


def test_keyboard_interrupt_from_a_fixture_teardown_propagates_immediately() -> None:
    """`_di._release_all` (which both `_di.setup`'s cleanup and `_di.teardown` funnel through)
    re-raises `KeyboardInterrupt`/`SystemExit` immediately instead of folding them into its
    `BaseExceptionGroup` — without that, this would surface as `ERROR` for the test and a run
    that keeps going, not a `KeyboardInterrupt` propagating out of `run_suite`."""

    @voci.fixture()
    def flaky():
        yield 1
        raise KeyboardInterrupt

    async def test_func(x: int = voci.Depends(flaky)) -> None:
        pass

    with pytest.raises(KeyboardInterrupt):
        run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])


def test_system_exit_from_a_fixture_teardown_propagates_immediately() -> None:
    @voci.fixture()
    def flaky():
        yield 1
        raise SystemExit(1)

    async def test_func(x: int = voci.Depends(flaky)) -> None:
        pass

    with pytest.raises(SystemExit):
        run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])


def test_session_scope_fixture_is_still_torn_down_after_a_keyboard_interrupt_mid_call() -> None:
    """`store.aclose()` runs in a `finally` around the whole loop, so a `KeyboardInterrupt`
    raised by the test body itself (not a fixture) still gets best-effort session-scope
    teardown on the way out."""
    torn_down: list[str] = []

    @voci.fixture(scope="session")
    def db():
        yield "db"
        torn_down.append("db")

    async def test_func(x: str = voci.Depends(db)) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert torn_down == ["db"]


def test_session_scope_teardown_failure_is_reported_to_stderr_and_does_not_fail_the_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A session-scope teardown failure reaches stderr, does not touch the already-`PASSED`
    test's own outcome, and does not turn the run's exit code nonzero on its own -- there is no
    `TestResult` to attribute it to."""

    @voci.fixture(scope="session")
    def flaky_session():
        yield 1
        raise RuntimeError("session teardown boom")

    async def test_func(x: int = voci.Depends(flaky_session)) -> None:
        assert x == 1

    results = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert [r.outcome for r in results] == [Outcome.PASSED]
    assert exit_code_for(results, []) == 0
    assert "session teardown boom" in capsys.readouterr().err


# Concurrency: tests dispatch as concurrent `asyncio.Task`s admitted by a shared
# `AdmissionGate(concurrency)`, inside one `asyncio.TaskGroup`.
# ------------------------------------------------------------------------------------------


def test_concurrency_bounds_the_number_of_tests_in_flight_at_once() -> None:
    """The gate genuinely bounds how many tests are inside their call phase at once, not
    just how many `asyncio.Task`s exist -- tracked via the actual concurrent-entry count from
    inside the test body. `> 1` also rules out the gate accidentally serializing
    everything, which `peak <= concurrency` alone would not catch."""
    in_flight = 0
    peak = 0

    async def _test() -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1

    records = [_record(i, _test, f"test_{i}") for i in range(20)]

    results = run_suite(records, concurrency=4)

    assert [r.outcome for r in results] == [Outcome.PASSED] * 20
    assert peak <= 4
    assert peak > 1


def test_concurrency_bounds_module_scope_teardown_too() -> None:
    """Module-scope teardown runs *inside* the gate's admission, so it is concurrency-bounded
    too -- four independent single-test modules, each with an async `scope="module"` fixture
    whose own teardown awaits, at `concurrency=2`, must never show more than 2 concurrently
    in-flight teardowns."""
    in_teardown = 0
    peak_teardown = 0

    def _make_module_fixture() -> voci.Fixture[None]:
        @voci.fixture(scope="module")
        async def per_module():
            nonlocal in_teardown, peak_teardown
            yield None
            in_teardown += 1
            peak_teardown = max(peak_teardown, in_teardown)
            await asyncio.sleep(0.03)
            in_teardown -= 1

        return per_module

    records = []
    for i in range(4):
        fixture = _make_module_fixture()

        async def test_func(x: None = voci.Depends(fixture)) -> None:
            pass

        records.append(
            _record(i, test_func, f"test_{i}", plan=plan_for(test_func), path=Path(f"mod_{i}.py"))
        )

    results = run_suite(records, concurrency=2)

    assert [r.outcome for r in results] == [Outcome.PASSED] * 4
    assert peak_teardown <= 2
    assert peak_teardown > 1


def test_concurrency_one_is_exactly_serial_in_logical_order() -> None:
    """`concurrency=1` behaves as an exact serial mode -- dispatched one at a time, in logical
    (`records`/index) order -- through the same concurrent machinery, not a separate code path.
    A shared event log pins both "one at a time" and "in logical order"."""
    events: list[str] = []

    def _make(i: int) -> Callable[[], object]:
        async def test_func() -> None:
            events.append(f"start-{i}")
            await asyncio.sleep(0.01)
            events.append(f"end-{i}")

        return test_func

    records = [_record(i, _make(i), f"test_{i}") for i in range(4)]

    results = run_suite(records, concurrency=1)

    assert [r.outcome for r in results] == [Outcome.PASSED] * 4
    assert events == [
        "start-0",
        "end-0",
        "start-1",
        "end-1",
        "start-2",
        "end-2",
        "start-3",
        "end-3",
    ]


# `AdmissionGate`: `exclusive=` fixture admission and `@voci.solo`.
# ------------------------------------------------------------------------------------------


def _overlap_tracker() -> tuple[Callable[[], None], Callable[[], None], Callable[[], int]]:
    """A shared in-flight counter, as `enter`/`exit`/`peak`, for pinning real overlap (or its
    absence) between two or more concurrently-dispatched test bodies."""
    in_flight = 0
    peak = 0

    def enter() -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)

    def leave() -> None:
        nonlocal in_flight
        in_flight -= 1

    return enter, leave, lambda: peak


def test_exclusive_string_token_never_lets_two_tests_run_concurrently() -> None:
    """Two fixtures naming the same `exclusive="db"` token contend, even though nothing else
    would stop their tests running at once at `concurrency=4`."""
    enter, leave, peak = _overlap_tracker()

    @voci.fixture(exclusive="db")
    async def conn_a() -> AsyncIterator[None]:
        yield None

    @voci.fixture(exclusive="db")
    async def conn_b() -> AsyncIterator[None]:
        yield None

    async def test_one(x: None = voci.Depends(conn_a)) -> None:
        enter()
        await asyncio.sleep(0.02)
        leave()

    async def test_two(x: None = voci.Depends(conn_b)) -> None:
        enter()
        await asyncio.sleep(0.02)
        leave()

    records = [
        _record(0, test_one, "test_one", plan=plan_for(test_one)),
        _record(1, test_two, "test_two", plan=plan_for(test_two)),
    ]

    results = run_suite(records, concurrency=4)

    assert [r.outcome for r in results] == [Outcome.PASSED, Outcome.PASSED]
    assert peak() == 1


def test_exclusive_true_only_contends_with_its_own_fixture() -> None:
    """`exclusive=True` is a token private to that one fixture -- two *different* `exclusive=True`
    fixtures never contend with each other, so their tests genuinely overlap."""
    enter, leave, peak = _overlap_tracker()

    @voci.fixture(exclusive=True)
    async def res_a() -> AsyncIterator[None]:
        yield None

    @voci.fixture(exclusive=True)
    async def res_b() -> AsyncIterator[None]:
        yield None

    async def test_one(x: None = voci.Depends(res_a)) -> None:
        enter()
        await asyncio.sleep(0.03)
        leave()

    async def test_two(x: None = voci.Depends(res_b)) -> None:
        enter()
        await asyncio.sleep(0.03)
        leave()

    records = [
        _record(0, test_one, "test_one", plan=plan_for(test_one)),
        _record(1, test_two, "test_two", plan=plan_for(test_two)),
    ]

    results = run_suite(records, concurrency=4)

    assert [r.outcome for r in results] == [Outcome.PASSED, Outcome.PASSED]
    assert peak() == 2


def test_exclusive_true_serializes_two_tests_sharing_the_same_fixture() -> None:
    enter, leave, peak = _overlap_tracker()

    @voci.fixture(exclusive=True)
    async def shared() -> AsyncIterator[None]:
        yield None

    async def test_one(x: None = voci.Depends(shared)) -> None:
        enter()
        await asyncio.sleep(0.02)
        leave()

    async def test_two(x: None = voci.Depends(shared)) -> None:
        enter()
        await asyncio.sleep(0.02)
        leave()

    records = [
        _record(0, test_one, "test_one", plan=plan_for(test_one)),
        _record(1, test_two, "test_two", plan=plan_for(test_two)),
    ]

    results = run_suite(records, concurrency=4)

    assert [r.outcome for r in results] == [Outcome.PASSED, Outcome.PASSED]
    assert peak() == 1


def test_solo_test_never_overlaps_with_anything_else() -> None:
    """A `@voci.solo` test is never admitted alongside another test, ordinary or exclusive, and
    blocks every other admission for as long as it runs -- a suite-wide write lock. Ordinary
    tests are still free to overlap with *each other*, so a bare "nothing ever overlaps"
    assertion would pass for the wrong reason -- this checks specifically for `"solo"` sharing
    `active` with anything else."""
    active: set[str] = set()
    solo_violations: list[frozenset[str]] = []

    def _make(name: str, *, solo: bool = False) -> Callable[[], object]:
        async def test_func() -> None:
            active.add(name)
            if "solo" in active and active != {"solo"}:
                solo_violations.append(frozenset(active))
            await asyncio.sleep(0.02)
            active.discard(name)

        return voci.solo(test_func) if solo else test_func

    records = [_record(0, _make("solo", solo=True), "test_solo")]
    records += [_record(i, _make(f"ordinary_{i}"), f"test_ordinary_{i}") for i in range(1, 6)]

    results = run_suite(records, concurrency=4)

    assert [r.outcome for r in results] == [Outcome.PASSED] * 6
    assert solo_violations == []


def test_ordinary_tests_still_overlap_around_an_unrelated_exclusive_fixture() -> None:
    """A test with no `exclusive=` fixtures and no `@voci.solo` mark is unaffected by another
    test's unrelated exclusive resource -- `AdmissionGate` never over-serializes the whole suite
    for one contended fixture."""
    enter, leave, peak = _overlap_tracker()

    @voci.fixture(exclusive="db")
    async def db() -> AsyncIterator[None]:
        yield None

    async def uses_db(x: None = voci.Depends(db)) -> None:
        await asyncio.sleep(0.05)

    async def plain() -> None:
        enter()
        await asyncio.sleep(0.02)
        leave()

    records = [_record(0, uses_db, "test_uses_db", plan=plan_for(uses_db))]
    records += [_record(i, plain, f"test_plain_{i}") for i in range(1, 5)]

    results = run_suite(records, concurrency=4)

    assert [r.outcome for r in results] == [Outcome.PASSED] * 5
    assert peak() > 1


def test_piled_up_exclusive_contenders_never_take_a_concurrency_slot_from_others() -> None:
    """Many tests contending for the same `exclusive=` token pile up waiting on the gate, not on
    a concurrency slot -- an unrelated test is still admitted and runs concurrently with whichever
    contender currently holds the token, exactly as if the pile-up weren't there. Gating
    concurrency and exclusivity as two separate layers (a semaphore, then a gate behind it) would
    let a waiter hold its slot idle while it waits, so enough same-token contenders could fill
    every slot and starve this unrelated test out of the suite entirely."""
    enter, leave, peak = _overlap_tracker()

    @voci.fixture(exclusive="db")
    async def db() -> AsyncIterator[None]:
        yield None

    async def uses_db(x: None = voci.Depends(db)) -> None:
        enter()
        await asyncio.sleep(0.05)
        leave()

    async def plain() -> None:
        enter()
        await asyncio.sleep(0.02)
        leave()

    records = [_record(i, uses_db, f"test_uses_db_{i}", plan=plan_for(uses_db)) for i in range(3)]
    records.append(_record(3, plain, "test_plain"))

    results = run_suite(records, concurrency=3)

    assert [r.outcome for r in results] == [Outcome.PASSED] * 4
    assert peak() > 1


def test_admission_gate_release_cannot_be_interrupted_partway() -> None:
    """`AdmissionGate.release` is synchronous, so there is no point at which a cancellation
    already pending on its caller can interrupt it partway and leave `_running`/`_running_tokens`
    stuck -- which would deadlock every other test still waiting on the gate for the rest of the
    run. Released from inside a task that is already cancelling, which is exactly the shape
    `dispatch_one`'s outer `finally` runs in when a sibling's interrupt propagates."""

    async def scenario() -> None:
        gate = AdmissionGate(concurrency=4)
        await gate.acquire(frozenset({"db"}), solo=False)

        async def release_while_cancelling() -> None:
            try:
                await asyncio.sleep(60)
            finally:
                gate.release(frozenset({"db"}), solo=False)

        task = asyncio.ensure_future(release_while_cancelling())
        await asyncio.sleep(0)  # let it reach the sleep
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert gate._running == 0
        assert gate._running_tokens == set()

    run_async(scenario())


def test_admission_gate_admits_a_waiter_freed_slot_without_rescanning_the_queue() -> None:
    """A release hands its freed slot straight to a queued waiter instead of waking the whole
    queue to race for it. With every test's task created up front the queue is the whole suite,
    so re-testing every waiter's predicate on every release is quadratic in the suite's size."""

    async def scenario() -> None:
        gate = AdmissionGate(concurrency=1)
        await gate.acquire(frozenset(), solo=False)

        waiting = [asyncio.ensure_future(gate.acquire(frozenset(), solo=False)) for _ in range(3)]
        await asyncio.sleep(0)
        assert len(gate._waiters) == 3

        gate.release(frozenset(), solo=False)
        # Booked synchronously, inside `release` -- the admitted waiter holds the slot before
        # its own coroutine has had a chance to resume, so nothing else can take it first.
        assert gate._running == 1
        assert len(gate._waiters) == 2
        await waiting[0]

        for task in waiting[1:]:
            gate.release(frozenset(), solo=False)
            await task
        gate.release(frozenset(), solo=False)
        assert gate._running == 0
        assert not gate._waiters

    run_async(scenario())


def test_admission_gate_forgets_a_waiter_cancelled_before_it_is_admitted() -> None:
    """A queued test cancelled while it waits leaves nothing behind: no queue entry for a later
    release to hand a slot to (which nobody would ever give back), and no accounting of its own."""

    async def scenario() -> None:
        gate = AdmissionGate(concurrency=1)
        await gate.acquire(frozenset(), solo=False)

        waiter = asyncio.ensure_future(gate.acquire(frozenset({"db"}), solo=False))
        await asyncio.sleep(0)
        assert len(gate._waiters) == 1

        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not gate._waiters

        gate.release(frozenset(), solo=False)
        assert gate._running == 0
        assert gate._running_tokens == set()

    run_async(scenario())


def test_admission_gate_release_skips_a_waiter_cancelled_but_not_yet_dequeued() -> None:
    """`Task.cancel` cancels the future its target is suspended on synchronously, so a cancelled
    waiter is still in the queue until its own coroutine resumes to take itself out. A `release`
    landing in that window must drop it rather than complete its future -- which would raise
    `InvalidStateError` after having already booked it a slot nobody is left to give back."""

    async def scenario() -> None:
        gate = AdmissionGate(concurrency=1)
        await gate.acquire(frozenset(), solo=False)

        waiter = asyncio.ensure_future(gate.acquire(frozenset(), solo=False))
        await asyncio.sleep(0)
        waiter.cancel()
        assert gate._waiters[0].future.cancelled()  # cancelled, still queued

        gate.release(frozenset(), solo=False)
        assert gate._running == 0
        assert not gate._waiters

        with pytest.raises(asyncio.CancelledError):
            await waiter

    run_async(scenario())


def test_admission_gate_gives_back_a_slot_its_waiter_was_cancelled_before_claiming() -> None:
    """The one-tick window where a waiter has been admitted (its slot booked by `release`) but
    its own coroutine hasn't resumed yet to find out. A cancellation landing there must hand the
    slot back: the caller never reaches the `finally` that would release it, so the slot would
    otherwise be held by nobody for the rest of the run."""

    async def scenario() -> None:
        gate = AdmissionGate(concurrency=1)
        await gate.acquire(frozenset({"db"}), solo=False)

        waiter = asyncio.ensure_future(gate.acquire(frozenset({"db"}), solo=False))
        await asyncio.sleep(0)
        gate.release(frozenset({"db"}), solo=False)
        assert gate._running == 1  # booked for `waiter`, which has not resumed yet

        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert gate._running == 0
        assert gate._running_tokens == set()

    run_async(scenario())


def test_concurrency_one_serializes_module_scope_teardown_before_the_next_test_starts() -> None:
    """At `concurrency=1`, a module's fixture teardown (an async fixture whose teardown itself
    awaits) must fully finish before the *next* test's own body starts, not merely before the
    next test's result is recorded."""
    events: list[str] = []

    @voci.fixture(scope="module")
    async def per_module():
        yield None
        events.append("teardown-a start")
        await asyncio.sleep(0.05)
        events.append("teardown-a end")

    async def test_a(x: None = voci.Depends(per_module)) -> None:
        events.append("test-a")

    async def test_b() -> None:
        events.append("test-b start")
        await asyncio.sleep(0.01)
        events.append("test-b end")

    records = [
        _record(0, test_a, "test_a", plan=plan_for(test_a), path=Path("mod_a.py")),
        _record(1, test_b, "test_b", path=Path("mod_b.py")),
    ]

    results = run_suite(records, concurrency=1)

    assert [r.outcome for r in results] == [Outcome.PASSED] * 2
    assert events == [
        "test-a",
        "teardown-a start",
        "teardown-a end",
        "test-b start",
        "test-b end",
    ]


def test_results_are_in_logical_order_even_when_completion_order_is_scrambled() -> None:
    """Physical (completion) order is not the same as logical order under real concurrency --
    the later-indexed, shorter-sleeping test finishes first, but `results` still comes back
    index-ordered."""
    finished: list[int] = []

    def _sleeps(index: int, seconds: float) -> Callable[[], object]:
        async def test_func() -> None:
            await asyncio.sleep(seconds)
            finished.append(index)

        return test_func

    records = [
        _record(0, _sleeps(0, 0.06), "test_slowest"),
        _record(1, _sleeps(1, 0.03), "test_middle"),
        _record(2, _sleeps(2, 0.0), "test_fastest"),
    ]

    results = run_suite(records, concurrency=3)

    assert finished == [2, 1, 0]  # completion order really was scrambled, not just re-sorted
    assert [r.index for r in results] == [0, 1, 2]
    assert [r.id for r in results] == [r.id for r in records]
    assert [r.outcome for r in results] == [Outcome.PASSED] * 3


# `on_result`: called once per test, in real completion order, before `results[index]` is set.
# ------------------------------------------------------------------------------------------


def test_on_result_fires_in_completion_order_while_results_stays_logical() -> None:
    """Completion order is read from `on_result` itself rather than a side channel inside the
    test bodies. The pair of assertions (callback order vs. `results` order) is what
    distinguishes "the callback observes real completion order" from "the runner happened to be
    serial"."""

    def _sleeps(seconds: float) -> Callable[[], object]:
        async def test_func() -> None:
            await asyncio.sleep(seconds)

        return test_func

    records = [
        _record(0, _sleeps(0.06), "test_slowest"),
        _record(1, _sleeps(0.03), "test_middle"),
        _record(2, _sleeps(0.0), "test_fastest"),
    ]
    completion_order: list[int] = []

    results = run_suite(
        records, concurrency=3, on_result=lambda result: completion_order.append(result.index)
    )

    assert completion_order == [2, 1, 0]  # fastest-to-slowest: real completion order
    assert [r.index for r in results] == [0, 1, 2]  # logical order regardless


def test_on_result_sees_captured_output_already_folded_on() -> None:
    """`run_suite`'s own docstring: `on_result` fires with captured output "already folded on for
    a failing outcome" -- i.e. after `dispatch_one`'s `dataclasses.replace`, not the
    pre-`replace` object, which would still have an empty `captured_stdout` regardless of what
    the test printed."""

    async def _prints_then_fails() -> None:
        print("captured-before-callback")
        raise AssertionError("boom")

    seen: list[Result] = []

    run_suite(
        [_record(0, _prints_then_fails, "test_prints_then_fails")],
        on_result=lambda result: seen.append(result),
    )

    assert len(seen) == 1
    assert "captured-before-callback" in seen[0].captured_stdout


def test_maxfail_stops_dispatching_after_the_threshold() -> None:
    """`concurrency=1` so the stop point is exact: tests start in logical order, so nothing
    after the first failure ever runs."""
    started: list[str] = []

    async def _record_start_and_fail() -> None:
        started.append("fail")
        raise AssertionError("nope")

    async def _record_start() -> None:
        started.append("pass")

    records = [
        _record(0, _record_start_and_fail, "test_fails"),
        _record(1, _record_start, "test_after_a"),
        _record(2, _record_start, "test_after_b"),
    ]

    results = run_suite(records, concurrency=1, maxfail=1)

    assert started == ["fail"]
    # One result per test that actually ran, still in logical order.
    assert [result.id for result in results] == ["mod.py::test_fails"]


def test_maxfail_counts_failures_not_tests() -> None:
    records = [
        _record(0, _passes, "test_a"),
        _record(1, _fails, "test_b"),
        _record(2, _passes, "test_c"),
        _record(3, _fails, "test_d"),
        _record(4, _passes, "test_e"),
    ]

    results = run_suite(records, concurrency=1, maxfail=2)

    assert [result.id for result in results] == [
        "mod.py::test_a",
        "mod.py::test_b",
        "mod.py::test_c",
        "mod.py::test_d",
    ]


def test_maxfail_that_is_never_reached_runs_everything() -> None:
    records = [_record(0, _passes, "test_a"), _record(1, _fails, "test_b")]

    results = run_suite(records, concurrency=1, maxfail=2)

    assert len(results) == 2


def test_maxfail_counts_errors_and_timeouts_too() -> None:
    """Every `FAILING_OUTCOMES` member counts: a suite stopped early is stopped by whatever
    went wrong, not by the FAILED bucket specifically."""

    async def _boom() -> AsyncIterator[int]:
        raise RuntimeError("setup boom")
        yield 1  # pragma: no cover -- unreachable, keeps this a generator fixture

    broken = voci.fixture()(_boom)

    async def _needs_it(value: int = voci.Depends(broken)) -> None:
        pass  # pragma: no cover -- setup fails before the body runs

    records = [
        _record(0, _needs_it, "test_errors", plan=plan_for(_needs_it)),
        _record(1, _passes, "test_after"),
    ]

    results = run_suite(records, concurrency=1, maxfail=1)

    assert [result.outcome for result in results] == [Outcome.ERROR]


@pytest.mark.parametrize("bad", [0, -1])
def test_run_suite_rejects_non_positive_maxfail(bad: int) -> None:
    with pytest.raises(ValueError):
        run_suite([_record(0, _passes, "test_passes")], maxfail=bad)


@pytest.mark.parametrize("bad", [0, -1])
def test_run_suite_rejects_non_positive_concurrency(bad: int) -> None:
    with pytest.raises(ValueError):
        run_suite([_record(0, _passes, "test_passes")], concurrency=bad)


@pytest.mark.parametrize("bad", [0, -1, -0.5, float("nan"), float("inf")])
def test_run_suite_rejects_non_positive_or_non_finite_timeout(bad: float) -> None:
    with pytest.raises(ValueError):
        run_suite([_record(0, _passes, "test_passes")], timeout=bad)


def test_run_suite_rejects_an_isolated_record_without_isolated_config() -> None:
    """`@voci.isolated`'s subprocess needs a rootdir to re-collect from -- run_suite refuses to
    silently run it in-process instead of raising, which is exactly the bug this mark used to
    have (ROADMAP.md, before the subprocess tier existed)."""

    @voci.isolated
    async def test_func() -> None:
        pass

    with pytest.raises(ValueError, match=r"@voci\.isolated"):
        run_suite([_record(0, test_func, "test_func")])


def test_keyboard_interrupt_among_several_concurrent_siblings_propagates_cleanly() -> None:
    """When one test among several genuinely-concurrent siblings raises `KeyboardInterrupt`,
    it must come out of `run_suite` as a bare `KeyboardInterrupt`, not a `BaseExceptionGroup`."""

    async def _raises_keyboard_interrupt() -> None:
        raise KeyboardInterrupt

    async def _sleeps_long() -> None:
        await asyncio.sleep(10)

    records = [
        _record(0, _sleeps_long, "test_sibling_a"),
        _record(1, _raises_keyboard_interrupt, "test_interrupt"),
        _record(2, _sleeps_long, "test_sibling_b"),
    ]

    with pytest.raises(KeyboardInterrupt) as exc_info:
        run_suite(records, concurrency=3)
    assert not isinstance(exc_info.value, BaseExceptionGroup)


def test_two_siblings_interrupting_at_once_still_yields_a_bare_interrupt_not_a_group() -> None:
    """Two different siblings can genuinely raise `KeyboardInterrupt`/`SystemExit` at once;
    which one wins is a genuine race this test cannot pin without flaking, so it only asserts
    the invariant that holds regardless: a bare interrupt of one of the two raised types, never
    a `BaseExceptionGroup`."""

    async def _raises_keyboard_interrupt() -> None:
        raise KeyboardInterrupt

    async def _raises_system_exit() -> None:
        raise SystemExit(7)

    records = [
        _record(0, _raises_keyboard_interrupt, "test_ki"),
        _record(1, _raises_system_exit, "test_se"),
    ]

    with pytest.raises((KeyboardInterrupt, SystemExit)) as exc_info:
        run_suite(records, concurrency=2)
    assert not isinstance(exc_info.value, BaseExceptionGroup)


def test_a_test_raising_its_own_group_containing_a_keyboard_interrupt_is_just_a_failure() -> None:
    """A test body that raises its own `BaseExceptionGroup` wrapping a `KeyboardInterrupt`:
    `_run_one`'s `except BaseException` catches it first -- a `BaseExceptionGroup` is not
    itself an instance of `KeyboardInterrupt`/`SystemExit`/`CancelledError`, so it never
    matches the re-raise guards -- and reports it as an ordinary `FAILED` result, same as any
    other exception a test body raises."""

    async def _raises_a_group_containing_a_keyboard_interrupt() -> None:
        raise BaseExceptionGroup("boom", [KeyboardInterrupt()])

    (result,) = run_suite(
        [_record(0, _raises_a_group_containing_a_keyboard_interrupt, "test_group")]
    )

    assert result.outcome is Outcome.FAILED
    assert result.failure is not None
    assert "KeyboardInterrupt" in result.failure


def test_module_scope_fixture_shared_and_torn_down_once_under_real_overlap() -> None:
    """Every test sleeps inside its own body, so with `concurrency=5` all five are genuinely in
    flight together (pinned by `peak`) -- and the module fixture must still be built exactly
    once (single-flight `ScopeStore.acquire`) and torn down exactly once, not once per test."""
    builds: list[int] = []
    torn_down: list[str] = []
    in_flight = 0
    peak = 0

    @voci.fixture(scope="module")
    def per_module():
        builds.append(1)
        yield len(builds)
        torn_down.append("closed")

    async def test_func(x: int = voci.Depends(per_module)) -> None:
        nonlocal in_flight, peak
        assert x == 1
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1

    records = [_record(i, test_func, f"test_{i}", plan=plan_for(test_func)) for i in range(5)]

    results = run_suite(records, concurrency=5)

    assert [r.outcome for r in results] == [Outcome.PASSED] * 5
    assert peak > 1  # genuinely overlapped, not an artifact of running one at a time
    assert len(builds) == 1
    assert torn_down == ["closed"]


def test_module_scope_survives_a_sibling_setup_failure_under_concurrency() -> None:
    """`test_a`'s setup acquires the module fixture successfully and then fails on its second
    fixture; `test_b` (slower, so it is still mid-setup when `test_a` fails) shares the same
    module fixture and passes. A sibling's setup failure must not release a module fixture that
    another still-mid-flight sibling holds a live reference to -- both `builds` and `torn_down`
    stay at one."""
    builds: list[int] = []
    torn_down: list[str] = []

    @voci.fixture(scope="module")
    def per_module():
        builds.append(1)
        yield len(builds)
        torn_down.append("closed")

    @voci.fixture()
    def broken():
        raise RuntimeError("setup boom")

    @voci.fixture()
    async def slow():
        await asyncio.sleep(0.03)
        return "ok"

    async def test_a(m: int = voci.Depends(per_module), b: int = voci.Depends(broken)) -> None:
        raise AssertionError("must never run: setup already failed")

    async def test_b(s: str = voci.Depends(slow), m: int = voci.Depends(per_module)) -> None:
        assert m == 1

    records = [
        _record(0, test_a, "test_a", plan=plan_for(test_a), path=Path("mod.py")),
        _record(1, test_b, "test_b", plan=plan_for(test_b), path=Path("mod.py")),
    ]

    results = run_suite(records, concurrency=2)

    assert results[0].outcome is Outcome.ERROR
    assert results[1].outcome is Outcome.PASSED
    assert len(builds) == 1
    assert torn_down == ["closed"]


def test_module_scope_survives_a_sibling_timeout_mid_setup_under_concurrency() -> None:
    """A `--timeout` deadline firing mid-setup reaches the same `_di.setup` cleanup path (via
    `asyncio.CancelledError`) as an ordinary fixture failure does."""
    builds: list[int] = []
    torn_down: list[str] = []

    @voci.fixture(scope="module")
    def per_module():
        builds.append(1)
        yield len(builds)
        torn_down.append("closed")

    @voci.fixture()
    async def hangs():
        await asyncio.sleep(10)

    @voci.fixture()
    async def slow():
        await asyncio.sleep(0.03)
        return "ok"

    async def test_a(m: int = voci.Depends(per_module), h: object = voci.Depends(hangs)) -> None:
        raise AssertionError("must never run: setup timed out")

    async def test_b(s: str = voci.Depends(slow), m: int = voci.Depends(per_module)) -> None:
        assert m == 1

    records = [
        _record(0, test_a, "test_a", plan=plan_for(test_a), path=Path("mod.py")),
        _record(1, test_b, "test_b", plan=plan_for(test_b), path=Path("mod.py")),
    ]

    results = run_suite(records, concurrency=2, timeout=0.05)

    assert results[0].outcome is Outcome.TIMEOUT
    assert results[1].outcome is Outcome.PASSED
    assert len(builds) == 1
    assert torn_down == ["closed"]


# `--timeout`: an optional `asyncio.timeout` around each test's setup+call.
# ------------------------------------------------------------------------------------------


def test_timeout_produces_timeout_outcome_with_a_failure_message() -> None:
    async def _hangs() -> None:
        await asyncio.sleep(10)

    (result,) = run_suite([_record(0, _hangs, "test_hangs")], timeout=0.05)

    assert result.outcome is Outcome.TIMEOUT
    assert result.failure is not None
    assert "timeout" in result.failure.lower()
    assert "0.05" in result.failure


def test_timeout_survives_the_body_substituting_a_different_exception() -> None:
    """A test that catches the injected `CancelledError` and raises something else instead of
    letting it propagate must still be reported `TIMEOUT`, not `FAILED` -- `_run_one`
    cross-checks the `Timeout` object's own `.expired()` state for this, not just the
    exception channel."""

    async def _absorbs_the_cancellation() -> None:
        try:
            await asyncio.sleep(10)
        except BaseException:
            raise ValueError("cleanup did something else instead") from None

    (result,) = run_suite([_record(0, _absorbs_the_cancellation, "test_absorbs")], timeout=0.03)

    assert result.outcome is Outcome.TIMEOUT
    assert result.failure is not None
    assert "timeout" in result.failure.lower()
    assert "cleanup did something else instead" in result.failure


def test_timeout_while_building_a_shared_fixture_does_not_poison_it_for_later_tests() -> None:
    """The timed-out test's deadline is its own. A `session`-scope fixture it happened to be
    constructing when the budget expired must still be built for the tests behind it, rather than
    every one of them erroring with the `CancelledError` that stopped the first."""
    builds: list[int] = []

    @voci.fixture(scope="session")
    async def slow() -> AsyncIterator[str]:
        builds.append(1)
        await asyncio.sleep(0.06)
        yield "db"

    @voci.timeout(0.02)
    async def times_out(db: str = voci.Depends(slow)) -> None:
        raise AssertionError("must never run: setup timed out")

    async def uses_slow(db: str = voci.Depends(slow)) -> None:
        assert db == "db"

    records = [
        _record(0, times_out, "test_times_out", plan=plan_for(times_out)),
        _record(1, uses_slow, "test_a", plan=plan_for(uses_slow)),
        _record(2, uses_slow, "test_b", plan=plan_for(uses_slow)),
    ]

    results = run_suite(records, concurrency=1)

    assert [r.outcome for r in results] == [Outcome.TIMEOUT, Outcome.PASSED, Outcome.PASSED]
    # Rebuilt once by the test behind the timed-out one, then shared from the cache as usual.
    assert len(builds) == 2


def test_a_timed_out_waiter_on_a_shared_fixture_does_not_take_its_siblings_down_with_it() -> None:
    """The concurrent shape of the above: the test whose budget expires is one of the *waiters*
    on the shared construction, not the one running it. Its deadline is still its own -- the test
    actually constructing the fixture must finish, and every other waiter must get the value."""

    @voci.fixture(scope="session")
    async def slow() -> AsyncIterator[str]:
        await asyncio.sleep(0.1)
        yield "db"

    async def patient(db: str = voci.Depends(slow)) -> None:
        assert db == "db"

    @voci.timeout(0.02)
    async def impatient(db: str = voci.Depends(slow)) -> None:
        raise AssertionError("must never run: setup timed out")

    records = [
        _record(0, patient, "test_first", plan=plan_for(patient)),
        _record(1, impatient, "test_impatient", plan=plan_for(impatient)),
        _record(2, patient, "test_third", plan=plan_for(patient)),
    ]

    results = run_suite(records, concurrency=3)

    assert [r.outcome for r in results] == [Outcome.PASSED, Outcome.TIMEOUT, Outcome.PASSED]


def test_a_timed_out_tests_teardown_is_bounded_rather_than_hanging_the_run() -> None:
    """The fixture a `--timeout` just fired on is the first suspect for hanging in its own
    teardown too. Leaving that teardown unbounded would hold the test's admission slot forever
    and hang the whole run with nothing reported -- exactly the failure `--timeout` exists to
    bound -- so a timed-out test's teardown gets the same grace a cancelled one's does."""

    @voci.fixture()
    async def never_tears_down() -> AsyncIterator[str]:
        yield "x"
        await asyncio.sleep(60)

    async def hangs(v: str = voci.Depends(never_tears_down)) -> None:
        await asyncio.sleep(10)

    records = [_record(0, hangs, "test_hangs", plan=plan_for(hangs))]

    (result,) = run_suite(records, timeout=0.02, teardown_grace=0.05)

    assert result.outcome is Outcome.TIMEOUT
    assert result.failure is not None
    assert "may not have been fully torn down" in result.failure


def test_timeout_clock_starts_after_admission_not_at_dispatch() -> None:
    """Each test's `--timeout` budget is a fresh `asyncio.timeout` entered only once `_run_one`
    actually starts running (after `AdmissionGate.acquire`), not a shared deadline counted from
    when `run_suite` was first called. At `concurrency=1`, four tests that each sleep 0.05s (0.2s
    of total queued time) but each carry a 0.1s *per-test* budget all pass."""

    async def _sleeps() -> None:
        await asyncio.sleep(0.05)

    records = [_record(i, _sleeps, f"test_{i}") for i in range(4)]

    results = run_suite(records, concurrency=1, timeout=0.1)

    assert [r.outcome for r in results] == [Outcome.PASSED] * 4


def test_timeout_during_setup_produces_timeout_and_does_not_run_teardown() -> None:
    """A hanging *fixture* (setup never returns) is distinct from a hanging test body: setup
    never completed, so there is nothing for teardown to release -- `_run_one`'s `setup_done`
    gate must stay `False`, not attempt (or fake) a teardown for a fixture that was never fully
    acquired."""
    torn_down: list[str] = []

    @voci.fixture()
    async def slow_setup():
        await asyncio.sleep(10)
        yield 1
        torn_down.append("closed")  # pragma: no cover -- must never be reached

    async def test_func(x: int = voci.Depends(slow_setup)) -> None:
        raise AssertionError("must never run: setup never finished")

    (result,) = run_suite(
        [_record(0, test_func, "test_func", plan=plan_for(test_func))], timeout=0.05
    )

    assert result.outcome is Outcome.TIMEOUT
    assert torn_down == []


def test_timeout_during_call_still_runs_teardown_for_what_setup_acquired() -> None:
    """The opposite case: setup succeeds well within budget, and it's the test *body* that hangs.
    A test must not leak its fixtures just because its own call timed out -- teardown still
    runs."""
    torn_down: list[str] = []

    @voci.fixture()
    def quick():
        yield 1
        torn_down.append("closed")

    async def test_func(x: int = voci.Depends(quick)) -> None:
        assert x == 1
        await asyncio.sleep(10)

    (result,) = run_suite(
        [_record(0, test_func, "test_func", plan=plan_for(test_func))], timeout=0.05
    )

    assert result.outcome is Outcome.TIMEOUT
    assert torn_down == ["closed"]


def test_timeout_mark_overrides_the_suite_wide_timeout() -> None:
    """`@voci.timeout(...)` wins over `run_suite`'s own `timeout=` for that one test, even
    when the suite budget would otherwise have been generous enough to let it pass."""

    @voci.timeout(0.05)
    async def _hangs() -> None:
        await asyncio.sleep(10)

    (result,) = run_suite([_record(0, _hangs, "test_hangs")], timeout=10)

    assert result.outcome is Outcome.TIMEOUT
    assert result.failure is not None
    assert "0.05" in result.failure


def test_timeout_mark_can_grant_more_time_than_the_suite_budget() -> None:
    @voci.timeout(1)
    async def _sleeps() -> None:
        await asyncio.sleep(0.05)

    (result,) = run_suite([_record(0, _sleeps, "test_sleeps")], timeout=0.02)

    assert result.outcome is Outcome.PASSED


# `@voci.xfail`: a failing call reports XFAILED, a passing one XPASSED (or FAILED, if strict).
# ------------------------------------------------------------------------------------------


def test_xfail_call_failure_reports_xfailed_not_failed() -> None:
    @voci.xfail("known broken")
    async def _fails() -> None:
        raise AssertionError("nope")

    (result,) = run_suite([_record(0, _fails, "test_fails")])

    assert result.outcome is Outcome.XFAILED
    assert result.failure is not None
    assert "known broken" in result.failure


def test_xfail_call_passing_reports_xpassed_not_passed() -> None:
    @voci.xfail("thought this was broken")
    async def _passes() -> None:
        pass

    (result,) = run_suite([_record(0, _passes, "test_passes")])

    assert result.outcome is Outcome.XPASSED


def test_xfail_strict_call_passing_reports_failed() -> None:
    @voci.xfail("thought this was broken", strict=True)
    async def _passes() -> None:
        pass

    (result,) = run_suite([_record(0, _passes, "test_passes")])

    assert result.outcome is Outcome.FAILED
    assert result.failure is not None
    assert "strict" in result.failure


def test_xfail_raises_matching_the_exception_type_reports_xfailed() -> None:
    @voci.xfail("known broken", raises=ValueError)
    async def _fails() -> None:
        raise ValueError("nope")

    (result,) = run_suite([_record(0, _fails, "test_fails")])

    assert result.outcome is Outcome.XFAILED


def test_xfail_raises_not_matching_the_exception_type_reports_failed() -> None:
    """A `raises=` mismatch is a real regression, not the expected failure -- reported FAILED,
    same as no `xfail` mark at all."""

    @voci.xfail("known broken", raises=ValueError)
    async def _fails() -> None:
        raise TypeError("wrong kind of broken")

    (result,) = run_suite([_record(0, _fails, "test_fails")])

    assert result.outcome is Outcome.FAILED


def test_xfail_whose_condition_does_not_hold_reports_failed() -> None:
    """The condition is decided at collection, so what reaches the runner is a record with no
    expectation on it at all -- and a failure it reports as the failure it is."""

    @voci.xfail("only on some platform", condition=False)
    async def _fails() -> None:
        raise AssertionError("nope")

    (result,) = run_suite([_record(0, _fails, "test_fails")])

    assert result.outcome is Outcome.FAILED


def test_xfail_whose_condition_holds_reports_xfailed() -> None:
    @voci.xfail("known broken here", condition=lambda: True)
    async def _fails() -> None:
        raise AssertionError("nope")

    (result,) = run_suite([_record(0, _fails, "test_fails")])

    assert result.outcome is Outcome.XFAILED


def test_marks_are_read_from_the_record_not_the_function() -> None:
    """Two records over one function, as a `voci.case(..., marks=...)` builds: the one carrying
    the expectation reports XFAILED and the other, the same failure, FAILED."""

    async def _fails() -> None:
        raise AssertionError("nope")

    expected = _record(0, _fails, "test_fails[0]", marks=marks_of(_expects_failure))
    plain = _record(1, _fails, "test_fails[1]")

    results = run_suite([expected, plain])

    assert [result.outcome for result in results] == [Outcome.XFAILED, Outcome.FAILED]


@voci.xfail("this case only")
def _expects_failure() -> None: ...


def test_xfail_does_not_apply_to_a_setup_error() -> None:
    """`xfail` wraps the call phase only -- a fixture that raises during setup still reports
    ERROR, `xfail` mark or not."""

    @voci.fixture()
    def broken() -> int:
        raise RuntimeError("setup boom")

    @voci.xfail("expected to fail")
    async def test_func(x: int = voci.Depends(broken)) -> None:
        raise AssertionError("must never run: setup already failed")

    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert result.outcome is Outcome.ERROR


def test_xfail_does_not_apply_to_a_timeout() -> None:
    @voci.xfail("expected to fail")
    async def _hangs() -> None:
        await asyncio.sleep(10)

    (result,) = run_suite([_record(0, _hangs, "test_hangs")], timeout=0.05)

    assert result.outcome is Outcome.TIMEOUT


# `voci.Skipped`/`voci.Failed`: the runtime counterparts of `@voci.skip` and an assertion --
# pytest's `pytest.skip()`/`pytest.fail()`, raised as a statement rather than read off a mark.
# ------------------------------------------------------------------------------------------


def test_skip_raised_in_the_call_phase_reports_skipped_with_its_reason() -> None:
    async def _skips() -> None:
        raise voci.Skipped("no backend configured")

    (result,) = run_suite([_record(0, _skips, "test_skips")])

    assert result.outcome is Outcome.SKIPPED
    assert result.failure == "no backend configured"


def test_skip_raised_during_fixture_setup_reports_skipped_not_error() -> None:
    @voci.fixture()
    def unavailable() -> int:
        raise voci.Skipped("backend not installed")

    async def test_func(x: int = voci.Depends(unavailable)) -> None:
        raise AssertionError("must never run: setup already skipped")

    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert result.outcome is Outcome.SKIPPED
    assert result.failure == "backend not installed"


def test_skip_in_the_call_phase_still_tears_fixtures_down() -> None:
    torn_down = False

    @voci.fixture()
    def resource():
        yield 1
        nonlocal torn_down
        torn_down = True

    async def test_func(x: int = voci.Depends(resource)) -> None:
        raise voci.Skipped("no backend")

    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert result.outcome is Outcome.SKIPPED
    assert torn_down


def test_skip_in_the_call_phase_takes_priority_over_an_xfail_mark() -> None:
    """A skip reached mid-call is reported as skipped regardless of what an `xfail` mark on the
    same test expected -- pytest's own imperative skip takes the same priority."""

    @voci.xfail("expected to fail, not skip")
    async def _skips() -> None:
        raise voci.Skipped("no backend")

    (result,) = run_suite([_record(0, _skips, "test_skips")])

    assert result.outcome is Outcome.SKIPPED


def test_a_teardown_failure_after_a_call_phase_skip_still_reports_error() -> None:
    """Mirrors teardown's priority over a passing or failing call: a skip that reached a clean
    call phase is not the last word if teardown then fails."""

    @voci.fixture()
    def flaky_teardown():
        yield 1
        raise RuntimeError("teardown boom")

    async def test_func(x: int = voci.Depends(flaky_teardown)) -> None:
        raise voci.Skipped("no backend")

    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert result.outcome is Outcome.ERROR
    assert result.failure is not None
    assert "teardown boom" in result.failure


def test_failed_raised_in_the_call_phase_reports_failed_with_its_message() -> None:
    """`voci.Failed` needs no special-casing in `_run_one`: it's caught by the same generic
    handler any other exception is, and reads as an ordinary failure."""

    async def _fails() -> None:
        raise voci.Failed("unreachable")

    (result,) = run_suite([_record(0, _fails, "test_fails")])

    assert result.outcome is Outcome.FAILED
    assert result.failure is not None
    assert "unreachable" in result.failure


def test_failed_raised_in_the_call_phase_reports_xfailed_under_an_xfail_mark() -> None:
    """Unlike `Skipped`, `Failed` is read through `xfail` exactly like any other failure."""

    @voci.xfail("known broken")
    async def _fails() -> None:
        raise voci.Failed("unreachable")

    (result,) = run_suite([_record(0, _fails, "test_fails")])

    assert result.outcome is Outcome.XFAILED


# Deliberately not shared with report/test_terminal.py's `_result`: that one carries the full
# Reporter surface (failure text, captured output, log records); this one only needs `outcome`
# for `exit_code_for`.
def _result(outcome: Outcome) -> Result:
    return Result(
        id="mod.py::t", index=0, outcome=outcome, duration=0.0, failure=None, failure_summary=None
    )


def _error(name: str = "mod.py") -> CollectionError:
    return CollectionError(path=Path(name), message="boom")


def test_exit_code_all_passed_is_zero() -> None:
    assert exit_code_for([_result(Outcome.PASSED)], []) == 0


def test_exit_code_with_a_failure_is_one() -> None:
    assert exit_code_for([_result(Outcome.PASSED), _result(Outcome.FAILED)], []) == 1


def test_exit_code_with_an_error_is_one() -> None:
    """`error` and `failed` both contribute `1` to the exit code."""
    assert exit_code_for([_result(Outcome.PASSED), _result(Outcome.ERROR)], []) == 1


def test_exit_code_with_a_collection_error_and_no_records_is_one() -> None:
    assert exit_code_for([], [_error()]) == 1


def test_exit_code_nothing_collected_is_five() -> None:
    assert exit_code_for([], []) == 5


def test_exit_code_all_skipped_is_zero_not_five() -> None:
    """`skipped` contributes `0` to the exit code, same as `passed` -- a suite that is entirely
    skip-marked tests was genuinely collected, unlike an empty selection."""
    assert exit_code_for([], [], skipped=3) == 0


def test_exit_code_skipped_does_not_mask_a_real_failure() -> None:
    assert exit_code_for([_result(Outcome.FAILED)], [], skipped=1) == 1


def test_exit_code_xfailed_and_xpassed_are_not_failures() -> None:
    """Both mean the test behaved exactly as its `xfail` mark said it would -- neither should
    turn a run red. A strict xpass reports FAILED instead of XPASSED (covered in test_run.py's
    `@voci.xfail` section), so it never reaches `exit_code_for` as XPASSED."""
    results = [_result(Outcome.PASSED), _result(Outcome.XFAILED), _result(Outcome.XPASSED)]
    assert exit_code_for(results, []) == 0


def test_exit_code_runtime_skipped_is_not_a_failure() -> None:
    """A `voci.Skipped` result reaches `exit_code_for` inside `results` itself (unlike a
    `skip`-marked test, which never runs), and contributes `0` the same as PASSED."""
    results = [_result(Outcome.PASSED), _result(Outcome.SKIPPED)]
    assert exit_code_for(results, []) == 0


# `unittest.mock`: solo scheduling for a patch-decorated test, and the guard on the rest.
# ------------------------------------------------------------------------------------------


def test_a_patching_test_never_overlaps_with_anything_else() -> None:
    """A test collection found `unittest.mock` patching on takes the whole gate, exactly as a
    `@voci.solo`-marked one does -- a patch is a write every concurrent test would see."""
    active: set[str] = set()
    violations: list[frozenset[str]] = []

    def _make(name: str) -> Callable[[], object]:
        async def test_func() -> None:
            active.add(name)
            if "patching" in active and active != {"patching"}:
                violations.append(frozenset(active))
            await asyncio.sleep(0.02)
            active.discard(name)

        return test_func

    records = [_record(0, _make("patching"), "test_patching", patches=("getcwd",))]
    records += [_record(i, _make(f"ordinary_{i}"), f"test_ordinary_{i}") for i in range(1, 6)]

    results = run_suite(records, concurrency=4)

    assert [r.outcome for r in results] == [Outcome.PASSED] * 6
    assert violations == []


def test_an_isolated_test_is_not_serialized_for_its_patching() -> None:
    """`@voci.isolated` patches its own subprocess, where there is nothing else to disturb."""

    async def test_isolated() -> None:
        pass

    async def test_plain() -> None:
        pass

    isolated_record = _record(0, voci.isolated(test_isolated), "test_isolated", patches=("x",))
    plain_record = _record(1, test_plain, "test_plain", patches=("x",))

    assert not solo_for_patching(isolated_record)
    assert solo_for_patching(plain_record)


def test_a_patch_entered_by_a_concurrent_test_fails_that_test() -> None:
    """`with mock.patch(...)` inside a body is invisible at collection, so it is caught where it
    installs: the test fails and the patch never reaches `os`."""

    async def test_patches_in_its_body() -> None:
        with mock.patch("os.getcwd", return_value="/x"):
            pass

    (result,) = run_suite([_record(0, test_patches_in_its_body, "test_patches_in_its_body")])

    assert result.outcome is Outcome.FAILED
    assert result.failure_summary is not None
    assert "getcwd" in result.failure_summary
    assert os.getcwd() != "/x"


def test_a_solo_test_may_patch_in_its_body() -> None:
    async def test_patches_in_its_body() -> None:
        with mock.patch("os.getcwd", return_value="/x"):
            assert os.getcwd() == "/x"

    records = [_record(0, voci.solo(test_patches_in_its_body), "test_patches_in_its_body")]

    (result,) = run_suite(records)

    assert result.outcome is Outcome.PASSED


def test_the_patch_guard_does_not_outlive_the_run() -> None:
    """It replaces a method on `unittest.mock`'s own class, so leaving it installed would
    change how patching behaves for whatever runs after `run_suite` returns."""
    run_suite([_record(0, _passes, "test_passes")])

    with mock.patch("os.getcwd", return_value="/x"):
        assert os.getcwd() == "/x"


# A call phase that raised nothing and still tested nothing: a returned value, or a coroutine
# it never awaited. See `_run.safety.call_misuse` for the messages themselves.
# ------------------------------------------------------------------------------------------


async def _returns_a_value() -> int:
    return 7


async def _forgets_to_await() -> None:
    _passes()  # pyrefly: ignore[unused-coroutine]  -- the whole point of these tests


def _sync_forgets_to_await() -> None:
    _passes()  # pyrefly: ignore[unused-coroutine]  -- the whole point of these tests


def test_a_test_that_returns_a_value_fails() -> None:
    (result,) = run_suite([_record(0, _returns_a_value, "test_returns")])

    assert result.outcome is Outcome.FAILED
    assert result.failure_summary is not None
    assert "returned 7" in result.failure_summary


def test_a_test_that_forgets_an_await_fails() -> None:
    (result,) = run_suite([_record(0, _forgets_to_await, "test_forgets")])

    assert result.outcome is Outcome.FAILED
    assert result.failure_summary is not None
    assert "never awaited" in result.failure_summary


def test_a_sync_test_that_forgets_an_await_fails() -> None:
    """A sync body runs in a worker thread, whose context is a copy of the dispatching task's
    -- so the coroutine it drops is still filed against this test and no other."""
    (result,) = run_suite([_record(0, _sync_forgets_to_await, "test_sync_forgets")])

    assert result.outcome is Outcome.FAILED
    assert result.failure_summary is not None
    assert "never awaited" in result.failure_summary


def test_one_test_forgetting_an_await_does_not_fail_its_concurrent_siblings() -> None:
    records = [
        _record(0, _forgets_to_await, "test_forgets"),
        *[_record(i, _passes, f"test_ok_{i}") for i in range(1, 8)],
    ]

    results = run_suite(records, concurrency=8)

    assert [result.outcome for result in results] == [Outcome.FAILED] + [Outcome.PASSED] * 7


def test_an_xfail_mark_does_not_absorb_a_returned_value() -> None:
    """`xfail` re-reads what the call phase *raised*; a returned value is not a failure the
    mark could have predicted, so it is reported as one."""

    async def test_returns() -> int:
        return 7

    marked = voci.xfail(reason="known")(test_returns)

    (result,) = run_suite([_record(0, marked, "test_returns")])

    assert result.outcome is Outcome.FAILED


def test_the_unawaited_hook_does_not_outlive_the_run() -> None:
    run_suite([_record(0, _passes, "test_passes")])

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _passes()  # pyrefly: ignore[unused-coroutine]  -- the whole point of these tests
    assert any("was never awaited" in str(warning.message) for warning in caught)


# Stopping early: `--maxfail` and a Ctrl-C both cancel what is in flight.
# ----------------------------------------------------------------------


async def _sleeps_a_long_time() -> None:
    await asyncio.sleep(30)


def test_maxfail_cancels_the_tests_still_in_flight() -> None:
    """The point of cancelling rather than waiting: a run stopped by its first failure does
    not sit through a 30-second sibling before it can report."""
    records = [
        _record(0, _sleeps_a_long_time, "test_slow_a"),
        _record(1, _sleeps_a_long_time, "test_slow_b"),
        _record(2, _fails, "test_fails"),
    ]

    started = time.monotonic()
    results = run_suite(records, concurrency=3, maxfail=1)
    elapsed = time.monotonic() - started

    assert elapsed < 10
    by_id = {result.id: result.outcome for result in results}
    assert by_id["mod.py::test_fails"] is Outcome.FAILED
    assert by_id["mod.py::test_slow_a"] is Outcome.CANCELLED
    assert by_id["mod.py::test_slow_b"] is Outcome.CANCELLED


def test_a_cancelled_test_still_tears_its_fixtures_down() -> None:
    torn_down: list[str] = []

    @voci.fixture()
    async def resource() -> AsyncIterator[str]:
        yield "resource"
        torn_down.append("resource")

    async def test_slow(value: str = voci.Depends(resource)) -> None:
        await asyncio.sleep(30)

    records = [
        _record(0, test_slow, "test_slow", plan=plan_for(test_slow)),
        _record(1, _fails, "test_fails"),
    ]

    results = run_suite(records, concurrency=2, maxfail=1)

    assert torn_down == ["resource"]
    assert {result.outcome for result in results} == {Outcome.CANCELLED, Outcome.FAILED}


def test_a_cancelled_tests_teardown_is_time_boxed() -> None:
    """A fixture waiting on something that will never come must not hold a stopped run open
    -- the release is waited on for `teardown_grace` and then given up on."""

    @voci.fixture()
    async def never_releases() -> AsyncIterator[str]:
        yield "resource"
        await asyncio.sleep(30)

    async def test_slow(value: str = voci.Depends(never_releases)) -> None:
        await asyncio.sleep(30)

    records = [
        _record(0, test_slow, "test_slow", plan=plan_for(test_slow)),
        _record(1, _fails, "test_fails"),
    ]

    started = time.monotonic()
    results = run_suite(records, concurrency=2, maxfail=1, teardown_grace=0.2)
    elapsed = time.monotonic() - started

    assert elapsed < 10
    cancelled = next(result for result in results if result.outcome is Outcome.CANCELLED)
    assert cancelled.failure is not None
    assert "may not have been fully torn down" in cancelled.failure


def test_a_cancelled_test_reports_why_it_was_cancelled() -> None:
    records = [
        _record(0, _sleeps_a_long_time, "test_slow"),
        _record(1, _fails, "test_fails"),
    ]

    results = run_suite(records, concurrency=2, maxfail=1)

    cancelled = next(result for result in results if result.outcome is Outcome.CANCELLED)
    assert cancelled.failure_summary == "cancelled: the run stopped after --maxfail"


def test_a_test_that_had_not_started_is_dropped_rather_than_cancelled() -> None:
    """Two different things a stopped run does: what was running is cancelled and reports,
    what never started leaves no result at all -- which is what `--maxfail`'s "not run" count
    is derived from."""
    records = [
        _record(0, _fails, "test_fails"),
        _record(1, _passes, "test_never_starts"),
    ]

    results = run_suite(records, concurrency=1, maxfail=1)

    assert [result.id for result in results] == ["mod.py::test_fails"]


@pytest.mark.parametrize("bad", [0, -1, float("inf")])
def test_run_suite_rejects_a_non_positive_teardown_grace(bad: float) -> None:
    with pytest.raises(ValueError):
        run_suite([_record(0, _passes, "test_passes")], teardown_grace=bad)


@pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="run_suite only takes SIGINT on the main thread",
)
def test_a_ctrl_c_cancels_the_run_and_reports_what_was_in_flight() -> None:
    """The signal is sent from inside a test body, so it can only land while `run_suite`'s own
    handler is installed -- which is also the only state this behavior exists in."""
    interrupted: list[bool] = []

    async def test_interrupts() -> None:
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.sleep(30)

    records = [
        _record(0, test_interrupts, "test_interrupts"),
        _record(1, _sleeps_a_long_time, "test_slow"),
    ]

    started = time.monotonic()
    results = run_suite(records, concurrency=2, on_interrupt=lambda: interrupted.append(True))
    elapsed = time.monotonic() - started

    assert elapsed < 10
    assert interrupted == [True]
    assert [result.outcome for result in results] == [Outcome.CANCELLED, Outcome.CANCELLED]
    assert results[0].failure_summary == "cancelled: the run was interrupted (Ctrl-C)"


@pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="run_suite only takes SIGINT on the main thread",
)
def test_the_interrupt_handler_does_not_outlive_the_run() -> None:
    before = signal.getsignal(signal.SIGINT)

    run_suite([_record(0, _passes, "test_passes")])

    assert signal.getsignal(signal.SIGINT) is before


def test_a_test_may_fan_out_over_more_threads_than_the_run_has_concurrency() -> None:
    """The executor a run installs is also where a test's own `asyncio.to_thread(...)` lands,
    so sizing it to `concurrency` alone would let a single test deadlock against itself under
    `--serial`."""
    barrier = threading.Barrier(2)

    async def test_fans_out() -> None:
        await asyncio.gather(
            asyncio.to_thread(barrier.wait, 10), asyncio.to_thread(barrier.wait, 10)
        )

    (result,) = run_suite([_record(0, test_fans_out, "test_fans_out")], concurrency=1)

    assert result.outcome is Outcome.PASSED


@pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="run_suite only takes SIGINT on the main thread",
)
def test_a_sigint_handler_voci_could_not_restore_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`signal.getsignal` returns `None` for a handler installed from outside Python. Voci
    has nothing to put back afterwards, so it doesn't take the signal in the first place."""
    installed: list[object] = []
    monkeypatch.setattr(signal, "getsignal", lambda _signum: None)
    monkeypatch.setattr(signal, "signal", lambda _signum, handler: installed.append(handler))

    (result,) = run_suite([_record(0, _passes, "test_passes")])

    assert result.outcome is Outcome.PASSED
    assert installed == []


@pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="run_suite only takes SIGINT on the main thread",
)
def test_a_second_ctrl_c_aborts_and_leaves_no_watchdog_thread_behind() -> None:
    """The `KeyboardInterrupt` the second Ctrl-C raises never resumes the coroutine driving
    the run, so anything that coroutine would have stopped on its way out has to be stopped
    from outside it too."""

    async def test_interrupts_twice() -> None:
        os.kill(os.getpid(), signal.SIGINT)
        try:
            # Where the first Ctrl-C's cancellation lands, once the loop has run the
            # callback the handler scheduled.
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            # The second Ctrl-C: raised as a KeyboardInterrupt by the handler itself, on
            # the next bytecode after this line, since the run is already stopping.
            os.kill(os.getpid(), signal.SIGINT)
            raise

    with pytest.raises(KeyboardInterrupt):
        run_suite([_record(0, test_interrupts_twice, "test_interrupts_twice")], loop_watchdog=0.05)

    assert not [thread for thread in threading.enumerate() if thread.name == "voci-loop-watchdog"]


def test_a_stop_landing_during_teardown_leaves_the_tests_own_verdict_alone() -> None:
    """The test had already answered; the run stopping mid-release is the run's doing, not a
    teardown error the test should be blamed for."""

    @voci.fixture()
    async def slow_release() -> AsyncIterator[str]:
        yield "resource"
        await asyncio.sleep(30)

    async def test_passes_then_releases(value: str = voci.Depends(slow_release)) -> None:
        pass

    async def test_fails_a_moment_later() -> None:
        await asyncio.sleep(0.1)
        raise AssertionError("nope")

    records = [
        _record(
            0,
            test_passes_then_releases,
            "test_passes_then_releases",
            plan=plan_for(test_passes_then_releases),
        ),
        _record(1, test_fails_a_moment_later, "test_fails"),
    ]

    started = time.monotonic()
    results = run_suite(records, concurrency=2, maxfail=1)

    assert time.monotonic() - started < 10
    assert [result.outcome for result in results] == [Outcome.PASSED, Outcome.FAILED]


def test_a_stop_landing_during_the_module_scope_flush_keeps_the_real_result() -> None:
    """The last test of a module releases that module's fixtures on its way out, holding a
    result it has already earned -- a cancellation there must not overwrite it."""

    @voci.fixture(scope="module")
    async def slow_module_release() -> AsyncIterator[str]:
        yield "resource"
        await asyncio.sleep(30)

    async def test_fails_then_flushes(value: str = voci.Depends(slow_module_release)) -> None:
        raise AssertionError("nope")

    async def test_fails_a_moment_later() -> None:
        await asyncio.sleep(0.1)
        raise AssertionError("nope")

    records = [
        _record(
            0,
            test_fails_then_flushes,
            "test_fails_then_flushes",
            plan=plan_for(test_fails_then_flushes),
            path=Path("first.py"),
        ),
        _record(1, test_fails_a_moment_later, "test_fails", path=Path("second.py")),
    ]

    started = time.monotonic()
    results = run_suite(records, concurrency=2, maxfail=1)

    assert time.monotonic() - started < 10
    assert [result.outcome for result in results] == [Outcome.FAILED, Outcome.FAILED]


@pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="run_suite only takes SIGINT on the main thread",
)
def test_a_ctrl_c_during_a_maxfail_stop_is_not_the_abort() -> None:
    """The abort is the second Ctrl-C of a run, counted by the handler itself -- not "some
    stop was already under way", which `--maxfail` sets before any Ctrl-C is pressed."""

    async def test_fails_immediately() -> None:
        raise AssertionError("nope")

    async def test_interrupts_when_cancelled() -> None:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            os.kill(os.getpid(), signal.SIGINT)
            raise

    records = [
        _record(0, test_interrupts_when_cancelled, "test_interrupts_when_cancelled"),
        _record(1, test_fails_immediately, "test_fails"),
    ]

    # No KeyboardInterrupt: the run ends by itself and still reports both tests.
    results = run_suite(records, concurrency=2, maxfail=1)

    assert [result.outcome for result in results] == [Outcome.CANCELLED, Outcome.FAILED]
