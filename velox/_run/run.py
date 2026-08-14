"""Runs collected tests concurrently and reports each one's pass/fail/error/timeout/xfail/xpass
outcome.

Tests are dispatched as concurrent `asyncio` tasks, admitted by a shared `AdmissionGate` that
bounds how many run at once and, within that bound, withholds a test whose `exclusive=` fixtures
claim a resource another running test already holds, or that is running while a `@velox.solo`
test holds the whole gate to itself. A test velox found `unittest.mock` patching on at collection
takes the gate the same way a `@velox.solo` one does, and for the duration of the run `_mocking`'s
guard is installed, so a patch from any *other* test is refused rather than let loose on
everything running alongside it. Each admitted test gets a real setup -> call -> teardown
envelope, plus an optional per-test `asyncio.timeout` budget -- the suite-wide `--timeout`, or a
`@velox.timeout(...)` mark overriding it for that test alone. An `async def` test's call phase is
awaited directly; a sync `def` one runs on the context-propagating executor
(`_capture.ContextPropagatingExecutor`) instead, so a blocking call inside it holds only
its own concurrency slot rather than the shared event loop. A `@velox.isolated`-marked test is
admitted through the same gate but dispatched to a fresh subprocess instead (`isolated.py`),
re-collected there from its own source file and run alone on that process's own loop. Results
are collected back into logical (collection) order regardless of the order tests actually
finish in, so a run's output is reproducible independent of scheduling. This module also
derives the process exit code from the collected results and collection errors.
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
import time
import traceback
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO, cast, final

from velox import _mocking
from velox._builtins import capture as _capture
from velox._collection.collect import CollectionError, TestRecord
from velox._di import runtime as _di
from velox._di.fixtures import exclusive_tokens_of
from velox._marks import XFail, marks_of
from velox._run import isolated as _isolated

__all__ = [
    "FAILING_OUTCOMES",
    "AdmissionGate",
    "IsolatedConfig",
    "Outcome",
    "TestResult",
    "exit_code_for",
    "run_suite",
    "solo_for_patching",
]

#: Re-exported so a caller only needs `from velox._run import run` to build the one argument
#: `run_suite` needs for `@velox.isolated` -- see `isolated.py`'s own docstring for why that
#: module can't import this one back.
IsolatedConfig = _isolated.IsolatedConfig

#: Much larger than a typical core count: most suites are bound by a downstream
#: service's latency, not by CPU.
DEFAULT_CONCURRENCY = 16


class Outcome(enum.Enum):
    """A test's final disposition."""

    PASSED = "passed"
    FAILED = "failed"
    #: Setup or teardown raised, as opposed to the test's own call phase.
    ERROR = "error"
    #: The setup+call envelope exceeded its `--timeout` budget. Its own outcome rather
    #: than a flavor of FAILED/ERROR, since the fix (raise the budget, or find the
    #: blocking call) is different from either.
    TIMEOUT = "timeout"
    #: The call phase raised the failure a `@velox.xfail(...)` mark expected. Not a
    #: failing outcome regardless of `strict` -- `strict` only governs XPASSED.
    XFAILED = "xfailed"
    #: The call phase passed despite a `@velox.xfail(...)` mark. Only reached when
    #: `strict` is unset; a strict mark's unexpected pass reports FAILED instead, since
    #: at that point there is no "expected" disposition left to report.
    XPASSED = "xpassed"


#: Outcomes that count as failing, both for the process exit code and for which
#: results keep their captured stdout/stderr/log records around. XFAILED and XPASSED
#: are deliberately excluded: both mean the test behaved exactly as its `xfail` mark
#: said it would.
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
    #: Formatted traceback text, or a synthesized message for TIMEOUT. `None` iff
    #: `outcome` is PASSED. For ERROR this may be the setup traceback, the teardown
    #: traceback, or both concatenated if the call phase also failed.
    failure: str | None
    #: A short one-line "ExceptionType: message" summary of whichever exception
    #: decided `outcome`, read directly off the exception object rather than parsed
    #: back out of `failure`'s traceback text. `None` iff `outcome` is PASSED.
    failure_summary: str | None
    captured_stdout: str = ""
    captured_stderr: str = ""
    log_records: tuple[logging.LogRecord, ...] = ()


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
    `record.func` carries one. A `raises=` mismatch still reports FAILED -- the wrong
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
            f"test passed, but was marked @velox.xfail(reason={xfail.reason!r}, strict=True)",
            f"XPASS(strict): expected failure but the test passed ({xfail.reason})",
        )
    return (
        Outcome.XPASSED,
        f"expected failure ({xfail.reason}), but the test passed",
        f"XPASS: {xfail.reason}",
    )


