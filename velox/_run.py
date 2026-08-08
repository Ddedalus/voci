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


async def _run_one(
    record: TestRecord, store: _di.ScopeStore
) -> tuple[TestResult, tuple[_di.CacheKey, ...]]:
    """Run one test's setup → call → teardown, and fold the three phases into one `TestResult`.

    Also returns this test's own `module`-scope cache keys, still held open (not yet released).
    `run_suite` accumulates these across every test that shares one module and releases them
    together once the last one finishes — see its docstring for why that's what makes
    `scope="module"` fixtures actually shared rather than rebuilt by the very next test.
    `function`/`call`/`session`-scope keys are released here, per test, same as always; a
    `session` key's `release` is always a harmless refcount-only decrement (real teardown is
    `aclose`'s job), so leaving it in the "release now" bucket changes nothing observable.

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
      teardown must not hide the assertion that already failed, and vice versa. The *outcome*,
      though, is `ERROR` either way, not `FAILED`-with-a-teardown-note: spec/05 §3 states this as
      an unconditional property of the teardown phase ("error, even if call passed"), not as a
      property that only applies when call passed, and a single boolean `outcome` field has no
      third state to spend on "failed, and also errored" without inventing one. The sharper
      distinction a future JUnit `<failure>` vs `<error>` element or a `--lf`-style rerun list
      would want is exactly what spec/04 §5's still-unbuilt "teardown errors" section is for —
      not a reason to grow `TestResult` a second outcome-shaped field now.
    - Otherwise (call succeeded, teardown succeeded): `PASSED`.

    `KeyboardInterrupt`/`SystemExit` are re-raised immediately out of every phase, unchanged from
    the M0 policy `run_suite` documents — they mean "stop the process" regardless of which phase
    they interrupt, not "this phase misbehaved". This only actually holds because `_di._release_all`
    (which both `_di.setup`'s cleanup and `_di.teardown` funnel through) now re-raises those two
    immediately itself rather than folding them into its `BaseExceptionGroup` — the `except
    (KeyboardInterrupt, SystemExit): raise` guards below exist for defense in depth and for the
    non-fixture-related raise sites (`_di.setup`'s own step loop, the test call itself).
    """
    start = time.monotonic()
    setup_failure: str | None = None
    call_failure: str | None = None
    teardown_failure: str | None = None
    kwargs: dict[str, Any] = {}
    keys: tuple[_di.CacheKey, ...] = ()
    module_keys: tuple[_di.CacheKey, ...] = ()

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

        # `key[0]` is the scope tag every shape `_di.key_for` returns starts with — reading it
        # back here is cheaper and more honest than threading a parallel scope list through
        # `_di.setup`'s return value just for this one caller.
        module_keys = tuple(key for key in keys if key[0] == "module")
        other_keys = tuple(key for key in keys if key[0] != "module")
        try:
            await _di.teardown(store, other_keys)
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

    result = TestResult(
        id=record.id, index=record.index, outcome=outcome, duration=duration, failure=failure
    )
    return result, module_keys


