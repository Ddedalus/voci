"""Runs collected tests concurrently and reports each one's pass/fail/error/timeout/xfail/xpass
outcome.

Tests are dispatched as concurrent `asyncio` tasks, admitted by a shared `AdmissionGate` that
bounds how many run at once and, within that bound, withholds a test whose `exclusive=` fixtures
claim a resource another running test already holds, or that is running while a `@voci.solo`
test holds the whole gate to itself. A test voci found `unittest.mock` patching on at collection
takes the gate the same way a `@voci.solo` one does, and for the duration of the run `_mocking`'s
guard is installed, so a patch from any *other* test is refused rather than let loose on
everything running alongside it. Each admitted test gets a real setup -> call -> teardown
envelope, plus an optional per-test `asyncio.timeout` budget -- the suite-wide `--timeout`, or a
`@voci.timeout(...)` mark overriding it for that test alone. An `async def` test's call phase is
awaited directly; a sync `def` one runs on the context-propagating executor
(`_capture.ContextPropagatingExecutor`) instead, so a blocking call inside it holds only
its own concurrency slot rather than the shared event loop. A `@voci.isolated`-marked test is
admitted through the same gate but dispatched to a fresh subprocess instead (`isolated.py`),
re-collected there from its own source file and run alone on that process's own loop. Results
are collected back into logical (collection) order regardless of the order tests actually
finish in, so a run's output is reproducible independent of scheduling. This module also
derives the process exit code from the collected results and collection errors.

Stopping a run early is one mechanism with two triggers: `--maxfail` reaching its threshold and
a Ctrl-C both go through `StopController`, which drops every test that hasn't started and
cancels every test that has, giving each cancelled test a time-boxed window to tear its fixtures
down. What a test does that no exception ever reports -- blocking the loop, returning a value,
forgetting an `await`, or sticking in a worker thread past the end of the run -- is `safety.py`'s
half, wired up here.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import enum
import functools
import inspect
import logging
import math
import os
import signal
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO, cast, final

from voci import _mocking, _warnings
from voci._builtins import capture as _capture
from voci._collection.collect import CollectionError, TestRecord
from voci._di import runtime as _di
from voci._di.fixtures import exclusive_tokens_of
from voci._marks import Marks, XFail
from voci._outcomes import Skipped
from voci._run import isolated as _isolated
from voci._run import safety as _safety

__all__ = [
    "FAILING_OUTCOMES",
    "AdmissionGate",
    "IsolatedConfig",
    "Outcome",
    "StopController",
    "TestResult",
    "exit_code_for",
    "run_suite",
    "solo_for_patching",
]

#: Re-exported so a caller only needs `from voci._run import run` to build the one argument
#: `run_suite` needs for `@voci.isolated` -- see `isolated.py`'s own docstring for why that
#: module can't import this one back.
IsolatedConfig = _isolated.IsolatedConfig

#: Much larger than a typical core count: most suites are bound by a downstream
#: service's latency, not by CPU.
DEFAULT_CONCURRENCY = 16

#: How long the fixture teardown of a test the run stopped waiting for -- cancelled, or timed out
#: -- gets before the run stops waiting for that too. The budget answers "how long is it worth
#: waiting for a clean release of whatever this test held" -- generous enough for a container to
#: stop or a connection pool to drain, short enough that Ctrl-C still feels like Ctrl-C.
DEFAULT_TEARDOWN_GRACE = 5.0


class Outcome(enum.Enum):
    """A test's final disposition."""

    PASSED = "passed"
    FAILED = "failed"
    #: Setup or teardown raised, as opposed to the test's own call phase.
    ERROR = "error"
    #: `voci.Skipped` was raised during setup or the call phase -- a runtime skip, as opposed
    #: to a `skip`/`skipif` mark, which never reaches `_run_one` at all (collection keeps a
    #: skip-marked test out of the run entirely). Not a failing outcome: exactly like a
    #: collection-time skip, it says nothing about whether the code under test works.
    SKIPPED = "skipped"
    #: The setup+call envelope exceeded its `--timeout` budget. Its own outcome rather
    #: than a flavor of FAILED/ERROR, since the fix (raise the budget, or find the
    #: blocking call) is different from either.
    TIMEOUT = "timeout"
    #: The call phase raised the failure a `@voci.xfail(...)` mark expected. Not a
    #: failing outcome regardless of `strict` -- `strict` only governs XPASSED.
    XFAILED = "xfailed"
    #: The call phase passed despite a `@voci.xfail(...)` mark. Only reached when
    #: `strict` is unset; a strict mark's unexpected pass reports FAILED instead, since
    #: at that point there is no "expected" disposition left to report.
    XPASSED = "xpassed"
    #: The run stopped -- `--maxfail`, or a Ctrl-C -- while this test was still in flight,
    #: so it was cancelled where it stood. Not a failing outcome: the test never got to say
    #: anything about the code under test, which is different from saying it works.
    CANCELLED = "cancelled"


#: Outcomes that count as failing, both for the process exit code and for which
#: results keep their captured stdout/stderr/log records around. XFAILED and XPASSED
#: are deliberately excluded: both mean the test behaved exactly as its `xfail` mark
#: said it would. So is CANCELLED, which is the run's own doing rather than anything
#: the test did -- the run it interrupted is what carries the non-zero exit code.
FAILING_OUTCOMES = (Outcome.FAILED, Outcome.ERROR, Outcome.TIMEOUT)


@dataclass(frozen=True, slots=True)
class TestResult:
    """One test's outcome: id, logical index, disposition, duration, and failure
    detail. `captured_stdout`/`captured_stderr`/`log_records` are populated only for
    failing outcomes; see `run_suite`."""

    id: str
    index: int
    outcome: Outcome
    duration: float
    #: Formatted traceback text, or a synthesized message for TIMEOUT/SKIPPED (for the latter,
    #: `str(the raised voci.Skipped)`, not a traceback). `None` iff `outcome` is PASSED. For
    #: ERROR this may be the setup traceback, the teardown traceback, or both concatenated if
    #: the call phase also failed.
    failure: str | None
    #: A short one-line "ExceptionType: message" summary of whichever exception
    #: decided `outcome`, read directly off the exception object rather than parsed
    #: back out of `failure`'s traceback text. `None` iff `outcome` is PASSED.
    failure_summary: str | None
    captured_stdout: str = ""
    captured_stderr: str = ""
    log_records: tuple[logging.LogRecord, ...] = ()
    #: What this test warned about, aggregated by warning and location. Populated for every
    #: outcome, not only failing ones: a passing test's deprecation warning is the whole point
    #: of the end-of-run summary.
    warnings: tuple[_warnings.RecordedWarning, ...] = ()


def _summarize_exception(exc: BaseException) -> str:
    """`f"{ExceptionType}: {first line of str(exc)}"`, read straight off the exception
    object rather than parsed back out of its rendered traceback."""
    message = str(exc)
    first_line = message.splitlines()[0] if message else ""
    return f"{type(exc).__name__}: {first_line}" if first_line else type(exc).__name__


def _resolve_call_outcome(
    xfail: XFail | None,
    *,
    call_exc: BaseException | None,
    call_failure: str | None,
    call_summary: str | None,
) -> tuple[Outcome, str | None, str | None]:
    """The call phase's own FAILED/PASSED disposition, reread through `xfail` when
    the record carries one. A `raises=` mismatch still reports FAILED -- the wrong
    exception is not the failure the mark said to expect."""
    if call_failure is not None:
        if xfail is None or (
            xfail.raises is not None
            and not (call_exc is not None and isinstance(call_exc, xfail.raises))
        ):
            return Outcome.FAILED, call_failure, call_summary
        return Outcome.XFAILED, f"{call_failure}\n(expected failure: {xfail.reason})", call_summary
    if xfail is None:
        return Outcome.PASSED, None, None
    if xfail.strict:
        return (
            Outcome.FAILED,
            f"test passed, but was marked @voci.xfail(reason={xfail.reason!r}, strict=True)",
            f"XPASS(strict): expected failure but the test passed ({xfail.reason})",
        )
    return (
        Outcome.XPASSED,
        f"expected failure ({xfail.reason}), but the test passed",
        f"XPASS: {xfail.reason}",
    )


