"""Execution: run the collected tests on one shared loop and report pass/fail (spec/05, M0 slice).

M0 scope only: `asyncio.Runner`, one test at a time in logical (`index`) order — no semaphore, no
concurrency (that's M1, spec/05 §1/§9), no fixtures, no per-test `TaskGroup`/`asyncio.timeout`
envelope, no capture routing. Two outcomes only, `PASSED`/`FAILED`; the full enum
(`error`/`skipped`/`xfailed`/`xpassed`/`interrupted`/`timeout`) lands with M1 (spec/05 §4) once
there is setup/teardown and a scheduler to produce the other cases.

This module also owns the M0-shaped exit code mapping (spec/02 §4 subset: 0/1/5 only — the rest
of the table needs collection-error severity and internal-error detection this milestone doesn't
have yet).
"""

from __future__ import annotations

import asyncio
import enum
import time
import traceback
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, cast

from velox._collect import CollectionError, TestRecord

__all__ = ["Outcome", "TestResult", "exit_code_for", "run_suite"]


class Outcome(enum.Enum):
    PASSED = "passed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TestResult:
    id: str
    index: int
    outcome: Outcome
    duration: float
    #: Formatted traceback text. `None` unless `outcome` is `FAILED`.
    failure: str | None


def run_suite(records: list[TestRecord]) -> list[TestResult]:
    """Run every record's `func` as a task on one `asyncio.Runner`, in logical (`index`) order.

    One `asyncio.Runner` for the whole call — created once, closed once, per spec/05 §1's "one
    long-lived event loop", even though M0 dispatches sequentially rather than concurrently.
    Each test is awaited to completion before the next is dispatched; an exception raised by the
    test function is caught at this boundary and turned into a `FAILED` `TestResult` carrying a
    formatted traceback — nothing escapes to the caller (spec/05 §3's "call" phase, minus setup/
    teardown, which don't exist yet in M0).

    Returns results in the same order as `records`, which is already logical order (spec/03 §1 —
    I2), so no sorting happens here.
    """
    results: list[TestResult] = []
    with asyncio.Runner() as runner:
        for record in records:
            start = time.monotonic()
            failure: str | None = None
            try:
                # `func` is typed as a plain `Callable[..., object]` (`TestRecord` never wraps
                # it), but only `async def test_*` is ever collected (`_is_own_test_function`),
                # so the call always produces a coroutine at runtime.
                # Review: `record.func()` is called with no arguments, so a test written
                # against the already-public DI surface (`db: DB = Depends(get_db)`) silently
                # runs with the raw `Depends` marker as its value instead of erroring. Combined
                # with marks being ignored at collection, M0 can report PASSED for a test that
                # was never really executed as written — squarely an I8 "silent pass".
                coro = cast("Coroutine[Any, Any, object]", record.func())
                runner.run(coro)
            # Review: `except Exception` lets a `BaseException` from the test abort the entire
            # suite. `asyncio.CancelledError` is the realistic one — any test whose inner task
            # gets cancelled re-raises it — and `runner.run` also surfaces `CancelledError`
            # when the wrapping task is cancelled. Verified: a test raising `CancelledError`
            # escapes `run_suite`, so every already-completed result is discarded, the
            # remaining tests never run, and `main` returns no exit code at all. At minimum
            # catch `BaseException` and re-raise `KeyboardInterrupt`/`SystemExit` after
            # recording the partial results (`interrupted` is exactly spec/05 §4's case for
            # this, even if the full enum is M1).
            except Exception:
                failure = traceback.format_exc()
            duration = time.monotonic() - start

            results.append(
                TestResult(
                    id=record.id,
                    index=record.index,
                    outcome=Outcome.FAILED if failure is not None else Outcome.PASSED,
                    duration=duration,
                    failure=failure,
                )
            )
    return results


def exit_code_for(results: list[TestResult], errors: list[CollectionError]) -> int:
    """The M0 subset of spec/02 §4's exit code table.

    - `5` — nothing was collected at all (no records, no errors either — an empty selection).
    - `1` — at least one collection error, or at least one `FAILED` result.
    - `0` — otherwise (every collected test passed).

    The rest of the table (`2` interrupted, `3` internal error, `4` usage error) needs machinery
    M0 doesn't have yet (Ctrl-C choreography, an internal-vs-suite-fault distinction for
    collection errors) and is out of scope here; `cli.main` still owns `4` for its own argument
    validation.
    """
    if not results and not errors:
        return 5
    if errors or any(result.outcome is Outcome.FAILED for result in results):
        return 1
    return 0
