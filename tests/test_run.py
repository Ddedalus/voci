"""Regression tests for velox._run (spec/05)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
import velox
from velox._collect import CollectionError
from velox._collect import TestRecord as Record  # `Test*` makes pytest try to collect it itself
from velox._fixtures import ResolutionPlan, plan_for
from velox._run import Outcome, exit_code_for, run_suite
from velox._run import TestResult as Result  # same reason

#: A test with no `Depends(...)` at all still needs a plan (`_collect.py` gives every `TestRecord`
#: one, uniformly) — this is the trivial one, shared by every test below that doesn't care about
#: DI at all.
_EMPTY_PLAN = ResolutionPlan(steps=(), root_args=())


def _record(
    index: int,
    func: Callable[..., object],
    qualname: str,
    plan: ResolutionPlan = _EMPTY_PLAN,
    path: Path = Path("mod.py"),
) -> Record:
    return Record(
        id=f"{path}::{qualname}",
        index=index,
        path=path,
        lineno=1,
        qualname=qualname,
        func=func,
        plan=plan,
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


# ------------------------------------------------------------------------------------------
# Setup -> call -> teardown, driven by real `ResolutionPlan`s (spec/04, spec/05 §3). M0 covered
# only a bare zero-argument call; these exercise the DI wiring `run_suite` now drives.
# ------------------------------------------------------------------------------------------


def test_function_scope_fixture_is_injected_with_a_working_value() -> None:
    @velox.fixture()
    def answer() -> int:
        return 42

    async def test_func(x: int = velox.Depends(answer)) -> None:
        assert x == 42

    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert result.outcome is Outcome.PASSED
    assert result.failure is None


def test_session_scope_fixture_is_shared_and_built_exactly_once_across_tests() -> None:
    """Two independently-`plan_for`-built plans referencing the same `Fixture` object still
    resolve to the same session cache key (`_di.key_for` keys session scope by `id(fixture)`,
    not by which test's plan asked for it) — so the second test observes the first's instance
    rather than triggering a second construction."""
    builds: list[int] = []

    @velox.fixture(scope="session")
    def counted() -> int:
        builds.append(1)
        return len(builds)

    async def test_a(x: int = velox.Depends(counted)) -> None:
        assert x == 1

    async def test_b(x: int = velox.Depends(counted)) -> None:
        assert x == 1  # same shared instance, not rebuilt for this test

    results = run_suite(
        [
            _record(0, test_a, "test_a", plan=plan_for(test_a)),
            _record(1, test_b, "test_b", plan=plan_for(test_b)),
        ]
    )

    assert [r.outcome for r in results] == [Outcome.PASSED, Outcome.PASSED]
    assert len(builds) == 1


def test_module_scope_fixture_is_shared_and_torn_down_once_across_tests_in_one_module() -> None:
    """The `module`-scope mirror of the `session` test above: `_run_one` holds `module`-scope
    keys open instead of releasing them per test, and `run_suite` releases them together once it
    reaches the last test sharing that `path` — proven here by both a build count and a teardown
    count of exactly one across two tests, not two of each."""
    builds: list[int] = []
    torn_down: list[str] = []

    @velox.fixture(scope="module")
    def per_module():
        builds.append(1)
        yield len(builds)
        torn_down.append("closed")

    async def test_a(x: int = velox.Depends(per_module)) -> None:
        assert x == 1

    async def test_b(x: int = velox.Depends(per_module)) -> None:
        assert x == 1  # same module-scope instance, not rebuilt for this test

    results = run_suite(
        [
            _record(0, test_a, "test_a", plan=plan_for(test_a)),
            _record(1, test_b, "test_b", plan=plan_for(test_b)),
        ]
    )

    assert [r.outcome for r in results] == [Outcome.PASSED, Outcome.PASSED]
    # One build, one teardown -- not two of each. Under the bug this fixes (each test's own
    # teardown releasing its `module`-scope keys immediately), `test_b` would rebuild `per_module`
    # from scratch (`builds == [1, 1]`) and it would tear down twice (`torn_down == ["closed",
    # "closed"]"), since both tests share `path="mod.py"` by `_record`'s default and nothing in
    # this suite ever holds it open across the boundary between them.
    assert len(builds) == 1
    assert torn_down == ["closed"]


def test_module_scope_fixture_is_not_shared_across_different_modules() -> None:
    """The `module_path` half of `_di.key_for`'s cache key, exercised for the first time at this
    level: two tests in *different* files must each get their own instance."""
    builds: list[int] = []

    @velox.fixture(scope="module")
    def per_module() -> int:
        builds.append(1)
        return len(builds)

    async def test_a(x: int = velox.Depends(per_module)) -> None:
        pass

    async def test_b(x: int = velox.Depends(per_module)) -> None:
        pass

    results = run_suite(
        [
            _record(0, test_a, "test_a", plan=plan_for(test_a), path=Path("mod_a.py")),
            _record(1, test_b, "test_b", plan=plan_for(test_b), path=Path("mod_b.py")),
        ]
    )

    assert [r.outcome for r in results] == [Outcome.PASSED, Outcome.PASSED]
    assert len(builds) == 2


def test_session_scope_fixture_is_torn_down_at_end_of_run() -> None:
    """`store.aclose()` after the loop, per spec/04 §3's "end of the run" — proven by observing
    the generator's teardown side effect only after `run_suite` has returned."""
    torn_down: list[str] = []

    @velox.fixture(scope="session")
    def db():
        yield "db"
        torn_down.append("db")

    async def test_func(x: str = velox.Depends(db)) -> None:
        assert x == "db"

    run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert torn_down == ["db"]


def test_fixture_setup_failure_produces_error_not_failed() -> None:
    @velox.fixture()
    def broken() -> int:
        raise RuntimeError("setup boom")

    async def test_func(x: int = velox.Depends(broken)) -> None:
        raise AssertionError("must never run: setup already failed")

    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert result.outcome is Outcome.ERROR
    assert result.failure is not None
    assert "setup boom" in result.failure


def test_fixture_teardown_failure_after_a_passing_call_produces_error() -> None:
    """spec/05 §3's phase table: a teardown failure surfaces as `error` "even if call passed" —
    the aggregated outcome must not stay `PASSED` just because the call phase itself was clean."""

    @velox.fixture()
    def flaky_teardown():
        yield 1
        raise RuntimeError("teardown boom")

    async def test_func(x: int = velox.Depends(flaky_teardown)) -> None:
        assert x == 1  # the call phase genuinely passes

    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert result.outcome is Outcome.ERROR
    assert result.failure is not None
    assert "teardown boom" in result.failure


def test_call_and_teardown_both_failing_still_reports_error_with_both_tracebacks() -> None:
    @velox.fixture()
    def flaky_teardown():
        yield 1
        raise RuntimeError("teardown boom")

    async def test_func(x: int = velox.Depends(flaky_teardown)) -> None:
        raise AssertionError("call boom")

    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert result.outcome is Outcome.ERROR
    assert result.failure is not None
    assert "call boom" in result.failure
    assert "teardown boom" in result.failure


# ------------------------------------------------------------------------------------------
# KeyboardInterrupt/SystemExit from every phase, including from inside a fixture's teardown —
# the `except (KeyboardInterrupt, SystemExit): raise` guards `_run_one` carries around setup,
# call, and teardown; and `run_suite`'s own best-effort session-scope teardown in `finally`.
# ------------------------------------------------------------------------------------------


def test_keyboard_interrupt_from_a_fixture_setup_propagates_immediately() -> None:
    @velox.fixture()
    def broken():
        raise KeyboardInterrupt

    async def test_func(x: int = velox.Depends(broken)) -> None:
        pass

    with pytest.raises(KeyboardInterrupt):
        run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])