@dataclass(slots=True)
class _SetupResult:
    """One test's setup phase, folded to a result rather than left as an exception -- see
    `_run_setup`."""

    kwargs: dict[str, Any] = dataclasses.field(default_factory=dict)
    keys: tuple[_di.CacheKey, ...] = ()
    done: bool = False
    failure: str | None = None
    summary: str | None = None
    skip_reason: str | None = None


@dataclass(slots=True)
class _CallResult:
    """One test's call phase, folded to a result rather than left as an exception -- see
    `_run_call`."""

    failure: str | None = None
    summary: str | None = None
    exc: BaseException | None = None
    skip_reason: str | None = None
    misused: bool = False


async def _run_setup(
    record: TestRecord, store: _di.ScopeStore, partial_module_keys: list[_di.CacheKey]
) -> _SetupResult:
    """Acquire `record`'s fixtures, folding a setup failure or an imperative skip into the
    returned result rather than raising. Only a stop-worthy interrupt -- Ctrl-C, or the
    `asyncio.CancelledError`/`TimeoutError` `_run_one`'s own deadline handles -- propagates."""
    try:
        kwargs, keys = await _di.setup(
            record.plan,
            store,
            test_id=record.id,
            module_path=str(record.path),
            partial_module_keys=partial_module_keys,
        )
        return _SetupResult(kwargs=kwargs, keys=keys, done=True)
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except Skipped as exc:
        # Caught ahead of the generic handler below: a skip raised while acquiring a
        # fixture is not a setup failure, and nothing acquired after it exists to run a
        # call phase against.
        return _SetupResult(skip_reason=str(exc))
    except BaseException as exc:
        return _SetupResult(failure=traceback.format_exc(), summary=_summarize_exception(exc))


async def _run_call(record: TestRecord, call_kwargs: dict[str, Any]) -> _CallResult:
    """Run `record.func`'s call phase alone, folding its failure or an imperative skip into
    the returned result -- same not-raising contract as `_run_setup`."""
    try:
        # Open across the call phase alone: CPython reports a coroutine nobody
        # awaited as soon as its last reference drops, which for one a test
        # created is somewhere inside this block -- at the statement that
        # dropped it, or at the test's own frame dying as it returns.
        with _safety.watch_unawaited() as unawaited:
            if inspect.iscoroutinefunction(record.func):
                coro = cast("Coroutine[Any, Any, object]", record.func(**call_kwargs))
                returned = await coro
            else:
                # A sync `def test_*`: dispatched to the loop's default executor
                # (`_capture.ContextPropagatingExecutor`, installed by `run_suite`)
                # rather than called inline, so a blocking call in its body stalls
                # only this test's own concurrency slot instead of the shared loop
                # every other concurrently-dispatched test also runs on. Wrapped so
                # that a body which never returns can still be named afterwards
                # (`safety.stuck_calls`) rather than silently holding the process
                # open.
                returned = await asyncio.get_running_loop().run_in_executor(
                    None,
                    _safety.track_sync_call(
                        record.id, functools.partial(record.func, **call_kwargs)
                    ),
                )
        # Read after the block, not inside it: a coroutine held in a local until
        # the test returns is reported as that frame is torn down, which is the
        # last thing to happen inside it.
        misuse = _safety.call_misuse(returned, unawaited)
        if misuse is not None:
            return _CallResult(failure=misuse.detail, summary=misuse.summary, misused=True)
        return _CallResult()
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except Skipped as exc:
        # Caught ahead of the generic handler below, and ahead of `xfail`: a skip
        # reached mid-call is reported as skipped regardless of what an `xfail` mark
        # on this test expected -- pytest's own imperative skip takes the same
        # priority over it.
        return _CallResult(skip_reason=str(exc))
    except BaseException as exc:
        return _CallResult(
            failure=traceback.format_exc(), summary=_summarize_exception(exc), exc=exc
        )


async def _run_teardown(
    store: _di.ScopeStore,
    setup: _SetupResult,
    *,
    cancelled: bool,
    timed_out: bool,
    stop: StopController | None,
    record_id: str,
    partial_module_keys: list[_di.CacheKey],
) -> tuple[tuple[_di.CacheKey, ...], str | None, str | None]:
    """Release `setup`'s fixtures -- or, if setup never finished, whatever module-scope keys
    it partially acquired (`_di.setup` leaves those unreleased on failure). Returns the
    module-scope keys still held open for `run_suite`'s own accounting, plus a teardown
    failure/summary pair folded to a note rather than raised, same contract as `_run_setup`/
    `_run_call`.
    """
    if not setup.done:
        # Gated on setup.done, not setup.failure is None: this also covers "the timeout
        # fired (or setup was cancelled) before setup returned", which leaves setup.failure
        # unset too.
        return tuple(partial_module_keys), None, None

    # key[0] is the scope tag every key _di.key_for returns starts with.
    module_keys = tuple(key for key in setup.keys if key[0] == "module")
    other_keys = tuple(key for key in setup.keys if key[0] != "module")
    # Time-boxed for a cancelled test, where the run is already ending and a fixture
    # that waits on something that will never come would hold it open -- and for a
    # timed-out one, where the fixture the deadline just fired on is the first suspect:
    # an unbounded teardown there would hang the whole run (holding its admission slot)
    # in exactly the case `--timeout` exists to bound. An ordinary test's teardown keeps
    # the budget it has always had: none.
    grace = stop.teardown_grace if (cancelled or timed_out) and stop is not None else None
    try:
        async with asyncio.timeout(grace):
            await _di.teardown(store, other_keys)
    except TimeoutError:
        # Only reachable when `grace` is not None, i.e. on the cancellation or timeout
        # path, where the result is CANCELLED/TIMEOUT and this becomes a note on it.
        # Said out loud too: a leaked container or connection is worth knowing about
        # while the run is still on screen, not only in the result the reporter drops.
        why = "the run being stopped" if cancelled else "this test's timeout"
        teardown_failure = (
            f"teardown did not finish within {grace}s of {why} -- the "
            f"fixture may not have been fully torn down"
        )
        teardown_summary = f"teardown exceeded its {grace}s grace budget"
        if stop is not None:
            stop.note(f"voci: {record_id}: {teardown_failure}")
        return module_keys, teardown_failure, teardown_summary
    except (KeyboardInterrupt, SystemExit):
        raise
    except asyncio.CancelledError:
        # Not re-raised (unlike the setup/call guards in _run_setup/_run_call): teardown
        # runs outside the `async with deadline:` block, so there is no
        # asyncio.timeout __aexit__ left to hand a re-raise to, and no outer
        # handler in _run_one left to catch it -- re-raising here would let a
        # bare CancelledError escape _run_one, breaking run_suite's invariant that
        # every dispatched task fills its own results slot.
        if stop is not None and stop.claim():
            # The run stopped while this test was releasing its fixtures, which is
            # nothing the test did: its call phase already reached a verdict and that
            # verdict stands. What is left to say is that the release may be half
            # done, and the place to say it is the run, not the result.
            stop.note(
                f"voci: {record_id}: teardown was cancelled when the run stopped -- "
                f"the fixture may not have been fully torn down"
            )
            return module_keys, None, None
        # A collateral cancellation instead: recorded as an ERROR, tagged
        # distinctly since it too may have left the fixture partially torn down.
        teardown_failure = (
            "teardown was cancelled (most likely collateral from a sibling's "
            "KeyboardInterrupt/SystemExit) -- the fixture may not have been fully torn "
            f"down:\n\n{traceback.format_exc()}"
        )
        teardown_summary = (
            "CancelledError: teardown cancelled (collateral from a sibling interrupt)"
        )
        return module_keys, teardown_failure, teardown_summary
    except BaseException as exc:
        return module_keys, traceback.format_exc(), _summarize_exception(exc)
    return module_keys, None, None


