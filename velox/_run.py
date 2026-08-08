"""Execution: run the collected tests on one shared loop and report pass/fail/error (spec/05).

M1 slice: `asyncio.Runner`, one test at a time in logical (`index`) order — still no semaphore, no
concurrency (that's a later session, spec/05 §1/§9), no per-test `TaskGroup`/`asyncio.timeout`
envelope, no capture routing. What *is* new this slice: each test now goes through real
setup → call → teardown phases (spec/05 §3), driven by `velox._di`, instead of a bare zero-argument
call — `TestRecord.func` is invoked with the kwargs `_di.setup` resolves from `record.plan`, not
with no arguments. Three outcomes now (`PASSED`/`FAILED`/`ERROR`); the rest of the enum
(`skipped`/`xfailed`/`xpassed`/`interrupted`/`timeout`, spec/05 §4) still needs machinery this
session doesn't build (marks-driven skip already happens earlier, in `_collect.py`, and doesn't
produce a `TestResult` at all yet — see `_collect.Skipped`).

This module also owns the exit code mapping, still a subset of spec/02 §4's full table (0/1/5
only — `2`/`3`/`4` need collection-error severity and internal-error detection this milestone
doesn't have yet).
"""

from __future__ import annotations

import asyncio
import enum
import sys
import time
import traceback
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, cast

from velox import _di
from velox._collect import CollectionError, TestRecord

__all__ = ["Outcome", "TestResult", "exit_code_for", "run_suite"]


class Outcome(enum.Enum):
    PASSED = "passed"
    FAILED = "failed"
    #: Setup or teardown raised (spec/05 §3-4) — distinct from `FAILED`, which is reserved for the
    #: call phase itself. See `_run_one`'s docstring for the exact aggregation rule when more than
    #: one phase fails.
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class TestResult:
    id: str
    index: int
    outcome: Outcome
    duration: float
    #: Formatted traceback text. `None` iff `outcome` is `PASSED`. For `ERROR`, this may be the
    #: setup traceback alone, the teardown traceback alone, or both concatenated when the call
    #: phase *also* failed before teardown ran (see `_run_one`).
    failure: str | None


