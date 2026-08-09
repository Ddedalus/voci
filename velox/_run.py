"""Execution: run the collected tests on one shared loop and report pass/fail/error (spec/05).

M1 concurrency slice: tests are now dispatched as concurrent `asyncio` tasks under one
`asyncio.Semaphore(concurrency)` (spec/05 §1) instead of one at a time, each with an optional
per-test `asyncio.timeout` budget (spec/05 §2-4). Still deliberately not built this slice (spec/06,
a separate scheduler session): exclusive-resource admission, the solo write-lock tier, aging,
`--maxfail`, and `--seed` — `Fixture.exclusive` exists on the declarative side already but nothing
here consults it yet, so two tests that *should* be mutually exclusive can and will run
concurrently until that scheduler lands. Also still deferred: per-test `TaskGroup` for catching a
test's own leaked background tasks (spec/05 §2 item 1) — there is no public surface yet for test
code to spawn into one — and the full Ctrl-C/`--maxfail` cancellation choreography of spec/05 §6
(shielded, time-boxed teardown grace; `interrupted` outcome). What *is* built: `TaskGroup`-based
fan-out, semaphore-bounded concurrency, `asyncio.timeout` around setup+call producing a real
`Outcome.TIMEOUT`, and results collected back into logical (`index`) order regardless of completion
order (I2) — physical (completion) order is no longer the same as logical order at all once this
lands, unlike the M0/M1-DI sequential loop this replaces.

Each test still goes through real setup → call → teardown phases (spec/05 §3), driven by
`velox._di`. Four outcomes now (`PASSED`/`FAILED`/`ERROR`/`TIMEOUT`); the rest of the enum
(`skipped`/`xfailed`/`xpassed`/`interrupted`, spec/05 §4) still needs machinery this session
doesn't build.

This module also owns the exit code mapping, still a subset of spec/02 §4's full table (0/1/5
only — `2`/`3`/`4` need collection-error severity and internal-error detection this milestone
doesn't have yet).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import enum
import logging
import math
import time
import traceback
from collections.abc import Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO, cast

from velox import _capture, _di
from velox._collect import CollectionError, TestRecord
from velox._marks import marks_of

__all__ = ["Outcome", "TestResult", "exit_code_for", "run_suite"]

#: spec/05 §1's default: "N ≫ cores is deliberate... the limit is the downstream service's
#: tolerance, not CPU count."
DEFAULT_CONCURRENCY = 16


class Outcome(enum.Enum):
    PASSED = "passed"
    FAILED = "failed"
    #: Setup or teardown raised (spec/05 §3-4) — distinct from `FAILED`, which is reserved for the
    #: call phase itself. See `_run_one`'s docstring for the exact aggregation rule when more than
    #: one phase fails.
    ERROR = "error"
    #: The setup+call envelope exceeded its `--timeout` budget (spec/05 §4). Deliberately its own
    #: outcome rather than a flavor of `FAILED`/`ERROR`: the remedy (raise the budget, or find the
    #: blocking call) is different from either. Detected two ways (see `_run_one`'s docstring): the
    #: exception `asyncio.timeout` itself raises, cross-checked against the `Timeout` object's own
    #: `.expired()` state so a test that catches the injected `CancelledError` and substitutes (or
    #: swallows) a different exception is still correctly reported `TIMEOUT`, not `FAILED`/`ERROR`/
    #: `PASSED`. The one thing neither check can catch: a test body that never reaches an `await`
    #: point at all cannot be preempted by any mechanism (spec/05 §2) — cooperative scheduling has
    #: no way to interrupt code that never yields, so that specific case still runs to completion.
    TIMEOUT = "timeout"


#: The outcomes `exit_code_for` maps to exit code `1` and `dispatch_one` (`run_suite`) keeps
#: captured stdout/stderr/log records for (spec/05 §4's exit-code table; spec/09 §6's "only for
#: failing tests"). One shared tuple rather than two independently-written ones, so the two
#: policies ("this outcome affects the exit code" and "this outcome is worth keeping capture
#: for") can't silently drift apart — they happen to be the same set today, and there is no
#: principled reason for them to disagree.
_FAILING_OUTCOMES = (Outcome.FAILED, Outcome.ERROR, Outcome.TIMEOUT)


@dataclass(frozen=True, slots=True)
class TestResult:
    id: str
    index: int
    outcome: Outcome
    duration: float
    #: Formatted traceback text (or, for `TIMEOUT`, a synthesized message). `None` iff `outcome`
    #: is `PASSED`. For `ERROR`, this may be the setup traceback alone, the teardown traceback
    #: alone, or both concatenated when the call phase *also* failed before teardown ran (see
    #: `_run_one`).
    failure: str | None
    #: This test's captured stdout/stderr and structured log records (spec/09 §6), attached by
    #: `run_suite`'s `dispatch_one` — never by `_run_one` itself, which knows nothing about
    #: capture at all (module docstring's "keep this additive"). Left at these empty defaults for
    #: every `PASSED` result: spec/09 §6 is explicit that captured output for passing tests is
    #: dropped "as soon as the result is finalized, so memory is bounded by concurrency, not by
    #: suite size" — keeping it for every test in a large, mostly-green suite would instead bound
    #: memory by *suite size*, which is exactly the failure mode a size-capped `Sink` (spec/09 §1)
    #: exists to avoid one level up.
    captured_stdout: str = ""
    captured_stderr: str = ""
    log_records: tuple[logging.LogRecord, ...] = ()


async def _run_one(
    record: TestRecord, store: _di.ScopeStore, *, timeout: float | None
) -> tuple[TestResult, tuple[_di.CacheKey, ...]]:
    """Run one test's setup → call → teardown, and fold the phases into one `TestResult`.

    Also returns this test's own `module`-scope cache keys, still held open (not yet released).
    `run_suite` accumulates these across every test sharing one module and releases them together
    only once *every* test of that module has finished — see `run_suite`'s docstring for why that
    is what makes `scope="module"` fixtures actually shared under concurrent dispatch, and why it
    is also what keeps that release from ever racing a concurrent `acquire()` on the same key. This
    is true *regardless* of whether this test's own setup succeeded: `_di.setup` is called with
    `partial_module_keys` (see below), so a `module`-scope key acquired partway through a setup
    that then fails is folded into the returned tuple exactly the same as a fully successful
    setup's module keys — there is no separate, out-of-band release path for it to leak through.
    `function`/`call`/`session`-scope keys are released here (or, on a setup failure, inside
    `_di.setup`'s own cleanup — see its docstring), per test, same as always; a `session` key's
    `release` is always a harmless refcount-only decrement (real teardown is `aclose`'s job), so
    releasing it early changes nothing observable.

    `timeout` (`None` means no limit) wraps `setup()` and the test's own `call` together in one
    `asyncio.timeout` budget, matching spec/05 §2's envelope sketch — teardown itself is *not*
    time-boxed this slice (spec/05 §6's shielded, time-boxed teardown grace is a separate,
    not-yet-built cancellation-choreography session); it always gets a chance to run to completion
    once setup has actually acquired something, timeout or not.

    Aggregation rule (spec/05 §3-4's phase/outcome tables, made concrete):

    - The whole setup+call envelope times out: outcome is `TIMEOUT`, and it takes priority over
      every other phase's failure (see `timed_out`'s two sources below) — the deadline is what
      actually killed this test, whatever exception happened to surface on the way out.
      `function`/`call`/`session`-scope keys `_di.setup` had acquired before the deadline hit are
      already released by its own internal cleanup; any `module`-scope key it had acquired is
      folded into this function's own returned tuple instead (see above) rather than released or
      dropped. If the timeout instead lands during `call` (setup already succeeded), teardown still
      runs for what setup acquired.
    - Setup raises (not a timeout): outcome is `ERROR`, using setup's traceback. The call phase
      never runs.
    - Setup succeeds: the call phase always runs, and teardown *always* runs afterwards regardless
      of whether the call raised or timed out — a test must not leak its fixtures.
    - Teardown raising is what upgrades the outcome to `ERROR`, even over a passing call
      (spec/05 §3: "error, even if call passed"). If the call *also* failed, both tracebacks are
      kept (concatenated, call first) rather than one silently shadowing the other.
    - Otherwise (call succeeded, teardown succeeded): `PASSED`.

    `setup_done` gates whether teardown runs at all — deliberately not `keys` (a fixture-less test
    has a legitimately empty `keys` even after setup *succeeds*, so `keys` alone can't tell "setup
    never returned" apart from "setup returned instantly with nothing to acquire"; the two need to
    be told apart for the `CancelledError` phase-attribution below, even though they're
    interchangeable for gating teardown itself — teardown on an empty `keys` is a no-op regardless).

    `KeyboardInterrupt`/`SystemExit` are re-raised immediately out of every phase, same policy as
    before concurrency: they mean "stop the process," not "this phase misbehaved."

    `CancelledError` needs more care than either, because concurrency gives it two genuinely
    different sources this function must tell apart:

    1. **This test's own `--timeout` deadline.** `asyncio.timeout`'s `__aexit__` converts *its own*
       cancellation into `TimeoutError` — but only if the `CancelledError` it threw in is allowed
       to propagate all the way out of the `async with asyncio.timeout(...)` block untouched. So
       both inner phase handlers re-raise `CancelledError` rather than folding it into
       `setup_failure`/`call_failure` there, and the outer `except TimeoutError` below is what
       actually observes it — augmented by a direct `.expired()` check on the `Timeout` object
       after the block (see the local `deadline` variable), because `__aexit__` only *raises*
       `TimeoutError` when the block exits *with* an exception it can still see: if a test's own
       code catches the injected `CancelledError` and raises a *different* exception instead of
       letting it propagate (or catches it and raises nothing at all), the inner phase handler
       records that as an ordinary `setup_failure`/`call_failure` and the `async with` block exits
       looking clean to `__aexit__`, so no `TimeoutError` is ever produced — CPython's own
       `Timeout.__aexit__` still transitions its internal state from `EXPIRING` to `EXPIRED`
       unconditionally in that case (only the exception-raising/-annotating half is gated on
       `exc_type is not None`), so `.expired()` sees the deadline fired even when the exception
       channel alone would have missed it entirely. This does not close every gap `asyncio.timeout`
       has: a test body that never hits an `await` point at all cannot be preempted by *any*
       mechanism (spec/05 §2's "blocking calls occupy a concurrency slot for their whole duration")
       — that is a fundamental property of cooperative scheduling, not something either detection
       path can fix.
    2. **Anything else** — the test cancelling its own task directly (`_cancels_itself`-style,
       predating concurrency: M0/M1's sequential runner already had to handle a test doing this,
       since nothing about a task cancelling itself ever required real concurrency to reach), or
       this task getting cross-task-cancelled by `run_suite`'s `TaskGroup` because a *sibling*
       raised `KeyboardInterrupt`/`SystemExit`. Both reach the outer `except asyncio.CancelledError`
       below, once `asyncio.timeout` has already had its chance and declined (its `__aexit__`
       leaves a non-`TimeoutError` `CancelledError` completely alone when the deadline wasn't its
       own — and `.expired()` agrees, since `_on_timeout` never ran). Folding *this* task's
       collateral cancellation into an ordinary `ERROR`/`FAILED` outcome instead of re-raising is
       safe even in the sibling-interrupt case: the sibling's own `KeyboardInterrupt`/`SystemExit`
       still propagates out of *its* task unimpeded (nothing here ever catches those two) and still
       aborts `run_suite` correctly (see its docstring) — nothing depends on *this* task also
       re-raising a bare `CancelledError` for that to work, and doing so would instead risk leaving
       this index's `results` slot never assigned.
    """
    start = time.monotonic()
    setup_failure: str | None = None
    call_failure: str | None = None
    teardown_failure: str | None = None
    cancelled_failure: str | None = None
    timed_out = False
    setup_done = False
    kwargs: dict[str, Any] = {}
    keys: tuple[_di.CacheKey, ...] = ()
    module_keys: tuple[_di.CacheKey, ...] = ()
    partial_module_keys: list[_di.CacheKey] = []

    # `asyncio.timeout(None)` is a documented no-op (no deadline scheduled), so entering it is
    # unconditional rather than branching on whether `timeout` was passed. Kept as a named local
    # (rather than only appearing in the `async with` below) so `.expired()` can be consulted after
    # the block, per the docstring's `CancelledError` §1 paragraph.
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
            except BaseException:
                setup_failure = traceback.format_exc()

            if setup_failure is None:
                try:
                    # `func` is typed as a plain `Callable[..., object]` (`TestRecord` never wraps
                    # it), but only `async def test_*` is ever collected (`_is_own_test_function`),
                    # so the call always produces a coroutine at runtime once argument binding
                    # succeeds — a `kwargs` mismatch (which `_di`/`plan_for`'s static checks should
                    # already have ruled out) would raise here, synchronously, before `await` runs.
                    coro = cast("Coroutine[Any, Any, object]", record.func(**kwargs))
                    await coro
                except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                    raise
                except BaseException:
                    call_failure = traceback.format_exc()
    except TimeoutError:
        # Only reachable when `timeout` is not `None` — `asyncio.timeout(None)` never raises this.
        # By the time this is caught, `asyncio.timeout.__aexit__` has already converted *its own*
        # cancellation into `TimeoutError`; a `CancelledError` from anywhere else was re-raised,
        # unconverted, by the guards above and is caught separately below, not here.
        timed_out = True
    except asyncio.CancelledError:
        # See the docstring's `CancelledError` paragraph. `setup_done` is what decides whether this
        # reads as a setup or a call failure — the same distinction the two inner handlers would
        # have recorded themselves had they been allowed to catch it directly.
        cancelled_failure = traceback.format_exc()
        if setup_done:
            call_failure = cancelled_failure
        else:
            setup_failure = cancelled_failure

    if not timed_out and deadline.expired():
        # The cross-check the docstring's `CancelledError` §1 paragraph describes: catches a
        # timeout whose injected `CancelledError` never survived to reach either `except` clause
        # above (or reached `except asyncio.CancelledError` above under ambiguous simultaneous
        # sibling-cancellation) because user code intercepted it and substituted, or swallowed,
        # something else. `setup_failure`/`call_failure` (if either got set on the way here) is
        # folded into the `TIMEOUT` failure text below rather than discarded, so a substituted
        # exception's own text is not lost even though it no longer decides the outcome.
        timed_out = True

    # Gate on `setup_done`, not `setup_failure is None`: covers "setup raised outright" and "the
    # timeout fired (or setup was cancelled) before setup returned" identically (both leave
    # `setup_done` `False`), and is a no-op-safe no-op for the trivial empty-plan test too — see
    # the docstring's `setup_done` paragraph for why `keys` alone can't make this distinction.
    if setup_done:
        # `key[0]` is the scope tag every shape `_di.key_for` returns starts with — reading it
        # back here is cheaper and more honest than threading a parallel scope list through
        # `_di.setup`'s return value just for this one caller.
        module_keys = tuple(key for key in keys if key[0] == "module")
        other_keys = tuple(key for key in keys if key[0] != "module")
        try:
            await _di.teardown(store, other_keys)
        except (KeyboardInterrupt, SystemExit):
            raise
        except asyncio.CancelledError:
            # Deliberately *not* re-raised, unlike the setup/call guards above — and that is not
            # the same asymmetry it looks like. Those two re-raise so `asyncio.timeout`'s own
            # `__aexit__` gets first refusal at converting *its own* cancellation into
            # `TimeoutError`; the re-raise is then caught by *this function's own* outer
            # `except asyncio.CancelledError` a few lines up, so it never actually escapes
            # `_run_one` — the task itself still completes normally, still returns an ordinary
            # `TestResult`. Teardown runs *outside* the `async with deadline:` block (teardown is
            # deliberately not time-boxed this slice), so there is no `__aexit__` left to hand a
            # re-raise to and no outer handler left inside this function to catch it again:
            # re-raising here would let a bare `CancelledError` escape `_run_one` itself, which
            # would break `run_suite`'s "every dispatched task unconditionally sets its own
            # `results[index]`" invariant whenever the cancellation is self-inflicted (a fixture's
            # own teardown cancelling its task — unlikely, but the same class of thing
            # `_cancels_itself` already covers for the call phase) rather than sibling-driven,
            # since nothing else would then also abort `run_suite` to explain the missing slot.
            # So this is still recorded as an `ERROR`, same as any other teardown failure, but
            # tagged distinctly from an ordinary fixture bug: a cancelled teardown (most often
            # collateral from a sibling's `KeyboardInterrupt`/`SystemExit`, see `run_suite`'s
            # docstring) may have left the fixture only partially torn down, which is genuinely
            # error-shaped and must not be reported `PASSED`, but it is not necessarily a bug in
            # the fixture itself.
            teardown_failure = (
                "teardown was cancelled (most likely collateral from a sibling's "
                "KeyboardInterrupt/SystemExit) -- the fixture may not have been fully torn "
                f"down:\n\n{traceback.format_exc()}"
            )
        except BaseException:
            teardown_failure = traceback.format_exc()
    elif partial_module_keys:
        # Setup failed (or timed out) partway through, but it had already acquired a `module`-scope
        # key before that happened — `_di.setup` deliberately did not release it (see its
        # docstring), so it is still live and still needs to reach `run_suite`'s module-lifetime
        # accounting the same as a fully successful setup's module keys would.
        module_keys = tuple(partial_module_keys)

    duration = time.monotonic() - start

    if timed_out:
        outcome = Outcome.TIMEOUT
        failure = f"test exceeded the --timeout={timeout}s budget"
        # Whichever of these is set (never both — `call_failure` implies setup succeeded, which
        # means `setup_failure` was never set) is the exception that actually surfaced while the
        # deadline was expiring, kept for context even though the budget is the headline.
        extra = call_failure if call_failure is not None else setup_failure
        if extra is not None:
            failure = f"{failure}\n\n{extra}"
    elif setup_failure is not None:
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


def run_suite(
    records: list[TestRecord],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float | None = None,
    capture_passthrough: bool = False,
    basetemp: Path | None = None,
    unattributed_output: list[str] | None = None,
) -> list[TestResult]:
    """Run every record concurrently on one `asyncio.Runner`, semaphore-bounded (spec/05 §1).

    spec/09 additions, all additive to the concurrency machinery below (module docstring — none
    of it touches `_run_one`'s own carefully-documented phase/outcome logic):

    - `_capture.install(...)`/`.uninstall()` bracket the whole call, mirroring `_rewrite.py`'s
      idempotent install/uninstall pattern — `sys.stdout`/`sys.stderr` become `Router`s and the
      root logging handler goes up for exactly this call's duration, torn down in a `finally` so
      nothing survives a `run_suite` call that raises (I1). `capture_passthrough` is `-s`/
      `--capture=no` (spec/09 §1); `basetemp` is `--basetemp` (spec/09 §5), `None` meaning "pick a
      fresh numbered root".
    - Each dispatched test gets its own `_capture.Sink` and a `_capture.TestContext` (sink, tags,
      the effective `timeout`, and a `_capture.WorkerSlots`-assigned worker index), published via
      `_capture.current_test_context.set(...)`/`.reset(token)` in `dispatch_one` — *around* the
      `_run_one` call, not inside it, which is what keeps this additive: `_run_one` never
      imports, references, or needs to know `_capture` exists at all. This is safe under
      concurrency for the same reason the rest of this function already is (see `TestContext`'s
      own docstring): `.set()` inside one `asyncio.Task` mutates only that task's private copy of
      the context, so no two concurrently-dispatched `dispatch_one` calls can ever see each
      other's `Sink`.
    - `captured_stdout`/`captured_stderr`/`log_records` are folded onto the `TestResult`
      `dispatch_one` already builds, via `dataclasses.replace`, but only when the outcome is in
      `_FAILING_OUTCOMES` — spec/09 §6's "only for failing tests, dropped immediately for
      everything else" is enforced right here, at the one place a passing result's `Sink` ever
      stops being referenced by anything.
    - `unattributed_output`, if given, is populated (mutable out-parameter, same idiom
      `_di.setup`'s `partial_module_keys` already uses) with whatever the session sink caught —
      see `_capture.py`'s module docstring for exactly what that is under this runtime.

    One `asyncio.Runner` for the whole call, one `_di.ScopeStore` for the whole call — unchanged
    from before this slice. What's new: every record gets its own `asyncio.Task` (via one
    top-level `asyncio.TaskGroup`), gated by a shared `asyncio.Semaphore(concurrency)` so at most
    `concurrency` tests are actually inside their setup/call/teardown envelope at once.
    `concurrency=1` degenerates to an exact serial mode through this *same* code path (spec/06 §7)
    rather than a separate sequential runner: the semaphore admits tasks strictly one at a time,
    in the order they were created (`asyncio.Semaphore` is FIFO), which is `records` order, which
    is logical order (spec/03 §1) — so it fully serializes and reproduces the old M0/M1-DI
    behavior, just through the concurrent machinery instead of around it.

    Results are written into a pre-sized `index`-keyed list rather than appended as tests finish,
    so the returned order is always logical order (I2) even though completion order is now a
    function of scheduling and is *not* guaranteed to match it at all.

    `module`-scope fixture lifetime under concurrency: a naive "release this test's module keys
    the moment its own teardown runs" would tear a module fixture down the instant the *first* of
    that module's concurrently-dispatched tests finishes, while its siblings are still mid-flight
    and still hold live references to it — `scope="module"` degenerating into `scope="function"`
    with extra steps. Instead, `remaining_by_module` is seeded with each module's total test count
    up front, decremented as each of its tests finishes (`_run_one` already returns its module
    keys instead of releasing them, exactly so this function can hold them open), and only once a
    module's count reaches zero — meaning *every* test that could ever ask for that module's
    fixtures has already run its own `acquire()` during setup — are the accumulated keys released
    together. This "all acquires happen-before the one release" ordering is also what keeps this
    from reopening `_di.ScopeStore.release`'s documented narrow concurrent-teardown window (still
    real, still deferred, see its docstring): a `release()` for a given key never overlaps an
    `acquire()` for that *same* key here, because by construction nothing will ever `acquire()` it
    again once the release fires — but that is only true because `_run_one` returns *every*
    `module`-scope key it ever acquires here, success or failure alike, rather than some of them
    being released by a second, out-of-band path. `_di.setup`'s own partial-failure cleanup would
    otherwise be exactly such a path (it releases whatever it managed to acquire before failing) —
    it is told not to, for `module` scope specifically, via the `partial_module_keys` parameter
    `_run_one` passes it (see both docstrings); this function is the *only* caller that ever
    releases a `module`-scope key. `session`-scope keys need no such choreography — `release()` is
    always a refcount-only no-op for them (real teardown is `aclose`'s job, strictly after every
    task has finished), so they're released the instant each test's own teardown (or setup-failure
    cleanup) runs, same as `function`/`call` scope. Also unlike the boundary-based code this
    replaces, nothing here depends on `_collect.collect` emitting one module's records
    contiguously — `remaining_by_module`'s count is taken over the whole list up front and
    decremented from whichever task happens to finish, in whatever order that turns out to be. That
    dependency was dropped rather than silently relied on: `collect` *does* still happen to produce
    contiguous records today, so this is deliberate future-proofing (for a `--seed`/aging scheduler
    that reorders `records` freely) rather than an accident nothing currently exercises.

    `KeyboardInterrupt`/`SystemExit`: `_run_one` re-raises both immediately out of any phase of any
    test (its own docstring). `asyncio.TaskGroup` special-cases exactly these two types itself —
    `_on_task_done` records the first one it sees as `self._base_error`, and `_aexit` does a bare
    `raise self._base_error` *before* it ever constructs an `ExceptionGroup`/`BaseExceptionGroup`
    (verified against CPython 3.13's `asyncio.taskgroups`) — so `runner.run(run_all())` below raises
    the original `KeyboardInterrupt`/`SystemExit` object directly; nothing here needs to catch and
    unwrap anything. (The one shape that would *not* come out this way is a test raising a
    `BaseExceptionGroup` that itself *contains* a `KeyboardInterrupt`/`SystemExit` — not a
    `TaskGroup` base error, so it would land in a real group. `_run_one`'s own contract already
    forecloses this: it never lets anything but a bare `KeyboardInterrupt`/`SystemExit` escape, so
    a test raising a `BaseExceptionGroup` of its own is instead caught by `_run_one`'s ordinary
    `except BaseException` and reported as an ordinary `FAILED`/`ERROR` result, same as any other
    exception a test raises — it never reaches `run_all`'s `TaskGroup` at all.) `store.aclose()`
    still runs in a `finally` unconditionally, same as before, so best-effort session-scope teardown
    happens even on this path. Known, deliberate gap, same shape as the ones this codebase already
    documents elsewhere: a module whose tests were still in flight when the interrupt landed never
    reaches `remaining_by_module == 0` and its accumulated module-scope keys are simply not
    released here — `aclose()`'s own sweep (see its docstring) is the safety net, and it no longer
    only ever finds session-scope entries once concurrency can leave other scopes stranded there
    too; it doesn't care what scope an entry claims, it just tears down whatever's left.

    `results[index]` vs `TestResult.index`: the slot a result is written to is this loop's own
    `enumerate` position; the `index` a `TestResult` *reports* is `record.index`, assigned once by
    `_collect.collect` across the whole concatenation of files. The two agree today only because
    `records` is always the complete, unfiltered collection — there is no `-k`/`-m` selection
    feature yet (spec/02) that would ever hand this function a *subset*. Once one exists, whoever
    builds it needs to decide up front whether `TestResult.index` should keep meaning "stable
    collection id" (in which case `results[i].index != i` becomes possible and expected) or whether
    selection should renumber, and update this docstring's "returned order is always logical order"
    claim to say which of the two it means.

    Full Ctrl-C/`--maxfail` cancellation choreography (an `interrupted` outcome, shielded
    time-boxed teardown grace, salvaging partial results) is spec/05 §6, not built this slice —
    same as the M0/M1-DI sequential runner, an interrupt here still aborts the whole call with
    nothing returned to `cli.main` rather than a partial `list[TestResult]`.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")
    if timeout is not None and not (math.isfinite(timeout) and timeout > 0):
        raise ValueError(f"timeout must be a positive, finite number of seconds, got {timeout}")

    results: list[TestResult | None] = [None] * len(records)
    store = _di.ScopeStore()
    worker_slots = _capture.WorkerSlots(concurrency)
    # Review (should fix): `install()` mutates process-global state (`sys.stdout`, `sys.stderr`,
    # the root logging handler) but is called ~100 lines above this function's `try:`, so nothing
    # between here and there is covered by the `finally` that calls `uninstall()`. If `install()`
    # itself raises partway -- which it can, `_resolve_basetemp_root` is fallible and runs after
    # the streams are already swapped (see the note there) -- the process is left with orphaned
    # `Router`s and no way to remove them. Even ignoring that, `ContextPropagatingExecutor()` and
    # `asyncio.Runner()` are both constructed after this line and before the `try`. Moving
    # `install()` to be the first statement inside the existing `try:` (or giving it its own
    # immediately-following `try/finally`) costs nothing and makes the I1 claim in the docstring
    # actually hold.
    capture_setup = _capture.install(passthrough=capture_passthrough, basetemp=basetemp)

    remaining_by_module: dict[Path, int] = {}
    for record in records:
        remaining_by_module[record.path] = remaining_by_module.get(record.path, 0) + 1
    pending_module_keys: dict[Path, list[_di.CacheKey]] = {}

    async def dispatch_one(index: int, record: TestRecord, semaphore: asyncio.Semaphore) -> None:
        # The whole body lives inside `async with semaphore:` — including the module-scope flush
        # below, not just `_run_one` — so the semaphore genuinely bounds "tests inside their
        # setup/call/teardown envelope, including the last one's module-fixture teardown" the way
        # this function's docstring claims, and so `concurrency=1` is a real exact-serial mode: the
        # next test cannot even start setup until this one's slot (module teardown included) is
        # released. The cost is that the *last* test to finish a module holds its slot a little
        # longer while that module's fixtures tear down; that is the correct trade for what the
        # semaphore is documented to bound.
        async with semaphore:
            # spec/09's whole attribution mechanism, in three lines: a fresh `Sink` for this one
            # test, a `TestContext` bundling it with this test's tags/effective-timeout/worker
            # slot, and `.set()` to publish it for the duration of `_run_one` below. `_run_one`
            # itself never sees any of this — it doesn't import `_capture` and doesn't need to;
            # every builtin-fixture provider and the installed `Router`/log handler read
            # `current_test_context` for themselves (see their own docstrings), so wrapping the
            # `_run_one` call is sufficient to attribute *everything* that call does, transitively,
            # to this test — including output from fixtures it constructs.
            slot = worker_slots.acquire()
            sink = _capture.Sink(label=record.id)
            test_context = _capture.TestContext(
                sink=sink, tags=marks_of(record.func).tags, timeout=timeout, worker=slot
            )
            token = _capture.current_test_context.set(test_context)
            try:
                result, module_keys = await _run_one(record, store, timeout=timeout)
            finally:
                # Reset before releasing the slot, not after: a reset is a context-local no-op
                # (see `TestContext`'s docstring — nothing else could ever have observed this
                # task's value regardless of order), but releasing the slot first would let a
                # *newly* `acquire()`-ing sibling briefly reuse the same worker index while this
                # task's own `TestContext` is technically still live in its own, about-to-be-
                # discarded context — harmless either way, but resetting first is the more
                # obviously-correct order to read.
                _capture.current_test_context.reset(token)
                worker_slots.release(slot)
            # Review (documentation): the comment above the `set()` says wrapping the `_run_one`
            # call attributes "*everything* that call does, transitively, to this test", and this
            # function's docstring plus `_capture.TestContext`'s both say the context is set
            # "around that test's entire setup/call/teardown envelope". `_teardown_module_scope`
            # below is part of that envelope for the last test of a module, and it runs after this
            # `reset` -- so a module-scope fixture's teardown output is *not* attributed to any
            # test. Verified: a module-scope async-generator fixture printing on both sides of its
            # `yield`, with one failing test -- the setup print lands in `result.captured_stdout`,
            # the teardown print lands in `unattributed_output` instead. It costs nothing to fix
            # (the teardown already runs inside this test's semaphore slot; moving the `reset`
            # after the module flush attributes it to the module's last test, which is the only
            # test that could sensibly own it), and it would also stop a broken module fixture's
            # diagnostic output from being reported several screens away from the tests it broke.

            # spec/09 §6: captured output is only worth keeping for a failing result — folded on
            # here, via `dataclasses.replace` rather than a `TestResult` constructor parameter
            # `_run_one` would have to grow, which is exactly the "additive, don't restructure
            # `_run_one`" boundary this module's own docstring draws. For `PASSED`, `sink` (and
            # its buffers) simply becomes unreferenced once this function returns — nothing further
            # to drop.
            if result.outcome in _FAILING_OUTCOMES:
                result = dataclasses.replace(
                    result,
                    captured_stdout=sink.out,
                    captured_stderr=sink.err,
                    log_records=tuple(sink.log_records),
                )
            results[index] = result

            if module_keys:
                pending_module_keys.setdefault(record.path, []).extend(module_keys)
            remaining_by_module[record.path] -= 1
            if remaining_by_module[record.path] == 0:
                keys = pending_module_keys.pop(record.path, None)
                if keys:
                    await _teardown_module_scope(
                        store, keys, path=record.path, real_stderr=capture_setup.real_stderr
                    )

    # Constructed synchronously, outside the loop — `concurrent.futures.ThreadPoolExecutor.
    # __init__` needs no running loop, and creating it here (rather than inside `run_all`) is
    # what lets the `finally` below shut it down *directly*, without going through the loop at
    # all (see that comment for why that matters).
    executor = _capture.ContextPropagatingExecutor()

    async def run_all() -> None:
        # spec/09 §3: installed once, for the whole run. `set_default_executor` itself needs a
        # running loop (hence called from in here, not from `run_suite`'s own sync body), but the
        # executor object it installs was already constructed above.
        asyncio.get_running_loop().set_default_executor(executor)
        semaphore = asyncio.Semaphore(concurrency)
        async with asyncio.TaskGroup() as tg:
            for index, record in enumerate(records):
                tg.create_task(dispatch_one(index, record, semaphore))

    # Not `with asyncio.Runner() as runner:` — `runner.close()` is called explicitly below, inside
    # its own narrow guard, for a reason worth stating up front: `Runner.close()` (what the `with`
    # form would call automatically) does its own automatic `loop.run_until_complete(loop.
    # shutdown_default_executor(...))`, and — verified against CPython 3.13's
    # `asyncio.runners`/`asyncio.base_events` — that call can raise `RuntimeError: Event loop
    # stopped before Future completed.` whenever a *custom* default executor was ever installed
    # (via `set_default_executor`, exactly what `run_all` does above) *and* the loop's most recent
    # `run_until_complete` propagated an uncaught `KeyboardInterrupt`/`SystemExit` rather than
    # returning normally. This reproduces even after this function's own `executor.shutdown(...)`
    # below has already run and even after explicitly resetting the loop's default executor to
    # `None` first — it is the loop's own internal bookkeeping tripping over itself following an
    # exception-driven exit, not a real leak (the executor's actual worker threads are already
    # stopped by `executor.shutdown(...)` regardless of whether `Runner.close()`'s own redundant
    # attempt afterward succeeds or raises) and not something either this module or the stdlib
    # documents a clean way to avoid triggering in the first place. Swallowed narrowly, at the one
    # call site that can raise it, rather than worked around by never installing a custom executor
    # at all — spec/09 §3's context-propagating executor is the actual feature.
    runner = asyncio.Runner()
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
            # `wait=False`: matches the "stop now, some things leak" trade-off this codebase
            # already accepts at every other interrupt boundary (`_di.py`'s `aclose`/`setup`
            # docstrings) — blocking here to join worker threads would turn a Ctrl-C into a hang
            # if one of them is stuck. `cancel_futures=True` drops whatever was still queued
            # (nothing dispatched here ever *needs* to finish once the run is over — every use of
            # this executor is `await`ed by the test that submitted it before its own envelope
            # ends). Threads already running finish on their own time, unobserved; harmless, since
            # each `run_suite` call gets its own fresh `executor` instance rather than sharing one
            # across calls (I1).
            executor.shutdown(wait=False, cancel_futures=True)
    finally:
        # Everything below must run whether `runner.run(run_all())` returned normally, raised a
        # real `KeyboardInterrupt`/`SystemExit`, or anything else — same "no state survives one
        # `run_suite` call" contract (I1) `_capture.install`/`.uninstall` are documented to keep.
        #
        # See this function's own comment above `runner = asyncio.Runner()` for exactly which call
        # inside `close()` this guards and why it's safe to swallow unconditionally here: by this
        # point this function's own cleanup (`store.aclose()`, `executor.shutdown(...)` just above)
        # has already done everything a caller could observe the *effects* of; nothing past this
        # point in `run_suite` depends on `close()` itself having fully succeeded, and the
        # exception this guards against is not new information about a real failure — see the
        # comment above for the verification. Anything raised from `runner.run(run_all())` itself
        # (a real `KeyboardInterrupt`/`SystemExit`, or an internal `_run_one` contract violation)
        # already had its own chance to propagate from the `try` above and is unaffected by this
        # guard, which only ever sees exceptions `close()` itself raises.
        # Review (must fix): the documented quirk is real -- I reproduced it -- but the guard is
        # scoped to the *rarest* of the interrupt cases, and on the common ones `close()` raises
        # something this does not suppress, which then skips the two statements below it in this
        # same `finally`. Instrumented `asyncio.Runner.close` around the real `run_suite`:
        #   all tests pass ..................... close() returned normally
        #   test uses the default executor ..... close() returned normally
        #   ONE test, raises KeyboardInterrupt . close() raised RuntimeError('Event loop stopped
        #                                        before Future completed.')      <-- the guarded one
        #   KI + two slow siblings ............. close() raised KeyboardInterrupt
        #   KI after executor use, + sibling ... close() raised KeyboardInterrupt
        #   SystemExit(7) + a slow sibling ..... close() raised SystemExit(7)
        # The mechanism for the bottom three: `close()` calls `_cancel_all_tasks(loop)`, which
        # `run_until_complete(gather(...))`s the still-pending sibling tasks; the pending `run_all`
        # task resumes into `TaskGroup._aexit`'s `raise self._base_error`, and `Task.__step`
        # re-raises `KeyboardInterrupt`/`SystemExit` bare rather than storing them, so the
        # interrupt escapes `close()` itself. It never reaches `shutdown_default_executor`, which
        # is why the `RuntimeError` only appears when there are no siblings left to cancel.
        # The consequence is the bug: that escaping interrupt propagates out of this `finally`
        # block, so `unattributed_output.extend(...)` and -- far more importantly --
        # `_capture.uninstall()` below **never run**. Verified end to end with one interrupting
        # test and two sleeping siblings at `concurrency=4`:
        #     run_suite raised KeyboardInterrupt
        #     _capture.installed() is None?  False
        #     sys.stdout restored?           False   (type Router)
        #     root log handlers 0 -> 1
        # and the next `print()` in that process disappeared. For the CLI that means Ctrl-C during
        # a real (multi-test) run leaves `sys.stderr` a `Router` too, so even the interpreter's own
        # `KeyboardInterrupt` traceback is swallowed and the user gets a silent exit. That directly
        # falsifies this function's own docstring ("torn down in a `finally` so nothing survives a
        # `run_suite` call that raises (I1)") on exactly the path it names, and it is the same
        # class of leak as the partial-`install()` one flagged in `_capture.install`.
        # Fix: put the restore work in its own `finally` that cannot be skipped by `close()` --
        # e.g. wrap `runner.close()` in an inner `try/finally`, or move the `uninstall()` pair
        # above it, since nothing about reading the session sink or restoring the streams depends
        # on the loop being closed. Suppressing `RuntimeError` here is fine and stays fine (I
        # checked the other `RuntimeError` sources inside `close()`: `shutdown_asyncgens` and
        # `_cancel_all_tasks` both route task exceptions to `loop.call_exception_handler` rather
        # than raising, so this really is narrow); the problem is only what it *doesn't* catch.
        with contextlib.suppress(RuntimeError):
            runner.close()

        # Read before `uninstall()` clears the module-global `_capture._installed` — the `Sink`
        # object itself is unaffected either way (we're holding our own reference via
        # `capture_setup`), but reading it first keeps this in the same order as everything else
        # in this `finally`: undo what `install()` did, last.
        if unattributed_output is not None:
            unattributed_output.extend(_capture.unattributed_sections(capture_setup.session_sink))
        _capture.uninstall()
    # Safe: every `dispatch_one` task unconditionally sets `results[index]` as its very first
    # action once the semaphore admits it and `_run_one` returns — an index surviving as `None`
    # here would mean some task exited `dispatch_one` without reaching that line, which can only
    # happen if it raised. `_run_one`'s own contract is that it never raises anything but
    # `KeyboardInterrupt`/`SystemExit` (every other exception, including `asyncio.CancelledError`
    # from any source, is caught and folded into a `TestResult` — see its docstring); relied on
    # here, not asserted anywhere. Given that contract, the only thing a `dispatch_one` task can
    # raise is one of those two, or the `asyncio.CancelledError` `TaskGroup` throws into every
    # *other* sibling once one of them does — and both cases make `runner.run(run_all())` above
    # raise in turn (a bare `KeyboardInterrupt`/`SystemExit`, per `TaskGroup`'s own special-casing
    # of exactly those two types — see this function's docstring), which exits `run_suite` entirely
    # before this line is ever reached. So a `None` surviving to here would mean `_run_one`'s own
    # contract was violated, not a gap in this function's exception handling.
    return cast(list[TestResult], results)


async def _teardown_module_scope(
    store: _di.ScopeStore, keys: list[_di.CacheKey], *, path: Path, real_stderr: TextIO
) -> None:
    """Best-effort release of one module's accumulated fixture keys, once its last test finishes.

    Runs *inside* the loop (called from a `dispatch_one` task), unlike `_teardown_best_effort`
    below, which is for the two call sites still outside it (`store.aclose()` from `run_suite`'s
    own synchronous body). Same swallow-and-report policy as that one — see its docstring for why
    this isn't attributed to any one `TestResult` or turned into a nonzero exit code yet, and for
    why `real_stderr` (not `sys.stderr`) is where this prints.
    """
    try:
        await _di.teardown(store, keys)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        print(f"velox: error tearing down module-scope fixtures ({path}):", file=real_stderr)
        traceback.print_exc(file=real_stderr)


def _teardown_best_effort(
    runner: asyncio.Runner, coro: Coroutine[Any, Any, None], what: str, *, real_stderr: TextIO
) -> None:
    """Run one end-of-scope teardown `coro` to completion from *outside* the loop (via
    `runner.run`), swallowing everything except `KeyboardInterrupt`/`SystemExit` — see
    `run_suite`'s "Known, deliberate gap" for why this prints to stderr instead of failing the run
    or attributing the error to any one `TestResult`. `_teardown_module_scope` above is the sibling
    for teardown triggered *inside* the loop, which must `await` directly rather than re-enter the
    runner.

    Prints to `real_stderr` — the stream `_capture.install()` captured *before* replacing
    `sys.stderr` with a `Router` — rather than `sys.stderr` itself: by the time this runs,
    `sys.stderr` *is* a `Router`, and this call happens from `run_suite`'s own synchronous body,
    outside any `dispatch_one` task, so a plain `print(..., file=sys.stderr)` here would be
    attributed to the session sink (module docstring's own "end-of-run session-scope teardown"
    example) and only ever surface in the unattributed-output section — several screens away from
    where a user watching stderr live would see it. This message is velox's own internal
    diagnostic, not test output to attribute at all, so it bypasses the Router entirely and always
    reaches the real stream immediately.
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
    """The current subset of spec/02 §4's exit code table.

    - `5` — nothing was collected at all (no records, no errors, no skips either — an empty
      selection). `skipped` defaults to `0` so callers that predate `_collect.Skipped` keep
      their existing behavior unchanged.
    - `1` — at least one collection error, or at least one `FAILED`/`ERROR`/`TIMEOUT` result
      (spec/05 §4's table gives all three the same exit-code contribution).
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
    if errors or any(result.outcome in _FAILING_OUTCOMES for result in results):
        return 1
    return 0