def _resolve_outcome(
    *,
    timed_out: bool,
    cancelled: bool,
    stop: StopController | None,
    timeout: float | None,
    setup: _SetupResult,
    call: _CallResult,
    teardown_failure: str | None,
    teardown_summary: str | None,
    skip_reason: str | None,
    xfail: XFail | None,
) -> tuple[Outcome, str | None, str | None]:
    """This test's final disposition, once every phase has had its say. A timed-out envelope
    is reported TIMEOUT ahead of any other phase's failure; a cancellation `stop` delivered is
    CANCELLED; setup raising is ERROR; teardown raising is ERROR even over a passing call
    (both tracebacks are kept if the call also failed); an imperative skip -- ahead of that
    phase's own ERROR/FAILED, but behind a later teardown's ERROR -- is SKIPPED; otherwise
    FAILED or PASSED (call raised or not), reread as XFAILED/XPASSED against `xfail`. See
    `_run_one`'s own docstring for why `xfail` only ever reclassifies those last two.
    """
    if timed_out:
        outcome = Outcome.TIMEOUT
        # Phrased without naming --timeout specifically: `timeout` here may be the
        # suite-wide budget or a per-test `@voci.timeout(...)` override, and this
        # message doesn't know which.
        failure = f"test exceeded its {timeout}s timeout budget"
        summary = failure
        # Whichever of these is set (never both) is the exception that actually
        # surfaced while the deadline was expiring, kept for context.
        extra = call.failure if call.failure is not None else setup.failure
        if extra is not None:
            failure = f"{failure}\n\n{extra}"
        # A teardown that overran the grace above is part of what this test left behind, and
        # TIMEOUT is the only outcome that reports it -- the branches below all read
        # `teardown_failure` for themselves.
        if teardown_failure is not None:
            failure = f"{failure}\n\n{teardown_failure}"
        return outcome, failure, summary
    if cancelled:
        # Ahead of every phase's own failure, and of `xfail`: whatever this test was
        # about to report, it didn't get to finish saying it.
        outcome = Outcome.CANCELLED
        reason = stop.reason_text if stop is not None else "the run stopped"
        failure = f"cancelled: {reason}"
        summary = failure
        if teardown_failure is not None:
            failure = f"{failure}\n\n{teardown_failure}"
        return outcome, failure, summary
    if setup.failure is not None:
        return Outcome.ERROR, setup.failure, setup.summary
    if teardown_failure is not None:
        failure = (
            f"{call.failure}\n\n(teardown also failed)\n\n{teardown_failure}"
            if call.failure is not None
            else teardown_failure
        )
        # Call-first, mirroring failure's own text ordering just above.
        summary = call.summary if call.failure is not None else teardown_summary
        return Outcome.ERROR, failure, summary
    if skip_reason is not None:
        # After setup/teardown failure, ahead of `_resolve_call_outcome`: a skip that reached
        # this far had a clean setup and (if it got that far) a clean teardown, and it is not
        # reread through `xfail` the way a call failure or pass is -- there is no "expected
        # failure" question left to ask about a test that never got to fail or pass.
        return Outcome.SKIPPED, skip_reason, f"SKIPPED: {skip_reason}"
    return _resolve_call_outcome(
        xfail, call_exc=call.exc, call_failure=call.failure, call_summary=call.summary
    )


async def _run_one(
    record: TestRecord,
    store: _di.ScopeStore,
    *,
    timeout: float | None,
    stop: StopController | None = None,
) -> tuple[TestResult, tuple[_di.CacheKey, ...]]:
    """Run one test's setup -> call -> teardown and fold the three phases into one
    `TestResult`.

    Also returns this test's own `module`-scope cache keys, still held open rather than
    released -- `run_suite` accumulates these across every test sharing a module and
    releases them together only once that module's last test has finished, which is
    what keeps `scope="module"` fixtures shared under concurrent dispatch.

    Teardown always runs once setup has acquired anything, whatever the call phase did.
    See `_resolve_outcome` for how the three phases' results resolve to one outcome.

    `stop`, when the run has one, is both how a cancellation is recognized as the run's
    own doing rather than a sibling's collateral damage and where the teardown of a
    cancelled -- or timed-out -- test gets its time box, past which the test reports
    CANCELLED/TIMEOUT with a note that its fixtures may not have been fully released.
    """
    start = time.monotonic()
    setup = _SetupResult()
    call = _CallResult()
    timed_out = False
    cancelled = False
    partial_module_keys: list[_di.CacheKey] = []

    # asyncio.timeout(None) is a documented no-op, so entering it is unconditional.
    # Kept as a named local so .expired() can be checked after the block, below.
    deadline = asyncio.timeout(timeout)
    try:
        async with deadline:
            setup = await _run_setup(record, store, partial_module_keys)
            if setup.failure is None and setup.skip_reason is None:
                # `record.params` first, `setup.kwargs` (this case's DI plan) second:
                # collection already rejects any name both `@voci.parametrize` and
                # `Depends(...)` claim (`_di._check_missing_injections`), so the two never
                # actually overlap -- this ordering is only a tie-breaker that can't be
                # exercised.
                call_kwargs = {**(record.params or {}), **setup.kwargs}
                call = await _run_call(record, call_kwargs)
    except TimeoutError:
        # Only reachable when `timeout` is not None. By the time this is caught,
        # `asyncio.timeout.__aexit__` has already converted its own cancellation into
        # TimeoutError; a CancelledError from anywhere else was re-raised, unconverted,
        # by the guards above and is caught separately below.
        timed_out = True
    except asyncio.CancelledError as exc:
        # Three different sources land here: the run itself stopping early (`--maxfail`, a
        # Ctrl-C), this test's own timeout deadline (if its CancelledError wasn't turned
        # into TimeoutError above -- see the cross-check below), or a collateral
        # cancellation from a sibling task. Only the first is `stop`'s to claim, and
        # claiming it also un-cancels this task so the teardown below can still await;
        # for the other two `setup.done` decides whether this reads as a setup or a call
        # failure.
        if stop is not None and stop.claim():
            cancelled = True
        else:
            cancelled_failure = traceback.format_exc()
            cancelled_summary = _summarize_exception(exc)
            if setup.done:
                call.failure = cancelled_failure
                call.summary = cancelled_summary
            else:
                setup.failure = cancelled_failure
                setup.summary = cancelled_summary

    if not timed_out and deadline.expired():
        # Cross-check: catches a timeout whose injected CancelledError never reached
        # either except clause above because the test's own code intercepted it and
        # substituted (or swallowed) a different exception. Any setup/call failure
        # captured on the way here is folded into the TIMEOUT text below rather than
        # discarded.
        timed_out = True

    module_keys, teardown_failure, teardown_summary = await _run_teardown(
        store,
        setup,
        cancelled=cancelled,
        timed_out=timed_out,
        stop=stop,
        record_id=record.id,
        partial_module_keys=partial_module_keys,
    )

    duration = time.monotonic() - start
    outcome, failure, summary = _resolve_outcome(
        timed_out=timed_out,
        cancelled=cancelled,
        stop=stop,
        timeout=timeout,
        setup=setup,
        call=call,
        teardown_failure=teardown_failure,
        teardown_summary=teardown_summary,
        skip_reason=setup.skip_reason if setup.skip_reason is not None else call.skip_reason,
        xfail=None if call.misused else record.marks.xfail,
    )

    result = TestResult(
        id=record.id,
        index=record.index,
        outcome=outcome,
        duration=duration,
        failure=failure,
        failure_summary=summary,
    )
    return result, module_keys


def solo_for_patching(record: TestRecord) -> bool:
    """Whether `record` runs alone because voci found `unittest.mock` patching on it.

    A `@voci.isolated` test is excluded: it patches its own subprocess, where there is nothing
    else to disturb, so it costs the suite no concurrency.
    """
    return bool(record.patches) and not record.marks.isolated


@final
@dataclass(slots=True)
class _Waiter:
    """One test queued on `AdmissionGate`, with the future `release` completes to admit it."""

    tokens: frozenset[object]
    solo: bool
    future: asyncio.Future[None]