def test_keyboard_interrupt_from_a_fixture_teardown_propagates_immediately() -> None:
    """`_di._release_all` (which both `_di.setup`'s cleanup and `_di.teardown` funnel through)
    re-raises `KeyboardInterrupt`/`SystemExit` immediately instead of folding them into its
    `BaseExceptionGroup` — without that, this would surface as `ERROR` for the test and a run
    that keeps going, not a `KeyboardInterrupt` propagating out of `run_suite`."""

    @velox.fixture()
    def flaky():
        yield 1
        raise KeyboardInterrupt

    async def test_func(x: int = velox.Depends(flaky)) -> None:
        pass

    with pytest.raises(KeyboardInterrupt):
        run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])


def test_system_exit_from_a_fixture_teardown_propagates_immediately() -> None:
    @velox.fixture()
    def flaky():
        yield 1
        raise SystemExit(1)

    async def test_func(x: int = velox.Depends(flaky)) -> None:
        pass

    with pytest.raises(SystemExit):
        run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])


def test_session_scope_fixture_is_still_torn_down_after_a_keyboard_interrupt_mid_call() -> None:
    """`store.aclose()` runs in a `finally` around the whole loop, so a `KeyboardInterrupt` raised
    by the test body itself (not a fixture) still gets best-effort session-scope teardown on the
    way out, rather than leaking the fixture — the bug `run_suite`'s docstring used to accept as
    a known gap and now fixes."""
    torn_down: list[str] = []

    @velox.fixture(scope="session")
    def db():
        yield "db"
        torn_down.append("db")

    async def test_func(x: str = velox.Depends(db)) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert torn_down == ["db"]


