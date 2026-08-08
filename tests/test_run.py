"""Regression tests for velox._run (spec/05, M0 slice)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

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


# Review: vacuous — a `time.monotonic()` delta is non-negative by construction, so this passes
# even if `duration` were hard-coded to 0. Sleep for a known interval and assert the duration
# brackets it, which is what "is timed" is meant to claim.
def test_duration_is_timed() -> None:
    (result,) = run_suite([_record(0, _passes, "test_passes")])
    assert result.duration >= 0.0


# Review: no test covers a test raising a `BaseException` (`asyncio.CancelledError` above all),
# which is the one input that makes `run_suite` abandon the whole suite rather than report a
# failure — see the review note on `_run.run_suite`'s `except Exception`. Also untested:
# `run_suite([])` (empty selection still builds and closes a Runner) and a `func` that returns
# a non-coroutine, which the `cast` in `run_suite` asserts can never happen.


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