@final
class AdmissionGate:
    """The single point every dispatched test is admitted through: bounds how many run at once,
    and, within that bound, coordinates `exclusive=` fixtures and `@voci.solo`.

    Concurrency, `exclusive=`, and `solo` are one decision, not three layered ones -- a test
    piled up waiting on a contended token or a solo lock holds no concurrency slot while it
    waits, so it can never starve an unrelated test out of one. A test with no exclusive tokens
    and no `solo` mark is admitted as soon as a slot is free and no solo test is running. A test
    with exclusive tokens additionally needs none of them to overlap what's already running --
    acquired for its whole token set in one step, never one token at a time, so two tests can
    never deadlock each holding a token the other needs. A `solo` test is admitted only once
    nothing else is running, and blocks every other admission until it releases. Waiters have no
    fairness guarantee (`ROADMAP.md`).

    Releasing hands the freed slot straight to a waiter rather than waking the queue to race for
    it: `run_suite` creates every test's task up front, so the queue is the whole suite, and
    re-testing every waiter's admission predicate on every release made admission quadratic in
    the number of tests (a 16k-test suite spent ~23s of wall clock doing nothing else). `_wake`
    scans from the head only as far as the free capacity allows, which for the ordinary case --
    a waiter with no tokens at the head of the queue -- is one step.
    """

    def __init__(self, concurrency: int) -> None:
        self._concurrency = concurrency
        self._running = 0
        self._running_tokens: set[object] = set()
        self._solo_active = False
        self._waiters: deque[_Waiter] = deque()

    async def acquire(self, tokens: frozenset[object], *, solo: bool) -> None:
        # Admitted on the spot when the gate has room, queue or no queue: a waiter here is
        # held up by a token or a solo lock, not by anything this caller could wait behind,
        # and making it wait anyway would leave a free slot idle. This is the same
        # barge-ahead the previous `Condition` had, and the same lack of a fairness
        # guarantee (`ROADMAP.md`).
        if self._admits(tokens, solo=solo):
            self._take(tokens, solo=solo)
            return
        waiter = _Waiter(tokens, solo, asyncio.get_running_loop().create_future())
        self._waiters.append(waiter)
        try:
            await waiter.future
        except asyncio.CancelledError:
            if waiter.future.done() and not waiter.future.cancelled():
                # Admitted by a `release` that ran before this cancellation was delivered:
                # the slot is already taken on this test's behalf, and its caller will never
                # reach the `finally` that would give it back.
                self.release(tokens, solo=solo)
            else:
                # `Task.cancel` cancels this future synchronously, so a `release` in the same
                # tick may already have dropped this waiter from the queue -- see `_wake`.
                with contextlib.suppress(ValueError):
                    self._waiters.remove(waiter)
            raise

    def release(self, tokens: frozenset[object], *, solo: bool) -> None:
        """Give back what `acquire` took, and admit whoever that makes room for.

        Synchronous on purpose. The caller (`dispatch_one`'s own outer `finally`) may already
        have a cancellation pending -- a sibling's `KeyboardInterrupt`/`SystemExit` propagating
        through `asyncio.TaskGroup`, or `on_result` raising -- and a release that could suspend
        is a release that can be interrupted before its bookkeeping runs, leaving `_running`/
        `_running_tokens` stuck and every test still queued on this gate deadlocked for the rest
        of the run. Nothing here awaits, so there is no point for that to happen at.
        """
        self._running -= 1
        if solo:
            self._solo_active = False
        else:
            self._running_tokens -= tokens
        self._wake()

    def _admits(self, tokens: frozenset[object], *, solo: bool) -> bool:
        """Whether the gate's current state has room for this test right now."""
        if solo:
            return self._running == 0
        return (
            self._running < self._concurrency
            and not self._solo_active
            and self._running_tokens.isdisjoint(tokens)
        )

    def _take(self, tokens: frozenset[object], *, solo: bool) -> None:
        """Book one admission's worth of state. Always paired with a later `release`."""
        if solo:
            self._solo_active = True
        else:
            self._running_tokens |= tokens
        self._running += 1

    def _wake(self) -> None:
        """Admit every queued waiter the state now has room for, from the head of the queue.

        The scan stops as soon as the gate is full again, so a release that frees one slot
        costs one step in the ordinary case; it walks past waiters held up by a token or a
        solo lock, since a later waiter may still fit in the slot they cannot use. Each
        waiter's admission is booked here, not when its coroutine resumes, so nothing can be
        admitted twice into the same slot.
        """
        index = 0
        while index < len(self._waiters) and not self._solo_active:
            waiter = self._waiters[index]
            if waiter.future.done():
                # Cancelled while queued: `Task.cancel` cancels the future it is suspended on
                # right there, before the coroutine resumes to take itself out of the queue.
                # Dropping it here rather than admitting it is what keeps a slot from being
                # handed to a test that is already gone -- and nobody would give it back.
                del self._waiters[index]
                continue
            if not self._admits(waiter.tokens, solo=waiter.solo):
                if self._running >= self._concurrency:
                    # Full, and only a release can change that -- which will call this again.
                    return
                index += 1
                continue
            del self._waiters[index]
            self._take(waiter.tokens, solo=waiter.solo)
            waiter.future.set_result(None)


def _current_task() -> asyncio.Task[Any] | None:
    """The running task, or `None` when there is no loop to have one.

    `asyncio.current_task()` raises instead, and the one place that matters is a task
    abandoned by an aborted run: the garbage collector finalizes its coroutine, running every
    `finally` it was suspended inside, from outside any loop.
    """
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


#: What each `StopController` reason reads as in a result and on screen.
_STOP_REASONS = {
    "maxfail": "the run stopped after --maxfail",
    "interrupt": "the run was interrupted (Ctrl-C)",
}


@final
class StopController:
    """The single decision to stop a run early, and the cancellation that carries it out.

    `--maxfail` reaching its threshold and a Ctrl-C mean the same two things: nothing new
    starts, and everything already in flight is cancelled where it stands rather than left to
    finish. A test cancelled this way reports CANCELLED -- `claim` is how `_run_one` tells this
    controller's cancellation apart from its own timeout or a sibling's collateral damage,
    and un-cancels the task in the same step so the test can still tear its fixtures down
    inside `teardown_grace`.

    Cancelling reaches only tests that have been admitted: one still queued on the
    `AdmissionGate` holds nothing to release, and `dispatch_one` drops it by checking
    `stopping` the moment it is admitted instead. Nothing here can make a test that swallows
    its cancellation stop -- `teardown_grace` after the stop, the tests still holding the run
    open are named on stderr instead, with what a second Ctrl-C would do about them.
    """

    def __init__(
        self,
        *,
        teardown_grace: float = DEFAULT_TEARDOWN_GRACE,
        note: Callable[[str], None] = lambda _text: None,
        on_interrupt: Callable[[], None] | None = None,
    ) -> None:
        self.teardown_grace = teardown_grace
        #: Where a line the user needs to see *while the run is still going* goes -- the
        #: teardown that overran its budget, the Ctrl-C acknowledgement. `run_suite` points
        #: this at the real stderr, since by then `sys.stderr` is a per-test `Router`.
        self.note = note
        self._on_interrupt = on_interrupt
        self._reason: str | None = None
        #: The admitted tests, by the task running each one -- the id is carried alongside so
        #: a test that ignores its cancellation can be named rather than counted.
        self._running: dict[asyncio.Task[Any], str] = {}
        self._cancelled: set[asyncio.Task[Any]] = set()
        #: Set by the second Ctrl-C: there is nothing graceful left to attempt, and the
        #: shutdown path must not wait on tasks that already ignored one cancellation.
        self.aborting = False

    @property
    def stopping(self) -> bool:
        return self._reason is not None

    @property
    def reason(self) -> str | None:
        """`"maxfail"`, `"interrupt"`, or `None` while the run is still going."""
        return self._reason

    @property
    def reason_text(self) -> str:
        """The stop reason as a cancelled test reports it."""
        return _STOP_REASONS.get(self._reason or "", "the run stopped")

    def in_flight(self) -> int:
        """How many tests are admitted and running right now."""
        return len(self._running)

    def request(self, reason: str) -> None:
        """Stop the run for `reason`, cancelling every test in flight but the caller's own.

        Idempotent, and deliberately first-reason-wins: a Ctrl-C during a `--maxfail` stop is
        the same stop, already under way. The caller is spared because the one place a
        `"maxfail"` stop is requested from is a test that has just finished and is about to
        record its own result.
        """
        if self._reason is not None:
            return
        self._reason = reason
        current = _current_task()
        for task in self._running:
            if task is not current:
                self._cancelled.add(task)
                task.cancel()
        # One check, `teardown_grace` from now: by then every cancelled test has either
        # finished or decided not to, and the second is the case worth naming -- a run that
        # stopped and then sat there is exactly what a user reads as voci hanging.
        asyncio.get_running_loop().call_later(self.teardown_grace, self._report_unresponsive)
        if reason == "interrupt" and self._on_interrupt is not None:
            self._on_interrupt()

    def _report_unresponsive(self) -> None:
        holding = sorted(self._running.values())
        if not holding:
            return
        self.note(
            f"voci: still waiting for {len(holding)} cancelled "
            f"{'test' if len(holding) == 1 else 'tests'} to stop: {', '.join(holding)} "
            f"(Ctrl-C again to abort)"
        )

    def register(self, test_id: str) -> None:
        """Enter the calling task into what a stop cancels. Called once the test is admitted,
        with no `await` between the caller's own `stopping` check and this."""
        task = _current_task()
        if task is not None:
            self._running[task] = test_id

    def unregister(self) -> None:
        """The reverse of `register`, whatever the test's envelope did."""
        task = _current_task()
        if task is not None:
            self._running.pop(task, None)
            self._cancelled.discard(task)

    def claim(self) -> bool:
        """Whether the `CancelledError` the caller just caught is one this controller
        delivered -- and if so, un-cancel the task so the rest of the test's envelope
        (teardown, releasing the admission gate, recording the result) can still `await`.

        Claimed at most once per cancellation: a test that catches, claims, and then somehow
        gets cancelled again is being cancelled by something that isn't this.
        """
        task = _current_task()
        if task is None or task not in self._cancelled:
            return False
        self._cancelled.discard(task)
        task.uncancel()
        return True