def test_session_scope_teardown_failure_is_reported_to_stderr_and_does_not_fail_the_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pins `run_suite`'s documented "known, deliberate gap": a session-scope teardown failure
    reaches stderr, does not touch the already-`PASSED` test's own outcome, and does not turn the
    run's exit code nonzero on its own — there is no `TestResult` to attribute it to yet."""

    @velox.fixture(scope="session")
    def flaky_session():
        yield 1
        raise RuntimeError("session teardown boom")

    async def test_func(x: int = velox.Depends(flaky_session)) -> None:
        assert x == 1

    results = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])

    assert [r.outcome for r in results] == [Outcome.PASSED]
    assert exit_code_for(results, []) == 0
    assert "session teardown boom" in capsys.readouterr().err


# ------------------------------------------------------------------------------------------
# Concurrency (spec/05 §1): tests dispatch as concurrent `asyncio.Task`s under a shared
# `asyncio.Semaphore(concurrency)`, inside one `asyncio.TaskGroup`.
# ------------------------------------------------------------------------------------------


def test_concurrency_bounds_the_number_of_tests_in_flight_at_once() -> None:
    """The semaphore genuinely bounds how many tests are inside their call phase at once, not
    just how many `asyncio.Task`s exist -- proven by tracking the actual concurrent-entry count
    from inside the test body itself and asserting its peak. `> 1` also rules out the semaphore
    accidentally serializing everything (which `peak <= concurrency` alone would not catch: a
    fully serial run also satisfies `peak <= 4`). Every record here uses `_EMPTY_PLAN`, so this
    exercises only the call phase -- `test_concurrency_bounds_module_scope_teardown_too` below is
    the sibling that pins the same bound for module-scope teardown, the phase that used to escape
    it entirely (it is awaited outside the semaphore) until that was fixed."""
    in_flight = 0
    peak = 0

    async def _test() -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1

    records = [_record(i, _test, f"test_{i}") for i in range(20)]

    results = run_suite(records, concurrency=4)

    assert [r.outcome for r in results] == [Outcome.PASSED] * 20
    assert peak <= 4
    assert peak > 1


