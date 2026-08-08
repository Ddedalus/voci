"""Regression tests for velox._run (spec/05, M0 slice)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from velox._collect import CollectionError
from velox._collect import TestRecord as Record  # `Test*` makes pytest try to collect it itself
from velox._run import Outcome, exit_code_for, run_suite
from velox._run import TestResult as Result  # same reason


def _record(index: int, func: Callable[..., object], qualname: str) -> Record:
    return Record(
        id=f"mod.py::{qualname}",
        index=index,
        path=Path("mod.py"),
        lineno=1,
        qualname=qualname,
        func=func,
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


def _result(outcome: Outcome) -> Result:
    return Result(id="mod.py::t", index=0, outcome=outcome, duration=0.0, failure=None)


def _error(name: str = "mod.py") -> CollectionError:
    return CollectionError(path=Path(name), message="boom")


def test_exit_code_all_passed_is_zero() -> None:
    assert exit_code_for([_result(Outcome.PASSED)], []) == 0


def test_exit_code_with_a_failure_is_one() -> None:
    assert exit_code_for([_result(Outcome.PASSED), _result(Outcome.FAILED)], []) == 1


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