def run_suite(  # noqa: C901
    records: list[TestRecord],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float | None = None,
    capture_passthrough: bool = False,
    maxfail: int | None = None,
    basetemp: Path | None = None,
    unattributed_output: list[str] | None = None,
    on_result: Callable[[TestResult], None] | None = None,
    on_interrupt: Callable[[], None] | None = None,
    loop_watchdog: float | None = _safety.DEFAULT_LOOP_WATCHDOG,
    teardown_grace: float = DEFAULT_TEARDOWN_GRACE,
    filterwarnings: Sequence[str] = (),
    isolated: IsolatedConfig | None = None,
    already_isolated: bool = False,
) -> list[TestResult]:
    """Run every record concurrently on one `asyncio.Runner`, admitting at most
    `concurrency` tests into their setup/call/teardown envelope at a time via `AdmissionGate`,
    which also withholds a test whose `exclusive=` fixtures contend with one already running,
    and any test at all while a `@voci.solo` test is running. `concurrency=1` fully
    serializes, in collection order.

    `isolated`, required whenever any record carries a `@voci.isolated` mark, is that test's
    subprocess dispatch config (`isolated.IsolatedConfig`) -- rootdir and the parent's own
    assertion-rewrite decision, so its subprocess re-collects the same way the parent did.
    `already_isolated`, set only by `_isolated_worker`'s own call, runs every record in-process
    regardless of its `isolated` mark -- this call already *is* the dedicated subprocess that
    mark asked for, so honoring it again here would spawn a subprocess from a subprocess forever.

    `on_result`, if given, is called once per test, synchronously, in real completion
    order, immediately before that test's result is written into the returned list --
    which stays indexed by logical (collection) order regardless of when each slot was
    actually filled. This is the only way to observe results as they actually finish; a
    streaming reporter uses it to flush a file's block the moment that file's last test
    completes. An exception raised by `on_result` itself aborts the run, the same as a
    bug in test execution would.

    `maxfail`, if given, stops the run once that many results have a failing outcome:
    tests not yet started are dropped, and every test still in flight is cancelled and
    reports CANCELLED. The returned list is shorter than `records` by exactly the number
    dropped -- still in logical order, so a caller comparing the two lengths learns the
    run stopped early. A dropped test's module never reaches its last test, so that
    module's `scope="module"` fixtures are released at the end of the run with the
    session's rather than as its own last test finishes.

    A Ctrl-C stops the run the same way, and calls `on_interrupt` once if it was given --
    for the duration of this call `SIGINT` is handled here rather than by whatever had it
    (restored on the way out, and left alone entirely when this isn't the main thread).
    A second Ctrl-C raises `KeyboardInterrupt` on the spot instead, which aborts the call
    and returns nothing, the same as a `KeyboardInterrupt`/`SystemExit` raised by a test.
    A cancelled test gets `teardown_grace` seconds to release its fixtures before the run
    stops waiting for it, and so does a test whose own `--timeout`/`@voci.timeout(...)`
    budget expired -- an unbounded teardown there would hold that test's admission slot and
    hang the run outright.

    `loop_watchdog` is how many seconds the event loop may go unresponsive before the
    blocking call holding it is named on stderr (`safety.LoopWatchdog`); `None` or a
    non-positive value turns that off. It reports and never fails a test: voci has no
    way to tell a blocking call apart from a fixture that legitimately takes a while.

    `filterwarnings` is the run's own warning filters, in `action:message:category:module:lineno`
    form and lowest precedence first, under which every test's warnings are collected onto its
    `TestResult.warnings`; a `@voci.filterwarnings(...)` mark layers over them for one test.
    Warnings raised with no test running are `_warnings.session_warnings()`.

    `module`-scope fixtures are released once every test of that module has finished,
    not as each test's own teardown runs, so a module fixture stays alive for its
    still-running siblings. `unattributed_output`, if given, is populated with whatever
    output the session sink caught (see `_builtins/capture.py`'s module docstring). A
    `KeyboardInterrupt`/`SystemExit` raised by any test aborts the whole call and
    nothing is returned.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")
    if maxfail is not None and maxfail < 1:
        raise ValueError(f"maxfail must be >= 1, got {maxfail}")
    if timeout is not None and not (math.isfinite(timeout) and timeout > 0):
        raise ValueError(f"timeout must be a positive, finite number of seconds, got {timeout}")
    if not (math.isfinite(teardown_grace) and teardown_grace > 0):
        raise ValueError(
            f"teardown_grace must be a positive, finite number of seconds, got {teardown_grace}"
        )
    if isolated is None and not already_isolated:
        needs_isolation = next((r for r in records if r.marks.isolated), None)
        if needs_isolation is not None:
            raise ValueError(
                f"{needs_isolation.id!r} is marked @voci.isolated but run_suite was not given "
                f"isolated=IsolatedConfig(...) -- its subprocess needs rootdir (and the parent's "
                f"own assertion-rewrite decision) to re-collect it"
            )

    # Parsed before anything is installed: a malformed spec is a usage error, and one that
    # surfaced after `_capture.install` had swapped out sys.stdout would print into nothing.
    session_filters = _warnings.parse_filters(filterwarnings)

    results: list[TestResult | None] = [None] * len(records)
    store = _di.ScopeStore()

    # install() is the first thing here that can fail and the first thing that mutates
    # process-global state, in that order: it resolves its one fallible step
    # (basetemp_root) before touching sys.stdout/sys.stderr/the log handler, so a
    # failure here leaves nothing installed for the finally below to need to undo.
    capture_setup = _capture.install(passthrough=capture_passthrough, basetemp=basetemp)

    def note(text: str) -> None:
        """A line the user needs while the run is still going. `real_stderr`, not
        `sys.stderr`: that one is a `Router` by now, and would file this under whichever
        test happens to be running -- or, from the watchdog's own thread, under none."""
        print(text, file=capture_setup.real_stderr, flush=True)

    # Set before the try, like `cli.main`'s own hook bookkeeping: if `_mocking.install` itself
    # raised, the finally must not try to undo something that was never done.
    guard_installed = False
    warnings_installed = False
    unawaited_installed = False
    #: Set below, once there is a loop to watch; named here so the outer `finally` can stop
    #: it whatever happened in between.
    watchdog: _safety.LoopWatchdog | None = None
    try:
        # After collection has imported every test module, so a suite that patches has already
        # brought `unittest.mock` in and this finds it (`_mocking.install`). False when an
        # enclosing run already installed the guard, whose uninstall is then not ours to do.
        guard_installed = _mocking.install()
        # Before `_safety.install`, whose hook the shim this puts in place is what consults.
        warnings_installed = _warnings.install(session_filters)
        unawaited_installed = _safety.install()
        worker_slots = _capture.WorkerSlots(concurrency)
        gate = AdmissionGate(concurrency)
        stop = StopController(teardown_grace=teardown_grace, note=note, on_interrupt=on_interrupt)

        remaining_by_module: dict[Path, int] = {}
        for record in records:
            remaining_by_module[record.path] = remaining_by_module.get(record.path, 0) + 1
        pending_module_keys: dict[Path, list[_di.CacheKey]] = {}
        #: `maxfail`'s bookkeeping. Read and written only from `dispatch_one` bodies, which
        #: never `await` between counting a failure and asking `stop` to act on it, so the
        #: count can't be missed by a task admitted in between.
        failures = 0

        async def dispatch_one(index: int, record: TestRecord) -> None:
            nonlocal failures
            # Every test's task is created up front, so a stop is enforced here, as each
            # one is about to start, rather than by not creating the task at all. A test
            # that returns without filling its results slot is what shortens the returned
            # list.
            if stop.stopping:
                return
            # The whole body lives between `gate.acquire()` and `gate.release()`, including
            # the module-scope flush below, so concurrency=1 is a genuine exact-serial mode:
            # the next test cannot start until this one's admission -- module teardown
            # included -- is released.
            # The record's, not the function's: one `@voci.parametrize` case can carry marks
            # the next case does not (`voci.case(..., marks=...)`).
            marks = record.marks
            tokens = exclusive_tokens_of(record.plan)
            # One value, computed once and passed to both acquire and release: the two must
            # agree, or the gate's solo bookkeeping never unwinds.
            solo = marks.solo or solo_for_patching(record)
            await gate.acquire(tokens, solo=solo)
            # A `@voci.timeout(...)` mark overrides the suite-wide budget for this test
            # alone; `marks.timeout is None` is the common case of "no override", not "no
            # limit" -- that's what the bare `timeout` parameter already means.
            test_timeout = marks.timeout if marks.timeout is not None else timeout
            started = time.monotonic()
            try:
                # Re-checked now that this test holds a slot, which is the moment that
                # actually decides whether it runs: every task reaches the check above
                # before the first result exists, since they are all created together and
                # then queue on the gate. Registering for cancellation immediately after,
                # with no `await` in between, is what leaves no window in which a test is
                # running but a stop can't reach it.
                if stop.stopping:
                    return
                stop.register(record.id)
                try:
                    result = await run_envelope(record, marks, test_timeout, solo=solo)
                except asyncio.CancelledError:
                    # Only reached when the cancellation landed somewhere `_run_one` isn't
                    # -- between it and the module-scope flush below, or inside that flush
                    # -- since `_run_one` claims its own. Filling the slot here anyway is
                    # what keeps "every admitted test reports something" true.
                    if not stop.claim():
                        raise
                    result = _cancelled_result(record, time.monotonic() - started, stop)
                finally:
                    stop.unregister()

                # Inside the gate, ahead of the release below, and with no `await`
                # between this and the release: releasing is what admits the next
                # waiting test, so counting the failure afterwards would race that
                # test's own `stopping` check and let it start anyway.
                if maxfail is not None and result.outcome in FAILING_OUTCOMES:
                    failures += 1
                    if failures >= maxfail:
                        stop.request("maxfail")
            finally:
                # Skipped only when the run was aborted outright: this task is then being
                # finalized by the garbage collector, long after the loop was closed -- and
                # admitting a waiter means completing its future, which still needs that
                # loop. There is no test left waiting on the gate to admit anyway.
                if not stop.aborting:
                    gate.release(tokens, solo=solo)

            # Fired in real completion order, before the logical-order results slot
            # below is written, so a streaming reporter never sees a filled slot
            # for a test it hasn't been told about yet.
            if on_result is not None:
                on_result(result)
            results[index] = result

        async def flush_module_scope(record: TestRecord) -> None:
            """Release `record`'s module's fixtures, if `record` was the last of its module
            to finish. A stop landing in here is claimed rather than allowed to propagate:
            the test itself is already done and has a real result, which a cancellation
            escaping this far would replace with a synthetic CANCELLED one."""
            remaining_by_module[record.path] -= 1
            if remaining_by_module[record.path] != 0:
                return
            keys = pending_module_keys.pop(record.path, None)
            if not keys:
                return
            try:
                await _teardown_module_scope(
                    store,
                    keys,
                    path=record.path,
                    real_stderr=capture_setup.real_stderr,
                    # Time-boxed once the run is stopping, for the same reason a cancelled
                    # test's own teardown is.
                    grace=stop.teardown_grace if stop.stopping else None,
                )
            except asyncio.CancelledError:
                # Never re-raised: this test has already earned a result, and a cancellation
                # escaping here would replace it with nothing at all. `claim` when the stop
                # is what delivered it, so the task is left un-cancelled for the rest of its
                # envelope; a sibling's collateral cancellation is simply reported.
                stop.claim()
                stop.note(
                    f"voci: module-scope fixtures ({record.path}) were left mid-teardown -- "
                    f"they may not have been fully released"
                )

        async def run_envelope(
            record: TestRecord, marks: Marks, test_timeout: float | None, *, solo: bool
        ) -> TestResult:
            """One admitted test's whole setup/call/teardown envelope, including the
            module-scope flush it owes its module if it turns out to be its last test.
            Split out of `dispatch_one` so that everything a stop can cancel sits inside one
            `try`, and everything that must still happen afterwards -- the gate release, the
            result -- sits outside it."""
            if marks.isolated and not already_isolated:
                if isolated is None:
                    # Unreachable: run_suite checks this for every isolated-marked
                    # record before any test is dispatched.
                    raise RuntimeError(
                        f"{record.id!r} is marked @voci.isolated with no IsolatedConfig -- "
                        f"run_suite's own upfront check should have caught this"
                    )
                # No Sink/TestContext here -- this test's whole setup/call/teardown
                # envelope, capture included, runs inside the subprocess's own
                # run_suite call and comes back already resolved.
                result = _result_from_json(
                    await _isolated.run_isolated(
                        record,
                        config=isolated,
                        timeout=test_timeout,
                        basetemp_root=capture_setup.basetemp_root,
                        scratch_dir=capture_setup.basetemp_root / ".voci-isolated",
                        note=note,
                        loop_watchdog=loop_watchdog,
                        teardown_grace=teardown_grace,
                        # The session's filters only: the subprocess re-collects the test
                        # from its own source, so its `@voci.filterwarnings` mark comes
                        # back with it rather than being handed over.
                        filterwarnings=filterwarnings,
                    )
                )
                # `run_isolated` kills its subprocess and reports rather than propagating a
                # cancellation, so a stop that reached this test arrives here as a returned
                # error result. Claiming it turns that into the CANCELLED it actually is.
                if stop.claim():
                    return _cancelled_result(record, result.duration, stop)
                # No current_test_context to attribute this flush to -- it falls back to
                # the session sink, same as any output with no test actively running would.
                await flush_module_scope(record)
                return result

            # A fresh Sink and TestContext for this one test, published via
            # current_test_context.set() for the duration of everything below --
            # not just _run_one, but this test's own module-scope-fixture flush
            # too, if it turns out to be the module's last test. _run_one itself
            # never references _capture at all; every builtin-fixture provider and
            # the installed Router/log handler read current_test_context for
            # themselves, so wrapping the call is enough to attribute everything it
            # does, transitively, to this test.
            slot = worker_slots.acquire()
            sink = _capture.Sink(label=record.id)
            test_context = _capture.TestContext(
                sink=sink,
                tags=marks.tags,
                timeout=test_timeout,
                worker=slot,
                # Solo holds the whole gate; an isolated mark reaching this branch at
                # all means `already_isolated` -- this process was spawned for this one
                # test. Either way nothing else is running to see a process-global
                # patch, so `_mocking`'s guard lets one through.
                patching_allowed=solo or marks.isolated,
            )
            token = _capture.current_test_context.set(test_context)
            try:
                # Opened over the same span as the sink, for the same reason: a warning
                # raised by a fixture this test set up, or by the module teardown it owes
                # its module, is this test's to answer for.
                with _warnings.collecting(_warnings.parse_filters(marks.filterwarnings)) as warned:
                    try:
                        result, module_keys = await _run_one(
                            record, store, timeout=test_timeout, stop=stop
                        )
                    finally:
                        worker_slots.release(slot)

                    if module_keys:
                        pending_module_keys.setdefault(record.path, []).extend(module_keys)
                    # Inside this test's current_test_context: if this test is the module's
                    # last, a module-scope fixture's own teardown print is attributed to it.
                    await flush_module_scope(record)
            finally:
                # Reset only now that nothing else this test's envelope owns --
                # including, for the module's last test, that module's own fixture
                # teardown -- could still write into sink.
                _capture.current_test_context.reset(token)

            # Captured output is only worth keeping for a failing result; warnings are worth
            # keeping whatever the test did.
            if result.outcome in FAILING_OUTCOMES:
                result = dataclasses.replace(
                    result,
                    captured_stdout=sink.out,
                    captured_stderr=sink.err,
                    log_records=tuple(sink.log_records),
                )
            recorded = warned.recorded()
            return dataclasses.replace(result, warnings=recorded) if recorded else result

        # Constructed synchronously, outside the loop, so the finally below can shut
        # this down directly without going through the loop at all. At least one worker
        # thread per concurrency slot, so a sync test never waits for a thread while its own
        # timeout budget runs -- and never fewer than Python's own default, since this is
        # also the pool a test's `asyncio.to_thread(...)` lands in and a test that fans out
        # over several threads must not be able to deadlock against itself.
        executor = _capture.ContextPropagatingExecutor(
            max_workers=max(concurrency, _default_max_workers()),
            thread_name_prefix="voci-worker",
        )
        if loop_watchdog is not None and loop_watchdog > 0:
            watchdog = _safety.LoopWatchdog(
                loop_watchdog,
                report=note,
                test_ids=_safety.code_index(records),
                in_flight=stop.in_flight,
            )

        async def run_all() -> None:
            asyncio.get_running_loop().set_default_executor(executor)
            # Both of these need the running loop, which is why they are armed from in here
            # rather than alongside the executor above, and both are undone before this
            # returns so nothing outlives the call that installed it.
            restore_sigint = _install_interrupt_handler(stop, note=note)
            if watchdog is not None:
                watchdog.start()
            try:
                async with asyncio.TaskGroup() as tg:
                    for index, record in enumerate(records):
                        tg.create_task(dispatch_one(index, record))
            finally:
                if watchdog is not None:
                    watchdog.stop()
                restore_sigint()

        # Not `with asyncio.Runner() as runner:` -- Runner.close()'s own automatic
        # executor shutdown can raise RuntimeError when a custom default executor was
        # installed (as run_all does above) and the loop's last run propagated an
        # uncaught KeyboardInterrupt/SystemExit. runner.close() is called explicitly
        # below inside contextlib.suppress(RuntimeError) instead.
        runner = asyncio.Runner()
        # A KeyboardInterrupt/SystemExit raised by a test or fixture teardown escapes
        # this runner's loop mid-flight (see the two comments below), which leaves the
        # `run_all` task -- or one `asyncio.Runner.close()` resumes while cancelling
        # leftovers -- holding an exception nothing ever calls `.exception()` on. Both
        # exception types already propagate out of `run_suite` deliberately (that's the
        # whole point of the two comments below); logging them again as "Task exception
        # was never retrieved" is asyncio's bookkeeping noise, not a real error.
        runner.get_loop().set_exception_handler(_ignore_retrieved_base_exceptions)
        try:
            try:
                runner.run(run_all())
            finally:
                _teardown_best_effort(
                    runner,
                    store.aclose(),
                    what="session-scope fixtures",
                    real_stderr=capture_setup.real_stderr,
                    # Same reasoning as a cancelled test's own teardown: a stopped run
                    # waits a bounded time for a clean release and then stops waiting.
                    grace=stop.teardown_grace if stop.stopping else None,
                )
                # wait=False: blocking here to join worker threads would turn a Ctrl-C
                # into a hang if one of them is stuck. cancel_futures=True drops
                # whatever was still queued; nothing dispatched here ever needs to
                # finish once the run is over.
                executor.shutdown(wait=False, cancel_futures=True)
        finally:
            # A separate, outer finally rather than folded into the one above: this
            # ordering was verified to avoid a subtle interaction where store.aclose()
            # could otherwise leave an abandoned task under certain interrupt shapes.
            #
            # close() can itself raise KeyboardInterrupt/SystemExit (not just the
            # documented RuntimeError) when sibling tasks were still pending -- letting
            # that propagate is correct, and still safe: the outer try below guarantees
            # _capture.uninstall() still runs regardless of what leaves this finally.
            with contextlib.suppress(RuntimeError):
                if stop.aborting:
                    # `Runner.close()` cancels whatever is left and then waits for it,
                    # which is the one thing a second Ctrl-C means not to do: the tasks
                    # still here are the ones that ignored the first cancellation, and
                    # waiting on them again would hang the abort. The loop is closed out
                    # from under them instead, with its exception handler silenced first:
                    # abandoning those tasks is what the abort *is*, so asyncio's reports
                    # about them ("Task was destroyed but it is pending", a shielded
                    # release that outlived its loop) describe the instruction rather than
                    # a problem with it.
                    aborted_loop = runner.get_loop()
                    aborted_loop.set_exception_handler(lambda _loop, _context: None)
                    _close_abandoned_tasks(aborted_loop)
                    aborted_loop.close()
                else:
                    runner.close()
    finally:
        # Guaranteed to run whether the try above completed normally, raised a real
        # KeyboardInterrupt/SystemExit, or raised for some other reason entirely --
        # every statement between install() succeeding and here lives inside this try.
        #
        # The watchdog is stopped here as well as in `run_all`: a `KeyboardInterrupt`
        # raised by the signal handler leaves the loop without ever resuming `run_all`,
        # so that coroutine's own `finally` never runs. Stopping twice is a no-op;
        # leaving the thread behind would have it report the frozen heartbeat of a loop
        # that no longer exists, into whatever runs next in this process.
        if watchdog is not None:
            watchdog.stop()
        if guard_installed:
            _mocking.uninstall()
        if unawaited_installed:
            _safety.uninstall()
        if warnings_installed:
            _warnings.uninstall()
        if unattributed_output is not None:
            unattributed_output.extend(_capture.unattributed_sections(capture_setup.session_sink))
        # Before _capture.uninstall(), so `real_stderr` is still the stream nothing else is
        # writing to. A sync test whose blocking call outlived the run holds the process
        # open until it returns, and nothing in Python can interrupt it -- so it gets named
        # rather than left to look like voci hanging on exit.
        still_stuck = _safety.stuck_calls()
        if still_stuck is not None:
            print(still_stuck, file=capture_setup.real_stderr, flush=True)
        _capture.uninstall()
    # Every dispatch_one task unconditionally sets results[index] as its final action once
    # its envelope returns, and that envelope's own contract is that it never raises
    # anything but KeyboardInterrupt/SystemExit -- so the only slots still None here are
    # the tests a stop (`maxfail`, a Ctrl-C) dropped before they started; a test that had
    # started reports CANCELLED and fills its slot. Dropping the rest keeps this list to
    # results that describe a test that actually ran, in logical order.
    return [result for result in results if result is not None]