async def _run_one(
    record: TestRecord, store: _di.ScopeStore, *, timeout: float | None
) -> tuple[TestResult, tuple[_di.CacheKey, ...]]:
    """Run one test's setup -> call -> teardown and fold the three phases into one
    `TestResult`.

    Also returns this test's own `module`-scope cache keys, still held open rather than
    released -- `run_suite` accumulates these across every test sharing a module and
    releases them together only once that module's last test has finished, which is
    what keeps `scope="module"` fixtures shared under concurrent dispatch.

    Teardown always runs once setup has acquired anything, whatever the call phase did.
    A timed-out envelope is reported TIMEOUT ahead of any other phase's failure; setup
    raising is ERROR; teardown raising is ERROR even over a passing call (both
    tracebacks are kept if the call also failed); otherwise FAILED or PASSED (call
    raised or not), reread as XFAILED/XPASSED when `record.func` carries a
    `@velox.xfail(...)` mark. `xfail` only ever reclassifies those last two -- a timeout,
    a setup error or a teardown error reports as such regardless of the mark, the same
    way pytest's own xfail only wraps the test's call phase.
    """
    start = time.monotonic()
    setup_failure: str | None = None
    setup_summary: str | None = None
    call_failure: str | None = None
    call_summary: str | None = None
    call_exc: BaseException | None = None
    teardown_failure: str | None = None
    teardown_summary: str | None = None
    cancelled_failure: str | None = None
    cancelled_summary: str | None = None
    timed_out = False
    setup_done = False
    kwargs: dict[str, Any] = {}
    keys: tuple[_di.CacheKey, ...] = ()
    module_keys: tuple[_di.CacheKey, ...] = ()
    partial_module_keys: list[_di.CacheKey] = []

    # asyncio.timeout(None) is a documented no-op, so entering it is unconditional.
    # Kept as a named local so .expired() can be checked after the block, below.
    deadline = asyncio.timeout(timeout)
    try:
        async with deadline:
            try:
                kwargs, keys = await _di.setup(
                    record.plan,
                    store,
                    test_id=record.id,
                    module_path=str(record.path),
                    partial_module_keys=partial_module_keys,
                )
                setup_done = True
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except BaseException as exc:
                setup_failure = traceback.format_exc()
                setup_summary = _summarize_exception(exc)

            if setup_failure is None:
                # `record.params` first, `kwargs` (this case's DI plan) second: collection
                # already rejects any name both `@velox.parametrize` and `Depends(...)` claim
                # (`_di._check_missing_injections`), so the two never actually overlap -- this
                # ordering is only a tie-breaker that can't be exercised.
                call_kwargs = {**(record.params or {}), **kwargs}
                try:
                    if inspect.iscoroutinefunction(record.func):
                        coro = cast("Coroutine[Any, Any, object]", record.func(**call_kwargs))
                        await coro
                    else:
                        # A sync `def test_*`: dispatched to the loop's default executor
                        # (`_capture.ContextPropagatingExecutor`, installed by `run_suite`)
                        # rather than called inline, so a blocking call in its body stalls
                        # only this test's own concurrency slot instead of the shared loop
                        # every other concurrently-dispatched test also runs on.
                        await asyncio.get_running_loop().run_in_executor(
                            None, functools.partial(record.func, **call_kwargs)
                        )
                except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                    raise
                except BaseException as exc:
                    call_failure = traceback.format_exc()
                    call_summary = _summarize_exception(exc)
                    call_exc = exc
    except TimeoutError:
        # Only reachable when `timeout` is not None. By the time this is caught,
        # `asyncio.timeout.__aexit__` has already converted its own cancellation into
        # TimeoutError; a CancelledError from anywhere else was re-raised, unconverted,
        # by the guards above and is caught separately below.
        timed_out = True
    except asyncio.CancelledError as exc:
        # Two different sources land here: this test's own timeout deadline (if its
        # CancelledError wasn't turned into TimeoutError above -- see the cross-check
        # below) or a collateral cancellation from a sibling task. `setup_done` decides
        # whether this reads as a setup or a call failure.
        cancelled_failure = traceback.format_exc()
        cancelled_summary = _summarize_exception(exc)
        if setup_done:
            call_failure = cancelled_failure
            call_summary = cancelled_summary
        else:
            setup_failure = cancelled_failure
            setup_summary = cancelled_summary

    if not timed_out and deadline.expired():
        # Cross-check: catches a timeout whose injected CancelledError never reached
        # either except clause above because the test's own code intercepted it and
        # substituted (or swallowed) a different exception. Any setup/call failure
        # captured on the way here is folded into the TIMEOUT text below rather than
        # discarded.
        timed_out = True

    # Gated on setup_done, not setup_failure is None: this also covers "the timeout
    # fired (or setup was cancelled) before setup returned", which leaves setup_failure
    # unset too.
    if setup_done:
        # key[0] is the scope tag every key _di.key_for returns starts with.
        module_keys = tuple(key for key in keys if key[0] == "module")
        other_keys = tuple(key for key in keys if key[0] != "module")
        try:
            await _di.teardown(store, other_keys)
        except (KeyboardInterrupt, SystemExit):
            raise
        except asyncio.CancelledError:
            # Not re-raised (unlike the setup/call guards above): teardown
            # runs outside the `async with deadline:` block, so there is no
            # asyncio.timeout __aexit__ left to hand a re-raise to, and no outer
            # handler in this function left to catch it -- re-raising here would let a
            # bare CancelledError escape _run_one, breaking run_suite's invariant that
            # every dispatched task fills its own results slot. Recorded as an ERROR
            # instead, tagged distinctly since a cancelled teardown may have left the
            # fixture only partially torn down.
            teardown_failure = (
                "teardown was cancelled (most likely collateral from a sibling's "
                "KeyboardInterrupt/SystemExit) -- the fixture may not have been fully torn "
                f"down:\n\n{traceback.format_exc()}"
            )
            teardown_summary = (
                "CancelledError: teardown cancelled (collateral from a sibling interrupt)"
            )
        except BaseException as exc:
            teardown_failure = traceback.format_exc()
            teardown_summary = _summarize_exception(exc)
    elif partial_module_keys:
        # Setup failed (or timed out) partway through but had already acquired a
        # module-scope key -- _di.setup leaves it unreleased, so it needs to reach
        # run_suite's module-lifetime accounting.
        module_keys = tuple(partial_module_keys)

    duration = time.monotonic() - start

    if timed_out:
        outcome = Outcome.TIMEOUT
        # Phrased without naming --timeout specifically: `timeout` here may be the
        # suite-wide budget or a per-test `@velox.timeout(...)` override, and this
        # message doesn't know which.
        failure = f"test exceeded its {timeout}s timeout budget"
        summary = failure
        # Whichever of these is set (never both) is the exception that actually
        # surfaced while the deadline was expiring, kept for context.
        extra = call_failure if call_failure is not None else setup_failure
        if extra is not None:
            failure = f"{failure}\n\n{extra}"
    elif setup_failure is not None:
        outcome, failure, summary = Outcome.ERROR, setup_failure, setup_summary
    elif teardown_failure is not None:
        outcome = Outcome.ERROR
        failure = (
            f"{call_failure}\n\n(teardown also failed)\n\n{teardown_failure}"
            if call_failure is not None
            else teardown_failure
        )
        # Call-first, mirroring failure's own text ordering just above.
        summary = call_summary if call_failure is not None else teardown_summary
    else:
        outcome, failure, summary = _resolve_call_outcome(
            marks_of(record.func).xfail,
            call_exc=call_exc,
            call_failure=call_failure,
            call_summary=call_summary,
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
    """Whether `record` runs alone because velox found `unittest.mock` patching on it.

    A `@velox.isolated` test is excluded: it patches its own subprocess, where there is nothing
    else to disturb, so it costs the suite no concurrency.
    """
    return bool(record.patches) and not marks_of(record.func).isolated


@final
class AdmissionGate:
    """The single point every dispatched test is admitted through: bounds how many run at once,
    and, within that bound, coordinates `exclusive=` fixtures and `@velox.solo`.

    Concurrency, `exclusive=`, and `solo` are one decision, not three layered ones -- a test
    piled up waiting on a contended token or a solo lock holds no concurrency slot while it
    waits, so it can never starve an unrelated test out of one. A test with no exclusive tokens
    and no `solo` mark is admitted as soon as a slot is free and no solo test is running. A test
    with exclusive tokens additionally needs none of them to overlap what's already running --
    acquired for its whole token set in one step, never one token at a time, so two tests can
    never deadlock each holding a token the other needs. A `solo` test is admitted only once
    nothing else is running, and blocks every other admission until it releases. Waiters have no
    fairness guarantee (`ROADMAP.md`).
    """

    def __init__(self, concurrency: int) -> None:
        self._condition = asyncio.Condition()
        self._concurrency = concurrency
        self._running = 0
        self._running_tokens: set[object] = set()
        self._solo_active = False

    async def acquire(self, tokens: frozenset[object], *, solo: bool) -> None:
        async with self._condition:
            if solo:
                await self._condition.wait_for(lambda: self._running == 0)
                self._solo_active = True
            else:
                await self._condition.wait_for(
                    lambda: (
                        self._running < self._concurrency
                        and not self._solo_active
                        and self._running_tokens.isdisjoint(tokens)
                    )
                )
                self._running_tokens |= tokens
            self._running += 1

    async def release(self, tokens: frozenset[object], *, solo: bool) -> None:
        # Shielded: the caller (`dispatch_one`'s own outer `finally`) may already have a
        # cancellation pending -- a sibling's KeyboardInterrupt/SystemExit propagating through
        # `asyncio.TaskGroup`, or `on_result` raising. Letting that interrupt the wait for
        # `_condition`'s lock before the bookkeeping below ran would leave `_running`/
        # `_running_tokens` permanently stuck, deadlocking every other test still waiting on
        # this gate for the rest of the run. `shield` lets that cancellation reach the caller
        # immediately while this still finishes in the background.
        await asyncio.shield(self._release(tokens, solo=solo))

    async def _release(self, tokens: frozenset[object], *, solo: bool) -> None:
        async with self._condition:
            self._running -= 1
            if solo:
                self._solo_active = False
            else:
                self._running_tokens -= tokens
            self._condition.notify_all()


def run_suite(
    records: list[TestRecord],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float | None = None,
    capture_passthrough: bool = False,
    basetemp: Path | None = None,
    unattributed_output: list[str] | None = None,
    on_result: Callable[[TestResult], None] | None = None,
    isolated: IsolatedConfig | None = None,
    already_isolated: bool = False,
) -> list[TestResult]:
    """Run every record concurrently on one `asyncio.Runner`, admitting at most
    `concurrency` tests into their setup/call/teardown envelope at a time via `AdmissionGate`,
    which also withholds a test whose `exclusive=` fixtures contend with one already running,
    and any test at all while a `@velox.solo` test is running. `concurrency=1` fully
    serializes, in collection order.

    `isolated`, required whenever any record carries a `@velox.isolated` mark, is that test's
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

    `module`-scope fixtures are released once every test of that module has finished,
    not as each test's own teardown runs, so a module fixture stays alive for its
    still-running siblings. `unattributed_output`, if given, is populated with whatever
    output the session sink caught (see `_builtins/capture.py`'s module docstring). A
    `KeyboardInterrupt`/`SystemExit` raised by any test aborts the whole call and
    nothing is returned.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")
    if timeout is not None and not (math.isfinite(timeout) and timeout > 0):
        raise ValueError(f"timeout must be a positive, finite number of seconds, got {timeout}")
    if isolated is None and not already_isolated:
        needs_isolation = next((r for r in records if marks_of(r.func).isolated), None)
        if needs_isolation is not None:
            raise ValueError(
                f"{needs_isolation.id!r} is marked @velox.isolated but run_suite was not given "
                f"isolated=IsolatedConfig(...) -- its subprocess needs rootdir (and the parent's "
                f"own assertion-rewrite decision) to re-collect it"
            )

    results: list[TestResult | None] = [None] * len(records)
    store = _di.ScopeStore()

    # install() is the first thing here that can fail and the first thing that mutates
    # process-global state, in that order: it resolves its one fallible step
    # (basetemp_root) before touching sys.stdout/sys.stderr/the log handler, so a
    # failure here leaves nothing installed for the finally below to need to undo.
    capture_setup = _capture.install(passthrough=capture_passthrough, basetemp=basetemp)
    # Set before the try, like `cli.main`'s own hook bookkeeping: if `_mocking.install` itself
    # raised, the finally must not try to undo something that was never done.
    guard_installed = False
    try:
        # After collection has imported every test module, so a suite that patches has already
        # brought `unittest.mock` in and this finds it (`_mocking.install`). False when an
        # enclosing run already installed the guard, whose uninstall is then not ours to do.
        guard_installed = _mocking.install()
        worker_slots = _capture.WorkerSlots(concurrency)
        gate = AdmissionGate(concurrency)

        remaining_by_module: dict[Path, int] = {}
        for record in records:
            remaining_by_module[record.path] = remaining_by_module.get(record.path, 0) + 1
        pending_module_keys: dict[Path, list[_di.CacheKey]] = {}

        async def dispatch_one(index: int, record: TestRecord) -> None:
            # The whole body lives between `gate.acquire()` and `gate.release()`, including
            # the module-scope flush below, so concurrency=1 is a genuine exact-serial mode:
            # the next test cannot start until this one's admission -- module teardown
            # included -- is released.
            marks = marks_of(record.func)
            tokens = exclusive_tokens_of(record.plan)
            # One value, computed once and passed to both acquire and release: the two must
            # agree, or the gate's solo bookkeeping never unwinds.
            solo = marks.solo or solo_for_patching(record)
            await gate.acquire(tokens, solo=solo)
            # A `@velox.timeout(...)` mark overrides the suite-wide budget for this test
            # alone; `marks.timeout is None` is the common case of "no override", not "no
            # limit" -- that's what the bare `timeout` parameter already means.
            test_timeout = marks.timeout if marks.timeout is not None else timeout
            try:
                if marks.isolated and not already_isolated:
                    if isolated is None:
                        # Unreachable: run_suite checks this for every isolated-marked
                        # record before any test is dispatched.
                        raise RuntimeError(
                            f"{record.id!r} is marked @velox.isolated with no IsolatedConfig -- "
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
                            scratch_dir=capture_setup.basetemp_root / ".velox-isolated",
                        )
                    )
                    remaining_by_module[record.path] -= 1
                    if remaining_by_module[record.path] == 0:
                        keys = pending_module_keys.pop(record.path, None)
                        if keys:
                            # No current_test_context to attribute this to -- it falls
                            # back to the session sink, same as any output with no test
                            # actively running would.
                            await _teardown_module_scope(
                                store,
                                keys,
                                path=record.path,
                                real_stderr=capture_setup.real_stderr,
                            )
                else:
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
                        try:
                            result, module_keys = await _run_one(
                                record, store, timeout=test_timeout
                            )
                        finally:
                            worker_slots.release(slot)

                        if module_keys:
                            pending_module_keys.setdefault(record.path, []).extend(module_keys)
                        remaining_by_module[record.path] -= 1
                        if remaining_by_module[record.path] == 0:
                            keys = pending_module_keys.pop(record.path, None)
                            if keys:
                                # Inside this test's current_test_context: this test is
                                # the module's last, so a module-scope fixture's own
                                # teardown print is attributed to it.
                                await _teardown_module_scope(
                                    store,
                                    keys,
                                    path=record.path,
                                    real_stderr=capture_setup.real_stderr,
                                )
                    finally:
                        # Reset only now that nothing else this test's envelope owns --
                        # including, for the module's last test, that module's own fixture
                        # teardown -- could still write into sink.
                        _capture.current_test_context.reset(token)

                    # Captured output is only worth keeping for a failing result.
                    if result.outcome in FAILING_OUTCOMES:
                        result = dataclasses.replace(
                            result,
                            captured_stdout=sink.out,
                            captured_stderr=sink.err,
                            log_records=tuple(sink.log_records),
                        )
            finally:
                await gate.release(tokens, solo=solo)

            # Fired in real completion order, before the logical-order results slot
            # below is written, so a streaming reporter never sees a filled slot
            # for a test it hasn't been told about yet.
            if on_result is not None:
                on_result(result)
            results[index] = result

        # Constructed synchronously, outside the loop, so the finally below can shut
        # this down directly without going through the loop at all.
        executor = _capture.ContextPropagatingExecutor()

        async def run_all() -> None:
            asyncio.get_running_loop().set_default_executor(executor)
            async with asyncio.TaskGroup() as tg:
                for index, record in enumerate(records):
                    tg.create_task(dispatch_one(index, record))

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
                runner.close()
    finally:
        # Guaranteed to run whether the try above completed normally, raised a real
        # KeyboardInterrupt/SystemExit, or raised for some other reason entirely --
        # every statement between install() succeeding and here lives inside this try.
        if guard_installed:
            _mocking.uninstall()
        if unattributed_output is not None:
            unattributed_output.extend(_capture.unattributed_sections(capture_setup.session_sink))
        _capture.uninstall()
    # Safe: every dispatch_one task unconditionally sets results[index] as its final
    # action once _run_one returns. _run_one's own contract is that it never raises
    # anything but KeyboardInterrupt/SystemExit, so a None surviving to here would mean
    # that contract was violated, not a gap in this function's own exception handling.
    return cast(list[TestResult], results)


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
    )


async def _teardown_module_scope(
    store: _di.ScopeStore, keys: list[_di.CacheKey], *, path: Path, real_stderr: TextIO
) -> None:
    """Best-effort release of one module's accumulated fixture keys, once its last test
    finishes. Runs inside the loop (called from a `dispatch_one` task), unlike
    `_teardown_best_effort` below, which is for the two call sites still outside it.
    """
    try:
        await _di.teardown(store, keys)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        print(f"velox: error tearing down module-scope fixtures ({path}):", file=real_stderr)
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
    runner: asyncio.Runner, coro: Coroutine[Any, Any, None], what: str, *, real_stderr: TextIO
) -> None:
    """Run one end-of-scope teardown `coro` to completion from outside the loop (via
    `runner.run`), swallowing everything except `KeyboardInterrupt`/`SystemExit`.
    `_teardown_module_scope` above is the sibling for teardown triggered inside the
    loop, which must `await` directly rather than re-enter the runner.

    Prints to `real_stderr` -- the stream `_capture.install()` captured before
    replacing `sys.stderr` with a `Router` -- rather than `sys.stderr` itself, since by
    the time this runs `sys.stderr` is a `Router` and this call happens outside any
    `dispatch_one` task, so a plain print here would be attributed to the session sink
    and only surface in the unattributed-output section instead of live on stderr.
    """
    try:
        runner.run(coro)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        print(f"velox: error tearing down {what}:", file=real_stderr)
        traceback.print_exc(file=real_stderr)


def exit_code_for(
    results: list[TestResult], errors: list[CollectionError], skipped: int = 0
) -> int:
    """The process exit code for one run: `5` if nothing was collected at all (no
    records, no errors, no skips), `1` if there was a collection error or any
    `FAILED`/`ERROR`/`TIMEOUT` result, `0` otherwise."""
    if not results and not errors and not skipped:
        return 5
    if errors or any(result.outcome in FAILING_OUTCOMES for result in results):
        return 1
    return 0