def test_concurrency_bounds_module_scope_teardown_too() -> None:
    """The phase the test above cannot see: module-scope teardown now runs *inside*
    `async with semaphore` (moved there specifically so the semaphore bounds the whole
    setup/call/teardown envelope, not just the call phase), so it must be concurrency-bounded the
    same way -- four independent single-test modules, each with an async `scope="module"` fixture
    whose own teardown awaits, at `concurrency=2`, must never show more than 2 concurrently
    in-flight teardowns."""
    in_teardown = 0
    peak_teardown = 0

    def _make_module_fixture() -> velox.Fixture[None]:
        @velox.fixture(scope="module")
        async def per_module():
            nonlocal in_teardown, peak_teardown
            yield None
            in_teardown += 1
            peak_teardown = max(peak_teardown, in_teardown)
            await asyncio.sleep(0.03)
            in_teardown -= 1

        return per_module

    records = []
    for i in range(4):
        fixture = _make_module_fixture()

        async def test_func(x: None = velox.Depends(fixture)) -> None:
            pass

        records.append(
            _record(i, test_func, f"test_{i}", plan=plan_for(test_func), path=Path(f"mod_{i}.py"))
        )

    results = run_suite(records, concurrency=2)

    assert [r.outcome for r in results] == [Outcome.PASSED] * 4
    assert peak_teardown <= 2
    assert peak_teardown > 1


def test_concurrency_one_is_exactly_serial_in_logical_order() -> None:
    """spec/06 §7: `concurrency=1` must behave as an exact serial mode -- dispatched one at a
    time, in logical (`records`/index) order -- through the *same* concurrent machinery, not a
    separate code path. A shared event log pins both "one at a time" (no interleaved starts) and
    "in logical order" (not completion order, which a bug here could still accidentally get
    right for reasons unrelated to admission order). Every record here uses `_EMPTY_PLAN`, so this
    only pins call-phase-to-call-phase serialization --
    `test_concurrency_one_serializes_module_scope_teardown_before_the_next_test_starts` below is
    the sibling that pins the same property across a module's fixture teardown, which used to be
    able to overlap the next test's call phase regardless of `concurrency=1`."""
    events: list[str] = []

    def _make(i: int) -> Callable[[], object]:
        async def test_func() -> None:
            events.append(f"start-{i}")
            await asyncio.sleep(0.01)
            events.append(f"end-{i}")

        return test_func

    records = [_record(i, _make(i), f"test_{i}") for i in range(4)]

    results = run_suite(records, concurrency=1)

    assert [r.outcome for r in results] == [Outcome.PASSED] * 4
    assert events == [
        "start-0",
        "end-0",
        "start-1",
        "end-1",
        "start-2",
        "end-2",
        "start-3",
        "end-3",
    ]


def test_concurrency_one_serializes_module_scope_teardown_before_the_next_test_starts() -> None:
    """The precondition `test_concurrency_one_is_exactly_serial_in_logical_order` relies on
    (`_EMPTY_PLAN`, so no fixtures) doesn't hold once a fixture is involved -- this pins the case
    that test cannot see: at `concurrency=1`, a module's fixture teardown (an async fixture whose
    teardown itself awaits, so it would visibly overlap the next test if it were not held inside
    the semaphore) must fully finish before the *next* test's own body starts, not merely before
    the next test's result is recorded."""
    events: list[str] = []

    @velox.fixture(scope="module")
    async def per_module():
        yield None
        events.append("teardown-a start")
        await asyncio.sleep(0.05)
        events.append("teardown-a end")

    async def test_a(x: None = velox.Depends(per_module)) -> None:
        events.append("test-a")

    async def test_b() -> None:
        events.append("test-b start")
        await asyncio.sleep(0.01)
        events.append("test-b end")

    records = [
        _record(0, test_a, "test_a", plan=plan_for(test_a), path=Path("mod_a.py")),
        _record(1, test_b, "test_b", path=Path("mod_b.py")),
    ]

    results = run_suite(records, concurrency=1)

    assert [r.outcome for r in results] == [Outcome.PASSED] * 2
    assert events == [
        "test-a",
        "teardown-a start",
        "teardown-a end",
        "test-b start",
        "test-b end",
    ]