def _close_abandoned_tasks(loop: asyncio.AbstractEventLoop) -> None:
    """Close the coroutines of every task still pending on `loop`, swallowing what their
    cleanup raises. Only for an aborted run, where those tasks are the ones that ignored a
    cancellation and the loop is about to be closed under them.

    Closing each one here rather than leaving it to the garbage collector is what keeps the
    abort quiet: a coroutine finalized later runs the same `finally` blocks from outside any
    loop and outside its own context, where releasing a `ContextVar` token or asking for the
    running task raises -- and an exception raised during finalization is printed by the
    interpreter, after the last line the user asked for.
    """
    for task in asyncio.all_tasks(loop):
        coro = task.get_coro()
        # `close` is a coroutine's, not every awaitable's: a task wrapping something else
        # has nothing here to finalize early, and the loop closing under it is the whole
        # story anyway.
        closer = getattr(coro, "close", None)
        if closer is not None:
            with contextlib.suppress(Exception):
                closer()


def _default_max_workers() -> int:
    """What `ThreadPoolExecutor` would have sized itself to, spelled out here because the
    executor `run_suite` installs is sized against it: voci raises that floor to fit its own
    concurrency, and never lowers it."""
    return min(32, (os.cpu_count() or 1) + 4)


def _cancelled_result(record: TestRecord, duration: float, stop: StopController) -> TestResult:
    """The CANCELLED result for a test the run stopped out from under, for the two paths
    that can't get one from `_run_one` -- an `@voci.isolated` test, whose subprocess
    dispatch reports rather than propagates, and a cancellation that lands between
    `_run_one` returning and its module-scope flush finishing."""
    reason = f"cancelled: {stop.reason_text}"
    return TestResult(
        id=record.id,
        index=record.index,
        outcome=Outcome.CANCELLED,
        duration=duration,
        failure=reason,
        failure_summary=reason,
    )


