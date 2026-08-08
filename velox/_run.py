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
    #: blocking call) is different from either, and it's near-free to detect with `asyncio.timeout`.
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
    is also what keeps that release from ever racing a concurrent `acquire()` on the same key.
    `function`/`call`/`session`-scope keys are released here, per test, same as always; a
    `session` key's `release` is always a harmless refcount-only decrement (real teardown is
    `aclose`'s job), so leaving it in the "release now" bucket changes nothing observable.

    `timeout` (`None` means no limit) wraps `setup()` and the test's own `call` together in one
    `asyncio.timeout` budget, matching spec/05 §2's envelope sketch — teardown itself is *not*
    time-boxed this slice (spec/05 §6's shielded, time-boxed teardown grace is a separate,
    not-yet-built cancellation-choreography session); it always gets a chance to run to completion
    once setup has actually acquired something, timeout or not.

    Aggregation rule (spec/05 §3-4's phase/outcome tables, made concrete):

    - The whole setup+call envelope times out: outcome is `TIMEOUT`. Whatever `_di.setup` itself
      had acquired before the deadline hit was already released by its own internal cleanup (see
      its docstring) — `keys` never gets assigned in that case (see below) — so there is nothing
      left for *this* function to tear down in that specific case. If the timeout instead lands
      during `call` (setup already succeeded), teardown still runs for what setup acquired.
    - Setup raises (not a timeout): outcome is `ERROR`, using setup's traceback. The call phase
      never runs; nothing is left to tear down (same reasoning as above — `_di.setup` cleans up
      its own partial acquisitions before raising).
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
       actually observes it.
    2. **Anything else** — the test cancelling its own task directly (`_cancels_itself`-style,
       predating concurrency: M0/M1's sequential runner already had to handle a test doing this,
       since nothing about a task cancelling itself ever required real concurrency to reach), or
       this task getting cross-task-cancelled by `run_suite`'s `TaskGroup` because a *sibling*
       raised `KeyboardInterrupt`/`SystemExit`. Both reach the outer `except asyncio.CancelledError`
       below, once `asyncio.timeout` has already had its chance and declined (its `__aexit__`
       leaves a non-`TimeoutError` `CancelledError` completely alone when the deadline wasn't its
       own). Folding *this* task's collateral cancellation into an ordinary `ERROR`/`FAILED`
       outcome instead of re-raising is safe even in the sibling-interrupt case: the sibling's own
       `KeyboardInterrupt`/`SystemExit` still propagates out of *its* task unimpeded (nothing here
       ever catches those two) and still aborts `run_suite` correctly through its `except*` —
       nothing depends on *this* task also re-raising a bare `CancelledError` for that to work, and
       doing so would instead risk leaving this index's `results` slot never assigned.
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

    try:
        # `asyncio.timeout(None)` is a documented no-op (no deadline scheduled), so this is
        # unconditional rather than branching on whether `timeout` was passed.
        async with asyncio.timeout(timeout):
            try:
                # Review (must fix): this call is where `run_suite`'s "all acquires happen-before
                # the one release" invariant for `module` scope actually breaks. `_di.setup`'s own
                # `except BaseException` cleanup calls `_release_all(store, reversed(acquired))`
                # over *every* key it managed to acquire — including `module`-scope ones — and
                # `run_suite` never sees that release, so it is not ordered after anything. Any
                # partial setup failure (a later fixture raising, or this envelope's own timeout
                # cancelling mid-setup) therefore drives a `module` key's refcount to zero and
                # tears the instance down while sibling tests of the same module are still being
                # dispatched. Verified end to end, two tests sharing `path`, `concurrency=2`,
                # test_a = [module fixture, a fixture that raises], test_b = [a slow fixture,
                # the same module fixture]: `builds == 2`, `torn_down == ["closed", "closed"]`,
                # i.e. `scope="module"` silently degraded to per-test. The same shape with an
                # `async` module fixture whose teardown awaits is worse and lands squarely in
                # `ScopeStore.release`'s documented "narrow concurrent-teardown window": test_b's
                # `acquire` finds the entry still present mid-`await entry.closer()` and is handed
                # the *same* object that is being torn down — observed log "open / test_b sees
                # closed=False / close / test_b end, closed=True", with test_b reported PASSED
                # while its connection closed underneath it. So the docstring below ("`keys` never
                # gets assigned in that case ... so there is nothing left for *this* function to
                # tear down") describes the local bookkeeping correctly but draws the wrong global
                # conclusion: setup's cleanup is not a private matter once `module` keys are
                # involved, and `run_suite`'s claim that it cannot reopen that window is false.
                # Fixing it means `_di.setup` reporting back which keys it released on the failure
                # path (or not releasing non-`function` scopes itself at all and letting the
                # caller that owns the module lifetime do it), not a change here alone.
                kwargs, keys = await _di.setup(
                    record.plan, store, test_id=record.id, module_path=str(record.path)
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
    # Review (should fix): the docstring's account of `asyncio.timeout.__aexit__` is right about
    # the case it considers and silently wrong about the one it doesn't. Reading CPython 3.13's
    # `Timeout.__aexit__`: it only raises `TimeoutError` when `self._state is _State.EXPIRING and
    # self._task.uncancel() <= self._cancelling and exc_type is not None and issubclass(exc_type,
    # CancelledError)`. The `exc_type is not None` conjunct is the gap — if the deadline fires but
    # the block exits *normally*, no `TimeoutError` is produced and `timed_out` stays `False`.
    # That is reachable through the two inner handlers above: a test body that catches the
    # injected `CancelledError` and raises something else from a `finally`/`except` hands the
    # inner `except BaseException` an ordinary exception, which it records as `call_failure` and
    # swallows, so the `async with` exits cleanly. Verified: a test whose body is
    # `try: await asyncio.sleep(10) / except BaseException: raise ValueError(...)` under
    # `timeout=0.03` returns `FAILED` after 0.032s, not `TIMEOUT` — a test that genuinely blew its
    # budget is reported as an ordinary assertion-style failure, which sends the reader looking
    # for the wrong bug. The cheap fix is to consult the deadline directly rather than relying
    # solely on the exception channel (keep a reference to the `Timeout` object and check
    # `.expired()` after the block), which also covers the `uncancel() > _cancelling` case where
    # a sibling-driven cancellation arrives at the same moment as the deadline.
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
        # Review: this `except BaseException` catches `asyncio.CancelledError`, which makes the
        # teardown phase the one place in this function that handles cancellation differently from
        # the two phases above — and the docstring's long `CancelledError` section never says so.
        # The two inner phase handlers list `asyncio.CancelledError` explicitly and re-raise it;
        # this one folds it into `teardown_failure` and reports `ERROR`. Concretely: when a sibling
        # raises `KeyboardInterrupt`, the `TaskGroup` cancels this task, and if the cancellation
        # lands while an async fixture's closer is awaiting, this test is reported as a teardown
        # `ERROR` rather than being recognised as collateral cancellation — a fabricated failure
        # attributed to the user's fixture on the way out of a Ctrl-C. Worth either adding
        # `asyncio.CancelledError` to the guard above (attributing it the way the outer handler
        # does) or documenting the asymmetry as deliberate; right now the docstring reads as if
        # all three phases behave alike.
        except BaseException:
            teardown_failure = traceback.format_exc()

    duration = time.monotonic() - start

    if timed_out:
        outcome = Outcome.TIMEOUT
        failure = f"test exceeded the --timeout={timeout}s budget"
        if call_failure is not None:
            # Rare but possible: the call raised *and* the timeout's own cancellation is what
            # unwound it (e.g. the call caught and re-raised inside a `finally`). Keep both.
            failure = f"{failure}\n\n{call_failure}"
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
    together. That "all acquires happen-before the one release" ordering is also what keeps this
    from reopening `_di.ScopeStore.release`'s documented narrow concurrent-teardown window (still
    real, still deferred, see its docstring): a `release()` for a given key never overlaps an
    `acquire()` for that *same* key here, because by construction nothing will ever `acquire()` it
    again once the release fires. `session`-scope keys need no such choreography — `release()` is
    always a refcount-only no-op for them (real teardown is `aclose`'s job, strictly after every
    task has finished), so they're released the instant each test's own teardown runs, same as
    `function`/`call` scope.

    `KeyboardInterrupt`/`SystemExit`: `_run_one` re-raises both immediately out of any phase of any
    test (its own docstring). A raise inside one `TaskGroup` child cancels every sibling task and,
    once they've all unwound, `TaskGroup.__aexit__` raises an `ExceptionGroup`/`BaseExceptionGroup`
    wrapping whatever propagated — `except*` below unwraps that back to the original
    `KeyboardInterrupt`/`SystemExit` itself rather than letting a `BaseExceptionGroup` (an
    unfamiliar, differently-shaped exception `cli.main` and every caller before this slice never
    had to handle) become the thing that actually exits the process. `store.aclose()` still runs
    in a `finally` unconditionally, same as before, so best-effort session-scope teardown happens
    even on this path. Known, deliberate gap, same shape as the ones this codebase already
    documents elsewhere: a module whose tests were still in flight when the interrupt landed never
    reaches `remaining_by_module == 0` and its accumulated module-scope keys are simply not
    released here — `aclose()`'s own sweep (see its docstring) is the safety net, and it no longer
    only ever finds session-scope entries once concurrency can leave other scopes stranded there
    too; it doesn't care what scope an entry claims, it just tears down whatever's left.

    Full Ctrl-C/`--maxfail` cancellation choreography (an `interrupted` outcome, shielded
    time-boxed teardown grace, salvaging partial results) is spec/05 §6, not built this slice —
    same as the M0/M1-DI sequential runner, an interrupt here still aborts the whole call with
    nothing returned to `cli.main` rather than a partial `list[TestResult]`.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")

    results: list[TestResult | None] = [None] * len(records)
    store = _di.ScopeStore()

    # Review (must fix): the docstring's central claim about this mechanism — "all acquires
    # happen-before the one release ... a `release()` for a given key never overlaps an
    # `acquire()` for that *same* key here" — is false, and the counterexample is in `_run_one`
    # (see the long note above its `_di.setup` call). The counting here is sound for the keys it
    # actually owns; what it misses is that `_di.setup` performs its *own* releases on the
    # partial-failure path, out of band, for keys this dict has already counted but never
    # received. So `remaining_by_module` guarantees "the last release `run_suite` issues comes
    # after every acquire", not "the last release *of that key* comes after every acquire", and
    # only the second is strong enough to keep `ScopeStore.release`'s window shut. Reproduced:
    # a module fixture built and torn down twice, and a sibling handed a mid-teardown instance.
    #
    # Review (good, worth stating): unlike the boundary-based code this replaces, nothing here
    # depends on `_collect.collect` emitting a module's records contiguously — the count is taken
    # over the whole list up front and decremented from whichever task happens to finish. That
    # dependency was dropped rather than silently relied on, and it is the right call (it is what
    # will let a future `--seed`/aging scheduler reorder `records` freely). It is worth saying so
    # in the docstring, because `collect` *does* still produce contiguous records today, so a
    # future edit could reintroduce the assumption without any test noticing.
    remaining_by_module: dict[Path, int] = {}
    for record in records:
        remaining_by_module[record.path] = remaining_by_module.get(record.path, 0) + 1
    pending_module_keys: dict[Path, list[_di.CacheKey]] = {}

    async def dispatch_one(index: int, record: TestRecord, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            result, module_keys = await _run_one(record, store, timeout=timeout)
        results[index] = result

        if module_keys:
            pending_module_keys.setdefault(record.path, []).extend(module_keys)
        remaining_by_module[record.path] -= 1
        if remaining_by_module[record.path] == 0:
            keys = pending_module_keys.pop(record.path, None)
            if keys:
                # Review (should fix): this `await` is *outside* the `async with semaphore` block,
                # so module-scope teardown is not admitted through the concurrency gate at all.
                # Two consequences, neither documented. (1) `run_suite`'s docstring says
                # `concurrency=1` "fully serializes and reproduces the old M0/M1-DI behavior" —
                # it does not. Verified with two single-test modules at `concurrency=1`, where
                # module A's fixture teardown awaits 0.05s and test B sleeps 0.02s: the observed
                # log is "test_a, teardown-A start, test_b start, test_b end, teardown-A end".
                # A user picking `--concurrency=1` specifically to debug an ordering problem still
                # gets a fixture teardown running concurrently with the next test. (2) `--timeout`
                # aside, the semaphore no longer bounds "tests inside their setup/call/teardown
                # envelope" the way both the docstring and `_run_one`'s docstring claim: when many
                # modules finish at once, arbitrarily many module teardowns can be in flight on top
                # of `concurrency` running tests. Moving the release inside the `async with` (or
                # taking the semaphore again around it) fixes both; if it is deliberate, the
                # `concurrency=1` sentence needs to be narrowed to the call phase.
                await _teardown_module_scope(store, keys, path=record.path)

    async def run_all() -> None:
        semaphore = asyncio.Semaphore(concurrency)
        async with asyncio.TaskGroup() as tg:
            # Review (low, latent): the slot a result is written to comes from `enumerate` here,
            # but the `index` a `TestResult` *reports* comes from `record.index`, assigned by
            # `_collect.collect` across the whole concatenation of files. They agree today only
            # because `records` is always the complete, unfiltered collection. The first selection
            # feature that hands `run_suite` a subset (`-k`/`-m`, spec/02) makes
            # `results[i].index != i` with nothing asserting either way — and `run_suite`'s own
            # docstring calls the returned order "index order", which would then be true of the
            # positions and false of the field. Worth deciding now which one `TestResult.index`
            # means: position in this run, or stable collection id.
            for index, record in enumerate(records):
                tg.create_task(dispatch_one(index, record, semaphore))

    with asyncio.Runner() as runner:
        try:
            try:
                runner.run(run_all())
            # Review (documentation is wrong; code is inert but harmless): `run_suite`'s docstring
            # says "`TaskGroup.__aexit__` raises an `ExceptionGroup`/`BaseExceptionGroup` wrapping
            # whatever propagated — `except*` below unwraps that back to the original". That is not
            # what `asyncio.TaskGroup` does. It special-cases exactly these two types:
            # `_on_task_done` sets `self._base_error` for the first `KeyboardInterrupt`/`SystemExit`
            # it sees, and `_aexit` does a bare `raise self._base_error` *before* it ever constructs
            # the group. Verified directly against a raw `TaskGroup`: a child raising
            # `KeyboardInterrupt` among sleeping siblings propagates a bare `KeyboardInterrupt`,
            # `isinstance(..., BaseExceptionGroup)` is `False`. So `except*` here only ever matches
            # because `except*` implicitly wraps a bare exception for the handler, and
            # `_first_interrupt` then returns the very object that was already propagating — a round
            # trip. Delete the whole `try`/`except*` and every current behaviour is unchanged (that
            # is also why the new sibling test proves nothing; see the note on it in
            # `tests/test_run.py`). Not a bug, but this is load-bearing-looking code whose
            # justification does not describe the runtime, which is how the *next* person
            # "simplifies" the wrong half. The one case that would genuinely need `_first_interrupt`
            # is a test raising a `BaseExceptionGroup` that *contains* a `KeyboardInterrupt`: that
            # is not a base error by `TaskGroup._is_base_error`, so it does land in a real group.
            #
            # Review (should fix, I8): `from None` sets `__suppress_context__`, so when two siblings
            # interrupt at once the second is erased from the traceback. Verified: one test raising
            # `KeyboardInterrupt` and another raising `SystemExit(7)` under `concurrency=2` yields a
            # bare `KeyboardInterrupt`; the `SystemExit`, and with it the exit code 7 the user asked
            # for, survives only on `__context__` with display suppressed. Sequential M0/M1 could
            # not reach this at all. At minimum drop `from None` so the loser is still visible.
            except* (KeyboardInterrupt, SystemExit) as eg:
                raise _first_interrupt(eg) from None
        finally:
            _teardown_best_effort(runner, store.aclose(), what="session-scope fixtures")
    # Safe: `run_all` only returns normally once every `dispatch_one` task has completed, and each
    # one unconditionally sets `results[index]` as its first action after the semaphore block — an
    # index surviving as `None` here would mean a task exited without doing that, which can only
    # happen via the `except*` above, which re-raises instead of reaching this line.
    #
    # Review (conclusion holds, stated reason does not): I tried to break this and could not, so
    # the `cast` is safe today — but "which can only happen via the `except*` above" is the wrong
    # justification and would stop protecting anything under an edit. There are two other ways a
    # task can exit without assigning its slot, and neither goes through that `except*`:
    # (a) cancellation while parked on `semaphore.acquire()`, before `_run_one` is ever entered —
    #     the `async with semaphore` raises and `results[index] = result` is skipped;
    # (b) any non-`KeyboardInterrupt`/`SystemExit` exception escaping `dispatch_one`.
    # Both are currently unreachable-or-harmless only because the `TaskGroup` re-raises in every
    # case (a plain `Exception` becomes an `ExceptionGroup` that the `except*` deliberately does
    # *not* catch, so it still propagates past this line). The real invariant is "any task that
    # fails to set its slot also makes `run_all()` raise", which is a property of `TaskGroup`, not
    # of the `except*`. Worth restating that way — and worth noting `_run_one`'s contract "never
    # raises anything but `KeyboardInterrupt`/`SystemExit`" is what (b) rests on and is not
    # asserted anywhere.
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


def _first_interrupt(eg: BaseExceptionGroup[BaseException]) -> BaseException:
    """The first `KeyboardInterrupt`/`SystemExit` found in `eg`, recursing into nested groups.

    `except*` has already filtered `eg` down to only the branches that matched
    `(KeyboardInterrupt, SystemExit)` (`run_suite`'s `except*` clause), so this always finds one —
    the fallback `RuntimeError` only exists so a future refactor that changes what `except*` clause
    calls this can't turn "no match" into a silent `IndexError`/`StopIteration` instead of a loud,
    diagnosable failure (I8).
    """
    for exc in eg.exceptions:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            return exc
        if isinstance(exc, BaseExceptionGroup):
            try:
                return _first_interrupt(exc)
            except RuntimeError:
                continue
    # Review (low): three things about this fallback. It is dead code twice over — `except*` filters
    # the group to matching leaves, *and* (see the note at the call site) `TaskGroup` never hands a
    # group containing an interrupt to this function in the first place. Its docstring's rationale
    # ("so a future refactor ... can't turn 'no match' into a silent `IndexError`/`StopIteration`")
    # is sound, but the implementation undercuts it on the point the brief cares about: raising a
    # bare `RuntimeError` with no `from eg` discards the actual exceptions from the `__cause__`
    # chain, which is exactly the swallowing I8 exists to prevent. `raise RuntimeError(...) from eg`
    # costs nothing and keeps them. Third, and the reason this is more than cosmetic: this function
    # is annotated `-> BaseException` but can *raise* instead of returning, and its only caller is
    # `raise _first_interrupt(eg) from None` — a caller that reads as though a value always comes
    # back. If it ever did fire, the `RuntimeError` would surface from inside an `except*` handler
    # during a Ctrl-C, which is the least debuggable moment available.
    raise RuntimeError("no KeyboardInterrupt/SystemExit found in an interrupt-only exception group")


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