def run_suite(records: list[TestRecord]) -> list[TestResult]:
    """Run every record through setup → call → teardown on one `asyncio.Runner`, in `index` order.

    One `asyncio.Runner` for the whole call — created once, closed once, per spec/05 §1's "one
    long-lived event loop" — and one `_di.ScopeStore` for the whole call too, so `module`/`session`
    scope fixtures are actually shared across the tests that reach them rather than rebuilt per
    test. Each test is awaited to completion (all three phases) before the next is dispatched;
    `_run_one` is where the phases are folded into a single `TestResult` — nothing about that
    folding needs a semaphore or a `TaskGroup` to be correct, so this stays a plain sequential loop
    exactly as M0 left it, just with a fatter per-test body.

    `session` scope is shared for free — its cache key doesn't mention the test at all, so every
    test's `acquire` on the same fixture just increments one shared refcount. `module` scope needs
    active help under this sequential loop: nothing is ever actually "in flight" at once (spec/04
    §3's module-teardown rule, "refcount → 0: the last in-flight test from that module finishes",
    is written for concurrent dispatch), so if each test released its own `module`-scope keys the
    instant its own teardown ran, the *first* test to touch a module fixture would tear it down
    immediately and the next test in the same module would rebuild it from scratch — module scope
    degenerating into function scope with a different cache key. `_run_one` instead holds
    `module`-scope keys open (returning them instead of releasing them), and this loop accumulates
    them across every test sharing one `path` and releases the lot only once it reaches the last
    test of that module — `records` is already grouped by file (`_collect.collect` appends one
    file's records contiguously before starting the next), so "last test of this module" is just
    "the next record's `path` differs, or there is no next record". Two *different* modules'
    fixtures are never accumulated together, since the flush happens at every boundary.

    After the loop, `store.aclose()` tears down every `session`-scope fixture still alive
    (spec/04 §3's "end of the run"). This runs in a `finally` — best-effort, not the shielded,
    time-boxed version spec/04 §5/§6 eventually wants (that needs the scheduler's cancellation
    machinery, not built this slice) — so it still happens even when the loop above is unwinding
    via `KeyboardInterrupt`/`SystemExit`: without a `finally` here, Ctrl-C mid-suite would leak
    every session-scope fixture, and that is not a hypothetical edge case — the moment a run is
    most likely to be interrupted is while it's running, which is the entire lifetime of this loop.

    Known, deliberate gap: both `aclose()` errors and module-boundary teardown errors are printed
    to stderr and swallowed rather than folded into any `TestResult` or turned into a nonzero exit
    code of their own. Neither is attributable to any *one* test (spec/05 §3 talks about "the
    run", not a test id, for exactly this reason), and there is no reporter yet to hang a dedicated
    "teardown errors" section off (spec/04 §5 wants one; not built this slice). Silently losing
    those errors entirely would be worse, so they go to stderr instead — a real gap, not a fixed
    one; in particular this also means an internal velox bug surfacing during teardown (as opposed
    to a user fixture's own exception) prints to the same stderr blob with nothing to tell them
    apart, where spec/02 §4's exit code `3` would eventually want to.

    `KeyboardInterrupt`/`SystemExit` propagate immediately, from any phase of any test (see
    `_run_one`) or from `aclose()`/a module-boundary flush themselves — those mean "stop the
    process," not "this run misbehaved," and nothing else in this codebase intercepts them either.
    A *fresh* interrupt raised while tearing down after an earlier, already-propagating one is what
    ultimately escapes (Python's own `finally`-replaces-the-original-exception rule), the same
    "the newest stop-now wins" precedence used everywhere else in this module. Partial results are
    not salvaged across that specific re-raise — the process is unwinding regardless, and
    `cli.main` has no return path left to report them through by that point.

    Returns results in the same order as `records`, which is already logical order (spec/03 §1 —
    I2), so no sorting happens here.
    """
    results: list[TestResult] = []
    store = _di.ScopeStore()
    with asyncio.Runner() as runner:
        try:
            pending_module_keys: list[_di.CacheKey] = []
            for index, record in enumerate(records):
                result, module_keys = runner.run(_run_one(record, store))
                results.append(result)
                pending_module_keys.extend(module_keys)

                is_last_of_module = (
                    index + 1 == len(records) or records[index + 1].path != record.path
                )
                if is_last_of_module and pending_module_keys:
                    _teardown_best_effort(
                        runner,
                        _di.teardown(store, pending_module_keys),
                        what="module-scope fixtures",
                    )
                    pending_module_keys = []
        finally:
            _teardown_best_effort(runner, store.aclose(), what="session-scope fixtures")
    return results


def _teardown_best_effort(
    runner: asyncio.Runner, coro: Coroutine[Any, Any, None], what: str
) -> None:
    """Run one end-of-scope teardown `coro` to completion, swallowing everything except
    `KeyboardInterrupt`/`SystemExit` — see `run_suite`'s "Known, deliberate gap" for why this
    prints to stderr instead of failing the run or attributing the error to any one `TestResult`.
    """
    try:
        runner.run(coro)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        print(f"velox: error tearing down {what}:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)


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