async def _run_one(record: TestRecord, store: _di.ScopeStore) -> TestResult:
    """Run one test's setup → call → teardown, and fold the three phases into one `TestResult`.

    Aggregation rule (spec/05 §3's phase table, made concrete):

    - Setup raises: outcome is `ERROR`, using setup's traceback. The call phase never runs, and
      teardown never runs either — `_di.setup` already releases whatever *it* managed to acquire
      before the failing step, in reverse, as part of raising (see its own docstring); there is
      nothing left for this function to tear down.
    - Setup succeeds: the call phase always runs, and teardown *always* runs afterwards regardless
      of whether the call raised — a test that fails must not leak its fixtures.
    - Teardown raising is what upgrades the outcome to `ERROR`, even over a passing call
      (spec/05 §3: "error, even if call passed"). If the call *also* failed, both tracebacks are
      kept (concatenated, call first) rather than one silently shadowing the other — a broken
      teardown must not hide the assertion that already failed, and vice versa.
    - Otherwise (call succeeded, teardown succeeded): `PASSED`.

    `KeyboardInterrupt`/`SystemExit` are re-raised immediately out of every phase, unchanged from
    the M0 policy `run_suite` documents — they mean "stop the process" regardless of which phase
    they interrupt, not "this phase misbehaved".
    """
    start = time.monotonic()
    setup_failure: str | None = None
    call_failure: str | None = None
    teardown_failure: str | None = None
    kwargs: dict[str, Any] = {}
    keys: tuple[_di.CacheKey, ...] = ()

    try:
        kwargs, keys = await _di.setup(
            record.plan, store, test_id=record.id, module_path=str(record.path)
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        setup_failure = traceback.format_exc()

    if setup_failure is None:
        try:
            # `func` is typed as a plain `Callable[..., object]` (`TestRecord` never wraps it),
            # but only `async def test_*` is ever collected (`_is_own_test_function`), so the
            # call always produces a coroutine at runtime, once argument binding succeeds — a
            # `kwargs` mismatch (which `_di`/`plan_for`'s static checks should already have
            # ruled out) would raise here, synchronously, before `await` ever runs.
            coro = cast("Coroutine[Any, Any, object]", record.func(**kwargs))
            await coro
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            call_failure = traceback.format_exc()

        try:
            await _di.teardown(store, keys)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            teardown_failure = traceback.format_exc()

    duration = time.monotonic() - start

    if setup_failure is not None:
        outcome, failure = Outcome.ERROR, setup_failure
    elif teardown_failure is not None:
        outcome = Outcome.ERROR
        failure = (
            f"{call_failure}\n\n(teardown also failed)\n\n{teardown_failure}"
            if call_failure is not None
            else teardown_failure
        )
    elif call_failure is not None:
        outcome, failure = Outcome.FAILED, call_failure
    else:
        outcome, failure = Outcome.PASSED, None

    return TestResult(
        id=record.id, index=record.index, outcome=outcome, duration=duration, failure=failure
    )


def run_suite(records: list[TestRecord]) -> list[TestResult]:
    """Run every record through setup → call → teardown on one `asyncio.Runner`, in `index` order.

    One `asyncio.Runner` for the whole call — created once, closed once, per spec/05 §1's "one
    long-lived event loop" — and one `_di.ScopeStore` for the whole call too, so `module`/`session`
    scope fixtures are actually shared across the tests that reach them rather than rebuilt per
    test. Each test is awaited to completion (all three phases) before the next is dispatched;
    `_run_one` is where the phases are folded into a single `TestResult` — nothing about that
    folding needs a semaphore or a `TaskGroup` to be correct, so this stays a plain sequential loop
    exactly as M0 left it, just with a fatter per-test body.

    After the loop, `store.aclose()` tears down every `session`-scope fixture still alive
    (spec/04 §3's "end of the run"). This still has to run *inside* the `asyncio.Runner` — closers
    are async — so it happens before the `with` block exits, not after.

    Known, deliberate gap: `aclose()` errors are printed to stderr and swallowed rather than
    folded into any `TestResult` or turned into a nonzero exit code of their own. A session-scope
    teardown failure is not attributable to any *one* test (spec/05 §3 talks about "the run", not
    a test id, for exactly this reason), and there is no reporter yet to hang a dedicated
    "teardown errors" section off (spec/04 §5 wants one; not built this slice). Silently losing
    that error entirely would be worse, so it goes to stderr instead — a real gap, not a fixed one.

    `KeyboardInterrupt`/`SystemExit` propagate immediately, from any phase of any test (see
    `_run_one`) or from `aclose()` itself — those mean "stop the process," not "this run
    misbehaved," and nothing else in this codebase intercepts them either. Partial results are not
    salvaged across that specific re-raise — the process is unwinding regardless, and `cli.main`
    has no return path left to report them through by that point.

    Returns results in the same order as `records`, which is already logical order (spec/03 §1 —
    I2), so no sorting happens here.
    """
    results: list[TestResult] = []
    store = _di.ScopeStore()
    with asyncio.Runner() as runner:
        for record in records:
            results.append(runner.run(_run_one(record, store)))

        try:
            runner.run(store.aclose())
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            # See "Known, deliberate gap" above.
            print("velox: error tearing down session-scope fixtures:", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
    return results


def exit_code_for(
    results: list[TestResult], errors: list[CollectionError], skipped: int = 0
) -> int:
    """The current subset of spec/02 §4's exit code table.

    - `5` — nothing was collected at all (no records, no errors, no skips either — an empty
      selection). `skipped` defaults to `0` so callers that predate `_collect.Skipped` keep
      their existing behavior unchanged.
    - `1` — at least one collection error, or at least one `FAILED`/`ERROR` result (spec/05 §4's
      table gives both the same exit-code contribution).
    - `0` — otherwise (every collected test passed, or was skipped — spec/05 §4's outcome table
      gives `skipped` a `0` exit-code contribution, same as `passed`; a suite that is all skips
      genuinely was collected and did nothing wrong, which is a different case from nothing
      having been found at all).

    The rest of the table (`2` interrupted, `3` internal error, `4` usage error) needs machinery
    this milestone doesn't have yet (Ctrl-C choreography, an internal-vs-suite-fault distinction
    for collection errors) and is out of scope here; `cli.main` still owns `4` for its own
    argument validation.
    """
    if not results and not errors and not skipped:
        return 5
    if errors or any(result.outcome in (Outcome.FAILED, Outcome.ERROR) for result in results):
        return 1
    return 0