def test_results_are_in_logical_order_even_when_completion_order_is_scrambled() -> None:
    """Physical (completion) order is no longer the same as logical order under real concurrency
    (`_run.py`'s own module docstring) -- the later-indexed, shorter-sleeping test finishes first,
    proven by recording completion order directly rather than only inferring it from the sleep
    durations, but `results` must still come back index-ordered (I2). The `finished` assertion is
    what tells this test apart from a purely sequential runner, which would produce the exact same
    `results` order for a different reason (nothing ever overlapped to reorder in the first
    place) -- without it, this test would pass unchanged even if a future change accidentally
    serialized dispatch."""
    finished: list[int] = []

    def _sleeps(index: int, seconds: float) -> Callable[[], object]:
        async def test_func() -> None:
            await asyncio.sleep(seconds)
            finished.append(index)

        return test_func

    records = [
        _record(0, _sleeps(0, 0.06), "test_slowest"),
        _record(1, _sleeps(1, 0.03), "test_middle"),
        _record(2, _sleeps(2, 0.0), "test_fastest"),
    ]

    results = run_suite(records, concurrency=3)

    assert finished == [2, 1, 0]  # completion order really was scrambled, not just re-sorted
    assert [r.index for r in results] == [0, 1, 2]
    assert [r.id for r in results] == [r.id for r in records]
    assert [r.outcome for r in results] == [Outcome.PASSED] * 3


def test_run_suite_rejects_non_positive_concurrency() -> None:
    for bad in (0, -1):
        with pytest.raises(ValueError):
            run_suite([_record(0, _passes, "test_passes")], concurrency=bad)