def _install_interrupt_handler(
    stop: StopController, *, note: Callable[[str], None]
) -> Callable[[], None]:
    """Take `SIGINT` for the duration of the run, and return the callable that gives it back.

    The first Ctrl-C asks `stop` to cancel what is in flight, from the loop, so the run ends
    the way `--maxfail` ends it: cancelled tests report CANCELLED, fixtures get their
    teardown budget, and the reporter still prints everything the run found. The second
    raises `KeyboardInterrupt` where it stands -- the escape hatch for a test that ignores
    cancellation, or for a loop too blocked to process the first one -- and hands `SIGINT`
    back on the way, so a third lands on Python's own default handler.

    A no-op off the main thread, where `signal.signal` isn't allowed and nothing was going
    to deliver a `SIGINT` here anyway.
    """
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    loop = asyncio.get_running_loop()
    previous = signal.getsignal(signal.SIGINT)
    if previous is None:
        # `SIGINT`'s handler was installed from outside Python, so there is nothing here that
        # could put it back afterwards. Left alone rather than replaced with something this
        # run can't undo.
        return lambda: None

    presses = 0

    def interrupt() -> None:
        """The first Ctrl-C, back on the loop."""
        in_flight = stop.in_flight()
        note(
            f"voci: interrupted -- cancelling {in_flight} "
            f"{'test' if in_flight == 1 else 'tests'} in flight "
            f"(Ctrl-C again to abort immediately)"
        )
        stop.request("interrupt")

    def handler(signum: int, frame: Any) -> None:
        nonlocal presses
        presses += 1
        # This handler's own presses, not `stop.stopping`: a `--maxfail` stop has already
        # set that, and the first Ctrl-C of a run must never be the one that throws the
        # report away.
        if presses > 1:
            stop.aborting = True
            signal.signal(signal.SIGINT, previous)
            # Suppressed, not skipped: a signal handler runs wherever the main thread was,
            # which can be inside a buffered write to this very stream -- and re-entering
            # one raises. Losing the line is survivable; losing the abort is not.
            with contextlib.suppress(RuntimeError):
                note("voci: interrupted again -- aborting now")
            raise KeyboardInterrupt
        # Nothing is printed and nothing is cancelled from the handler itself. It runs
        # wherever the main thread happened to be -- mid-write to stderr, or inside another
        # task's frame -- so both jobs are handed to the loop, which is also what wakes an
        # otherwise idle run up to notice the Ctrl-C at all.
        loop.call_soon_threadsafe(interrupt)

    signal.signal(signal.SIGINT, handler)

    def restore() -> None:
        # Only if it is still ours: the second Ctrl-C restores `previous` itself, and a test
        # is free to install a handler of its own and leave it there.
        if signal.getsignal(signal.SIGINT) is handler:
            signal.signal(signal.SIGINT, previous)

    return restore


