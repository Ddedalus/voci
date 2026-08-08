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
import enum
import math
import sys
import time
import traceback
from collections.abc import Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from velox import _di
from velox._collect import CollectionError, TestRecord

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
) -> list[TestResult]:
    """Run every record concurrently on one `asyncio.Runner`, semaphore-bounded (spec/05 §1).

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
            result, module_keys = await _run_one(record, store, timeout=timeout)
            results[index] = result

            if module_keys:
                pending_module_keys.setdefault(record.path, []).extend(module_keys)
            remaining_by_module[record.path] -= 1
            if remaining_by_module[record.path] == 0:
                keys = pending_module_keys.pop(record.path, None)
                if keys:
                    await _teardown_module_scope(store, keys, path=record.path)

    async def run_all() -> None:
        semaphore = asyncio.Semaphore(concurrency)
        async with asyncio.TaskGroup() as tg:
            for index, record in enumerate(records):
                tg.create_task(dispatch_one(index, record, semaphore))

    with asyncio.Runner() as runner:
        try:
            runner.run(run_all())
        finally:
            _teardown_best_effort(runner, store.aclose(), what="session-scope fixtures")
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
    store: _di.ScopeStore, keys: list[_di.CacheKey], *, path: Path
) -> None:
    """Best-effort release of one module's accumulated fixture keys, once its last test finishes.

    Runs *inside* the loop (called from a `dispatch_one` task), unlike `_teardown_best_effort`
    below, which is for the two call sites still outside it (`store.aclose()` from `run_suite`'s
    own synchronous body). Same swallow-and-report policy as that one — see its docstring for why
    this isn't attributed to any one `TestResult` or turned into a nonzero exit code yet.
    """
    try:
        await _di.teardown(store, keys)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        print(f"velox: error tearing down module-scope fixtures ({path}):", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)


def _teardown_best_effort(
    runner: asyncio.Runner, coro: Coroutine[Any, Any, None], what: str
) -> None:
    """Run one end-of-scope teardown `coro` to completion from *outside* the loop (via
    `runner.run`), swallowing everything except `KeyboardInterrupt`/`SystemExit` — see
    `run_suite`'s "Known, deliberate gap" for why this prints to stderr instead of failing the run
    or attributing the error to any one `TestResult`. `_teardown_module_scope` above is the sibling
    for teardown triggered *inside* the loop, which must `await` directly rather than re-enter the
    runner.
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
    failing = (Outcome.FAILED, Outcome.ERROR, Outcome.TIMEOUT)
    if errors or any(result.outcome in failing for result in results):
        return 1
    return 0