def test_run_suite_rejects_non_positive_or_non_finite_timeout() -> None:
    for bad in (0, -1, -0.5, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            run_suite([_record(0, _passes, "test_passes")], timeout=bad)


def test_keyboard_interrupt_among_several_concurrent_siblings_propagates_cleanly() -> None:
    """The multi-sibling variant of
    `test_keyboard_interrupt_propagates_instead_of_being_reported_as_a_failure`: when one test
    among several genuinely-concurrent siblings raises `KeyboardInterrupt`, it must come out of
    `run_suite` as a bare `KeyboardInterrupt`, not a `BaseExceptionGroup`. This pins
    `asyncio.TaskGroup`'s own real behavior (`_aexit` re-raises `self._base_error` directly rather
    than ever wrapping `KeyboardInterrupt`/`SystemExit` in a group -- see `run_suite`'s docstring)
    as the thing this codebase actually relies on now that it no longer carries its own unwrapping
    machinery for it."""

    async def _raises_keyboard_interrupt() -> None:
        raise KeyboardInterrupt

    async def _sleeps_long() -> None:
        await asyncio.sleep(10)

    records = [
        _record(0, _sleeps_long, "test_sibling_a"),
        _record(1, _raises_keyboard_interrupt, "test_interrupt"),
        _record(2, _sleeps_long, "test_sibling_b"),
    ]

    with pytest.raises(KeyboardInterrupt) as exc_info:
        run_suite(records, concurrency=3)
    assert not isinstance(exc_info.value, BaseExceptionGroup)


def test_two_siblings_interrupting_at_once_still_yields_a_bare_interrupt_not_a_group() -> None:
    """New surface under concurrency, unreachable by the sequential M0/M1 runner: two different
    siblings can genuinely raise `KeyboardInterrupt`/`SystemExit` at once. `TaskGroup._on_task_done`
    keeps whichever one it observes first as `self._base_error` -- which one wins is a genuine race
    this test cannot pin without flaking, so it only asserts the invariant that holds regardless of
    which one does: a bare interrupt of one of the two raised types, never a
    `BaseExceptionGroup`."""

    async def _raises_keyboard_interrupt() -> None:
        raise KeyboardInterrupt

    async def _raises_system_exit() -> None:
        raise SystemExit(7)

    records = [
        _record(0, _raises_keyboard_interrupt, "test_ki"),
        _record(1, _raises_system_exit, "test_se"),
    ]

    with pytest.raises((KeyboardInterrupt, SystemExit)) as exc_info:
        run_suite(records, concurrency=2)
    assert not isinstance(exc_info.value, BaseExceptionGroup)


def test_a_test_raising_its_own_group_containing_a_keyboard_interrupt_is_just_a_failure() -> None:
    """The one shape that would *not* come out of a raw `asyncio.TaskGroup` as a bare interrupt (a
    `BaseExceptionGroup` is not a `TaskGroup` "base error") never actually reaches `run_all`'s
    `TaskGroup` at all: `_run_one`'s own `except BaseException` catches it first -- a
    `BaseExceptionGroup` is not itself an instance of `KeyboardInterrupt`/`SystemExit`/
    `CancelledError`, so it never matches the re-raise guards -- and reports it as an ordinary
    `FAILED` result, same as any other exception a test body raises. Pins that this is the actual,
    only reachable behavior, now that `run_suite` no longer carries dead machinery that once
    suggested a nested-group nested case needed handling here."""

    async def _raises_a_group_containing_a_keyboard_interrupt() -> None:
        raise BaseExceptionGroup("boom", [KeyboardInterrupt()])

    (result,) = run_suite(
        [_record(0, _raises_a_group_containing_a_keyboard_interrupt, "test_group")]
    )

    assert result.outcome is Outcome.FAILED
    assert result.failure is not None
    assert "KeyboardInterrupt" in result.failure


def test_module_scope_fixture_shared_and_torn_down_once_under_real_overlap() -> None:
    """The concurrent-overlap sibling of
    `test_module_scope_fixture_is_shared_and_torn_down_once_across_tests_in_one_module`: that
    test never actually forces more than one of the module's tests to be mid-flight at once (M0's
    sequential runner and this one's concurrent dispatch both happen to produce one build and one
    teardown for it), so it can't by itself distinguish real concurrent sharing from an artifact
    of running one at a time. Here, every test sleeps inside its own body, so with
    `concurrency=5` all five are genuinely in flight together (pinned by `peak`) -- and the
    module fixture must still be built exactly once (single-flight `ScopeStore.acquire`) and torn
    down exactly once (only once `remaining_by_module` reaches zero), not once per test."""
    builds: list[int] = []
    torn_down: list[str] = []
    in_flight = 0
    peak = 0

    @velox.fixture(scope="module")
    def per_module():
        builds.append(1)
        yield len(builds)
        torn_down.append("closed")

    async def test_func(x: int = velox.Depends(per_module)) -> None:
        nonlocal in_flight, peak
        assert x == 1
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1

    records = [_record(i, test_func, f"test_{i}", plan=plan_for(test_func)) for i in range(5)]

    results = run_suite(records, concurrency=5)

    assert [r.outcome for r in results] == [Outcome.PASSED] * 5
    assert peak > 1  # genuinely overlapped, not an artifact of running one at a time
    assert len(builds) == 1
    assert torn_down == ["closed"]


def test_module_scope_survives_a_sibling_setup_failure_under_concurrency() -> None:
    """Regression test for the must-fix concurrency bug a review caught: `_di.setup`'s own
    partial-failure cleanup used to release *every* scope it had acquired, `module` included, out
    of band from `run_suite`'s own module-lifetime bookkeeping -- driving a shared module
    fixture's refcount to zero (and tearing it down) the moment one sibling's setup failed, even
    while another sibling of the same module was still mid-flight and still holding a live
    reference to it. `test_a`'s setup acquires the module fixture successfully and then fails on
    its second fixture; `test_b` (slower, so it is still mid-setup when `test_a` fails) shares the
    same module fixture and passes. Under the bug this fixes, `len(builds) == 2` and
    `torn_down == ["closed", "closed"]`; fixed, both stay at one."""
    builds: list[int] = []
    torn_down: list[str] = []

    @velox.fixture(scope="module")
    def per_module():
        builds.append(1)
        yield len(builds)
        torn_down.append("closed")

    @velox.fixture()
    def broken():
        raise RuntimeError("setup boom")

    @velox.fixture()
    async def slow():
        await asyncio.sleep(0.03)
        return "ok"

    async def test_a(m: int = velox.Depends(per_module), b: int = velox.Depends(broken)) -> None:
        raise AssertionError("must never run: setup already failed")

    async def test_b(s: str = velox.Depends(slow), m: int = velox.Depends(per_module)) -> None:
        assert m == 1

    records = [
        _record(0, test_a, "test_a", plan=plan_for(test_a), path=Path("mod.py")),
        _record(1, test_b, "test_b", plan=plan_for(test_b), path=Path("mod.py")),
    ]

    results = run_suite(records, concurrency=2)

    assert results[0].outcome is Outcome.ERROR
    assert results[1].outcome is Outcome.PASSED
    assert len(builds) == 1
    assert torn_down == ["closed"]


def test_module_scope_survives_a_sibling_timeout_mid_setup_under_concurrency() -> None:
    """The `--timeout` variant of the test above: a deadline firing mid-setup reaches the
    identical `_di.setup` cleanup path (via `asyncio.CancelledError`) as an ordinary fixture
    failure does -- same regression, different trigger."""
    builds: list[int] = []
    torn_down: list[str] = []

    @velox.fixture(scope="module")
    def per_module():
        builds.append(1)
        yield len(builds)
        torn_down.append("closed")

    @velox.fixture()
    async def hangs():
        await asyncio.sleep(10)

    @velox.fixture()
    async def slow():
        await asyncio.sleep(0.03)
        return "ok"

    async def test_a(m: int = velox.Depends(per_module), h: object = velox.Depends(hangs)) -> None:
        raise AssertionError("must never run: setup timed out")

    async def test_b(s: str = velox.Depends(slow), m: int = velox.Depends(per_module)) -> None:
        assert m == 1

    records = [
        _record(0, test_a, "test_a", plan=plan_for(test_a), path=Path("mod.py")),
        _record(1, test_b, "test_b", plan=plan_for(test_b), path=Path("mod.py")),
    ]

    results = run_suite(records, concurrency=2, timeout=0.05)

    assert results[0].outcome is Outcome.TIMEOUT
    assert results[1].outcome is Outcome.PASSED
    assert len(builds) == 1
    assert torn_down == ["closed"]


# ------------------------------------------------------------------------------------------
# `--timeout` (spec/05 §2-4): an optional `asyncio.timeout` around each test's setup+call.
# ------------------------------------------------------------------------------------------


def test_timeout_produces_timeout_outcome_with_a_failure_message() -> None:
    async def _hangs() -> None:
        await asyncio.sleep(10)

    (result,) = run_suite([_record(0, _hangs, "test_hangs")], timeout=0.05)

    assert result.outcome is Outcome.TIMEOUT
    assert result.failure is not None
    assert "timeout" in result.failure.lower()
    assert "0.05" in result.failure


def test_timeout_survives_the_body_substituting_a_different_exception() -> None:
    """Regression test for the must-fix detection gap a review caught: `asyncio.timeout`'s
    `__aexit__` only *raises* `TimeoutError` when the block exits with an exception it can still
    see -- if the test's own code catches the injected `CancelledError` and raises something else
    instead of letting it propagate, the exception channel alone would report `FAILED`, not
    `TIMEOUT`. `_run_one` cross-checks the `Timeout` object's own `.expired()` state to catch this
    too (see its docstring)."""

    async def _absorbs_the_cancellation() -> None:
        try:
            await asyncio.sleep(10)
        except BaseException:
            raise ValueError("cleanup did something else instead") from None

    (result,) = run_suite([_record(0, _absorbs_the_cancellation, "test_absorbs")], timeout=0.03)

    assert result.outcome is Outcome.TIMEOUT
    assert result.failure is not None
    assert "timeout" in result.failure.lower()
    assert "cleanup did something else instead" in result.failure


def test_timeout_clock_starts_after_the_semaphore_is_acquired_not_at_dispatch() -> None:
    """Deliberate design choice, not pinned anywhere until now: each test's `--timeout` budget is
    a fresh `asyncio.timeout` entered only once `_run_one` actually starts running (inside the
    semaphore), not a shared deadline counted from when `run_suite` was first called. At
    `concurrency=1`, four tests that each sleep 0.05s (0.2s of total queued time) but each carry a
    0.1s *per-test* budget all pass -- a queue-inclusive clock would time out every test after the
    first."""

    async def _sleeps() -> None:
        await asyncio.sleep(0.05)

    records = [_record(i, _sleeps, f"test_{i}") for i in range(4)]

    results = run_suite(records, concurrency=1, timeout=0.1)

    assert [r.outcome for r in results] == [Outcome.PASSED] * 4


def test_timeout_during_setup_produces_timeout_and_does_not_run_teardown() -> None:
    """A hanging *fixture* (setup never returns) is distinct from a hanging test body: setup
    never completed, so there is nothing for teardown to release -- `_run_one`'s `setup_done`
    gate must stay `False`, not attempt (or fake) a teardown for a fixture that was never fully
    acquired."""
    torn_down: list[str] = []

    @velox.fixture()
    async def slow_setup():
        await asyncio.sleep(10)
        yield 1
        torn_down.append("closed")  # pragma: no cover -- must never be reached

    async def test_func(x: int = velox.Depends(slow_setup)) -> None:
        raise AssertionError("must never run: setup never finished")

    (result,) = run_suite(
        [_record(0, test_func, "test_func", plan=plan_for(test_func))], timeout=0.05
    )

    assert result.outcome is Outcome.TIMEOUT
    assert torn_down == []


def test_timeout_during_call_still_runs_teardown_for_what_setup_acquired() -> None:
    """The opposite case: setup succeeds well within budget, and it's the test *body* that hangs.
    A test must not leak its fixtures just because its own call timed out -- teardown still
    runs."""
    torn_down: list[str] = []

    @velox.fixture()
    def quick():
        yield 1
        torn_down.append("closed")

    async def test_func(x: int = velox.Depends(quick)) -> None:
        assert x == 1
        await asyncio.sleep(10)

    (result,) = run_suite(
        [_record(0, test_func, "test_func", plan=plan_for(test_func))], timeout=0.05
    )

    assert result.outcome is Outcome.TIMEOUT
    assert torn_down == ["closed"]


def _result(outcome: Outcome) -> Result:
    return Result(id="mod.py::t", index=0, outcome=outcome, duration=0.0, failure=None)


def _error(name: str = "mod.py") -> CollectionError:
    return CollectionError(path=Path(name), message="boom")


def test_exit_code_all_passed_is_zero() -> None:
    assert exit_code_for([_result(Outcome.PASSED)], []) == 0


def test_exit_code_with_a_failure_is_one() -> None:
    assert exit_code_for([_result(Outcome.PASSED), _result(Outcome.FAILED)], []) == 1


def test_exit_code_with_an_error_is_one() -> None:
    """spec/05 §4's table: `error` and `failed` both contribute `1`."""
    assert exit_code_for([_result(Outcome.PASSED), _result(Outcome.ERROR)], []) == 1


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