def _result_from_json(data: dict[str, Any]) -> TestResult:
    """The inverse of `isolated.result_to_json`: the dict an isolated test's subprocess wrote
    back, as a real `TestResult`. `log_records` are reconstructed via `logging.makeLogRecord`
    from just the three fields that crossed the process boundary -- `name`/`levelname`/the
    already-rendered message -- which is everything `_report.terminal` ever reads off one.
    """
    return TestResult(
        id=data["id"],
        index=data["index"],
        outcome=Outcome(data["outcome"]),
        duration=data["duration"],
        failure=data["failure"],
        failure_summary=data["failure_summary"],
        captured_stdout=data["captured_stdout"],
        captured_stderr=data["captured_stderr"],
        log_records=tuple(
            logging.makeLogRecord(
                {"name": rec["name"], "levelname": rec["levelname"], "msg": rec["message"]}
            )
            for rec in data["log_records"]
        ),
        # Absent from the dicts a subprocess that never ran the test at all produces
        # (`isolated._crash_result`, and the worker's own re-collection failure).
        warnings=tuple(_warnings.RecordedWarning(**w) for w in data.get("warnings", ())),
    )


async def _teardown_module_scope(
    store: _di.ScopeStore,
    keys: list[_di.CacheKey],
    *,
    path: Path,
    real_stderr: TextIO,
    grace: float | None = None,
) -> None:
    """Best-effort release of one module's accumulated fixture keys, once its last test
    finishes. Runs inside the loop (called from a `dispatch_one` task), unlike
    `_teardown_best_effort` below, which is for the two call sites still outside it.

    `grace`, when the run is stopping, bounds how long that release is waited on -- an
    overrun is reported here and then left, since by then there is nobody left to report it
    to but this stream.
    """
    try:
        async with asyncio.timeout(grace):
            await _di.teardown(store, keys)
    except TimeoutError:
        print(
            f"voci: module-scope fixtures ({path}) did not finish tearing down within "
            f"{grace}s of the run being stopped",
            file=real_stderr,
        )
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        # A cancellation is the run being stopped, not this teardown failing, and the caller
        # is the one that can tell those apart -- and that has a result to protect.
        raise
    except BaseException:
        print(f"voci: error tearing down module-scope fixtures ({path}):", file=real_stderr)
        traceback.print_exc(file=real_stderr)


def _ignore_retrieved_base_exceptions(
    loop: asyncio.AbstractEventLoop, context: dict[str, Any]
) -> None:
    """Suppress asyncio's default "exception was never retrieved" logging for a
    `KeyboardInterrupt`/`SystemExit`, both of which `run_suite` always re-raises itself;
    anything else still goes to `loop.default_exception_handler`.
    """
    if isinstance(context.get("exception"), (KeyboardInterrupt, SystemExit)):
        return
    loop.default_exception_handler(context)


def _teardown_best_effort(
    runner: asyncio.Runner,
    coro: Coroutine[Any, Any, None],
    what: str,
    *,
    real_stderr: TextIO,
    grace: float | None = None,
) -> None:
    """Run one end-of-scope teardown `coro` to completion from outside the loop (via
    `runner.run`), swallowing everything except `KeyboardInterrupt`/`SystemExit`.
    `_teardown_module_scope` above is the sibling for teardown triggered inside the
    loop, which must `await` directly rather than re-enter the runner. `grace` bounds how
    long a stopped run waits for it, the same way a cancelled test's own teardown is
    bounded.

    Prints to `real_stderr` -- the stream `_capture.install()` captured before
    replacing `sys.stderr` with a `Router` -- rather than `sys.stderr` itself, since by
    the time this runs `sys.stderr` is a `Router` and this call happens outside any
    `dispatch_one` task, so a plain print here would be attributed to the session sink
    and only surface in the unattributed-output section instead of live on stderr.
    """
    try:
        try:
            runner.run(_within(coro, grace))
        finally:
            # `_within` is a second coroutine wrapped around this one, and a `runner.run`
            # that raises before its first step (an interrupt already pending on this
            # loop) leaves the inner one never awaited -- which Python reports as a
            # RuntimeWarning against whatever is running whenever it is finally collected.
            # Closing it here is a no-op once it has run.
            coro.close()
    except TimeoutError:
        print(
            f"voci: {what} did not finish tearing down within {grace}s of the run being stopped",
            file=real_stderr,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        print(f"voci: error tearing down {what}:", file=real_stderr)
        traceback.print_exc(file=real_stderr)


async def _within(coro: Coroutine[Any, Any, None], grace: float | None) -> None:
    """`coro` under an `asyncio.timeout(grace)`, so a caller outside the loop can put a
    budget on something it hands to `runner.run`. A `grace` of `None` is `asyncio.timeout`'s
    own documented no-op."""
    async with asyncio.timeout(grace):
        await coro


def exit_code_for(
    results: list[TestResult], errors: list[CollectionError], skipped: int = 0
) -> int:
    """The process exit code for one run: `5` if nothing was collected at all (no
    records, no errors, no skips), `1` if there was a collection error or any
    `FAILED`/`ERROR`/`TIMEOUT` result, `0` otherwise.

    A CANCELLED result carries no exit code of its own -- what stopped the run is what
    decides that, and for a Ctrl-C it's `cli.main` reporting the interruption itself."""
    if not results and not errors and not skipped:
        return 5
    if errors or any(result.outcome in FAILING_OUTCOMES for result in results):
        return 1
    return 0
