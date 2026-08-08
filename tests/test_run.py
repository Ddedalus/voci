"""Regression tests for velox._run (spec/05)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
import velox
from velox._collect import CollectionError
from velox._collect import TestRecord as Record  # `Test*` makes pytest try to collect it itself
from velox._fixtures import ResolutionPlan, plan_for
from velox._run import Outcome, exit_code_for, run_suite
from velox._run import TestResult as Result  # same reason

#: A test with no `Depends(...)` at all still needs a plan (`_collect.py` gives every `TestRecord`
#: one, uniformly) — this is the trivial one, shared by every test below that doesn't care about
#: DI at all.
_EMPTY_PLAN = ResolutionPlan(steps=(), root_args=())


def _record(
    index: int,
    func: Callable[..., object],
    qualname: str,
    plan: ResolutionPlan = _EMPTY_PLAN,
) -> Record:
    return Record(
        id=f"mod.py::{qualname}",
        index=index,
        path=Path("mod.py"),
        lineno=1,
        qualname=qualname,
        func=func,
        plan=plan,
    )


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
    cancels itself (the realistic path there in M0, which drives no cancellation of its own)
    must not abort every remaining test and discard every result already collected."""
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


# ------------------------------------------------------------------------------------------
# Setup -> call -> teardown, driven by real `ResolutionPlan`s (spec/04, spec/05 §3). M0 covered
# only a bare zero-argument call; these exercise the DI wiring `run_suite` now drives.
# ------------------------------------------------------------------------------------------


def test_function_scope_fixture_is_injected_with_a_working_value() -> None:
    @velox.fixture()
    def answer() -> int:
        return 42

    async def test_func(x: int = velox.Depends(answer)) -> None:
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

    @velox.fixture(scope="session")
    def counted() -> int:
        builds.append(1)
        return len(builds)

    async def test_a(x: int = velox.Depends(counted)) -> None:
        assert x == 1

    async def test_b(x: int = velox.Depends(counted)) -> None:
        assert x == 1  # same shared instance, not rebuilt for this test

    results = run_suite(
        [
            _record(0, test_a, "test_a", plan=plan_for(test_a)),
            _record(1, test_b, "test_b", plan=plan_for(test_b)),
        ]
    )

    assert [r.outcome for r in results] == [Outcome.PASSED, Outcome.PASSED]
    assert len(builds) == 1


def test_session_scope_fixture_is_torn_down_at_end_of_run() -> None:
    """`store.aclose()` after the loop, per spec/04 §3's "end of the run" — proven by observing
    the generator's teardown side effect only after `run_suite` has returned."""
    torn_down: list[str] = []

    @velox.fixture(scope="session")
    def db():
        yield "db"
        torn_down.append("db")

    async def test_func(x: str = velox.Depends(db)) -> None:
        assert x == "db"

    run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert torn_down == ["db"]


def test_fixture_setup_failure_produces_error_not_failed() -> None:
    @velox.fixture()
    def broken() -> int:
        raise RuntimeError("setup boom")

    async def test_func(x: int = velox.Depends(broken)) -> None:
        raise AssertionError("must never run: setup already failed")

    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert result.outcome is Outcome.ERROR
    assert result.failure is not None
    assert "setup boom" in result.failure


def test_fixture_teardown_failure_after_a_passing_call_produces_error() -> None:
    """spec/05 §3's phase table: a teardown failure surfaces as `error` "even if call passed" —
    the aggregated outcome must not stay `PASSED` just because the call phase itself was clean."""

    @velox.fixture()
    def flaky_teardown():
        yield 1
        raise RuntimeError("teardown boom")

    async def test_func(x: int = velox.Depends(flaky_teardown)) -> None:
        assert x == 1  # the call phase genuinely passes

    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert result.outcome is Outcome.ERROR
    assert result.failure is not None
    assert "teardown boom" in result.failure


def test_call_and_teardown_both_failing_still_reports_error_with_both_tracebacks() -> None:
    @velox.fixture()
    def flaky_teardown():
        yield 1
        raise RuntimeError("teardown boom")

    async def test_func(x: int = velox.Depends(flaky_teardown)) -> None:
        raise AssertionError("call boom")

    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert result.outcome is Outcome.ERROR
    assert result.failure is not None
    assert "call boom" in result.failure
    assert "teardown boom" in result.failure


def _result(outcome: Outcome) -> Result:
    return Result(id="mod.py::t", index=0, outcome=outcome, duration=0.0, failure=None)


def _error(name: str = "mod.py") -> CollectionError:
    return CollectionError(path=Path(name), message="boom")


def test_exit_code_all_passed_is_zero() -> None:
    assert exit_code_for([_result(Outcome.PASSED)], []) == 0


def test_exit_code_with_a_failure_is_one() -> None:
    assert exit_code_for([_result(Outcome.PASSED), _result(Outcome.FAILED)], []) == 1


def test_exit_code_with_an_error_is_one() -> None:
    """spec/05 §4's table: `error` and `failed` both contribute `1`."""
    assert exit_code_for([_result(Outcome.PASSED), _result(Outcome.ERROR)], []) == 1


def test_exit_code_with_a_collection_error_and_no_records_is_one() -> None:
    assert exit_code_for([], [_error()]) == 1


def test_exit_code_nothing_collected_is_five() -> None:
    assert exit_code_for([], []) == 5


def test_exit_code_all_skipped_is_zero_not_five() -> None:
    """spec/05 §4: `skipped` contributes `0` to the exit code, same as `passed` — a suite that
    is entirely skip-marked tests was genuinely collected and did nothing wrong, unlike an empty
    selection (which is what `5` means)."""
    assert exit_code_for([], [], skipped=3) == 0


def test_exit_code_skipped_does_not_mask_a_real_failure() -> None:
    assert exit_code_for([_result(Outcome.FAILED)], [], skipped=1) == 1
