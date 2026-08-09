"""Tests for velox._capture (spec/09): Router/Sink attribution, logging, tmp_path, the
context-propagating executor, and worker slots.

Concurrency-proving tests here follow `test_run.py`'s own style: `asyncio.run(...)`-driven async
scenarios, `asyncio.gather`/real `run_suite` dispatch rather than mocking anything out, and
closures collecting side effects into a plain list/dict for the assertions afterward (single-
threaded event loop, so no lock is needed for that bookkeeping — same reasoning `test_run.py`'s
own `nonlocal in_flight, peak` tests already rely on).
"""

from __future__ import annotations

import asyncio
import contextvars
import io
import logging
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
import velox
from velox import _capture
from velox._collect import TestRecord as Record
from velox._fixtures import ResolutionPlan, plan_for
from velox._run import Outcome, run_suite

#: Same trivial-plan convenience `test_run.py` defines, for tests that don't need `Depends(...)`.
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


# ------------------------------------------------------------------------------------------
# Sink / _CappedBuffer: size cap, head+tail truncation.
# ------------------------------------------------------------------------------------------


def test_sink_keeps_everything_under_the_cap_verbatim() -> None:
    sink = _capture.Sink("t", limit=1000)
    sink.write_out("hello ")
    sink.write_out("world")
    assert sink.out == "hello world"
    assert "omitted" not in sink.out


def test_sink_truncates_with_head_and_tail_once_over_the_cap() -> None:
    """spec/09 §1: once the cap is exceeded, the buffer switches to head+tail truncation with a
    marker rather than either silently dropping new writes or growing unboundedly (I5)."""
    sink = _capture.Sink("t", limit=100)
    sink.write_out("A" * 40)  # fits entirely in the head
    sink.write_out("Z" * 5000)  # blows the cap wide open

    out = sink.out
    assert out.startswith("A" * 40)
    assert "characters omitted" in out
    # The whole point: nowhere near the naive 5040-character concatenation.
    assert len(out) < 500
    # The tail is real trailing content, not just the marker.
    assert out.endswith("Z" * 10)


@pytest.mark.parametrize("n", [50, 51, 100, 101])
def test_capped_buffer_marker_appears_iff_something_was_actually_dropped(n: int) -> None:
    """Pins the real contract at the exact boundary a previous version got wrong: the marker
    means "something was dropped", not "the head budget was exceeded". With `limit=100` (head
    budget 50), a single write of `n` characters keeps everything for `n <= 100` -- the overflow
    past the head always fits inside the 50-character tail budget -- and only starts genuinely
    losing content at `n=101`. An earlier implementation latched a `_truncated` flag the moment
    the head filled (`n > 50`) and showed the marker whenever that flag was set, so `n=51` and
    `n=100` both produced a false "capture limit exceeded, 0 characters omitted" banner spliced
    into output that was actually retained in full."""
    buf = _capture._CappedBuffer(limit=100)
    buf.write("X" * n)
    out = buf.getvalue()
    if n <= 100:
        assert "omitted" not in out
        assert out == "X" * n
    else:
        assert "omitted" in out
        assert out.count("X") == 100


def test_sink_stdout_and_stderr_are_capped_independently() -> None:
    """A chatty stderr logger must not starve stdout's own budget or vice versa (module
    docstring's `DEFAULT_CAPTURE_LIMIT` comment)."""
    sink = _capture.Sink("t", limit=100)
    sink.write_err("E" * 5000)
    sink.write_out("plain stdout, untouched")
    assert sink.out == "plain stdout, untouched"
    assert "omitted" not in sink.out
    assert "omitted" in sink.err


# ------------------------------------------------------------------------------------------
# Router: ContextVar-based attribution, unit level.
# ------------------------------------------------------------------------------------------


def test_router_falls_back_to_the_session_sink_with_no_active_test_context() -> None:
    session_sink = _capture.Sink("<unattributed>")
    router = _capture.Router(io.StringIO(), "stdout", session_sink, passthrough=False)
    assert _capture.current_test_context.get() is None

    router.write("stray output\n")

    assert "stray output" in session_sink.out


def test_router_routes_to_whichever_sink_is_currently_set() -> None:
    session_sink = _capture.Sink("<unattributed>")
    router = _capture.Router(io.StringIO(), "stdout", session_sink, passthrough=False)
    test_sink = _capture.Sink("some::test")

    token = _capture.current_test_context.set(
        _capture.TestContext(sink=test_sink, tags=(), timeout=None, worker=0)
    )
    try:
        router.write("attributed output\n")
    finally:
        _capture.current_test_context.reset(token)

    assert "attributed output" in test_sink.out
    assert "attributed output" not in session_sink.out

    # Once reset, writes go back to falling through to the session sink.
    router.write("stray again\n")
    assert "stray again" in session_sink.out


def test_router_passthrough_prefixes_each_line_with_the_sink_label() -> None:
    """spec/09 §1: `-s`/`--capture=no` prefixes each line with the test id so concurrent output
    stays readable — a single `write()` call is not guaranteed to be newline-aligned, so this
    exercises a call ending mid-line followed by one starting mid-line."""
    real = io.StringIO()
    session_sink = _capture.Sink("<unattributed>")
    router = _capture.Router(real, "stdout", session_sink, passthrough=True)
    label = "tests/test_x.py::test_thing"
    sink = _capture.Sink(label)

    token = _capture.current_test_context.set(
        _capture.TestContext(sink=sink, tags=(), timeout=None, worker=0)
    )
    try:
        router.write("line one\n")
        router.write("line two")
        router.write("\nline three\n")
    finally:
        _capture.current_test_context.reset(token)

    assert real.getvalue() == (f"[{label}] line one\n[{label}] line two\n[{label}] line three\n")
    # Passthrough doesn't stop the ordinary capture from also happening.
    assert sink.out == "line one\nline two\nline three\n"


def test_router_without_passthrough_never_touches_the_real_stream() -> None:
    real = io.StringIO()
    session_sink = _capture.Sink("<unattributed>")
    router = _capture.Router(real, "stdout", session_sink, passthrough=False)
    router.write("only captured, never echoed\n")
    assert real.getvalue() == ""
    assert "only captured" in session_sink.out


def test_capture_isolates_concurrent_tests_stdout() -> None:
    """The whole point of the Router/Sink mechanism (spec/09 §1): two tasks writing distinctive,
    interleaved output at the same time must each see only their own text through their own
    `Sink`, never their sibling's.

    Forced into genuine lockstep with an `asyncio.Barrier(3)`: each of the three tasks writes once
    per round, then blocks until the *other two* have also reached the barrier before writing
    again, so consecutive writes provably come from different tasks rather than one task quietly
    running to completion before the next starts (which would make the isolation assertions below
    trivially true even if attribution were broken — the failure mode `order`'s own assertion
    exists to catch)."""

    async def scenario() -> None:
        session_sink = _capture.Sink("<unattributed>")
        router = _capture.Router(io.StringIO(), "stdout", session_sink, passthrough=False)
        sinks: dict[str, _capture.Sink] = {}
        order: list[str] = []
        barrier = asyncio.Barrier(3)

        async def run_as(label: str) -> None:
            sink = _capture.Sink(label)
            sinks[label] = sink
            token = _capture.current_test_context.set(
                _capture.TestContext(sink=sink, tags=(), timeout=None, worker=0)
            )
            try:
                for i in range(30):
                    router.write(f"{label}-{i}\n")
                    order.append(label)
                    await barrier.wait()
            finally:
                _capture.current_test_context.reset(token)

        await asyncio.gather(run_as("alpha"), run_as("beta"), run_as("gamma"))

        # Genuine interleaving, not three sequential runs: all three labels appear before any
        # label repeats, which the barrier guarantees every round.
        assert set(order[:3]) == {"alpha", "beta", "gamma"}

        for label, sink in sinks.items():
            for i in range(30):
                assert f"{label}-{i}\n" in sink.out
            for other in sinks:
                if other != label:
                    assert other not in sink.out

    asyncio.run(scenario())


# ------------------------------------------------------------------------------------------
# End-to-end through `_run.run_suite`: the real dispatch path, not just the Router in isolation.
# ------------------------------------------------------------------------------------------


def test_capture_fixture_is_live_during_the_test_not_just_post_hoc() -> None:
    """spec/09 §1/§7: `capture.out` reflects writes made *before* the read, mid-test — not a
    snapshot taken at some later point."""

    async def test_func(cap: velox.Capture = velox.Depends(velox.capture)) -> None:
        print("first")
        assert "first" in cap.out
        assert "second" not in cap.out
        print("second")
        assert "first" in cap.out
        assert "second" in cap.out

    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])
    assert result.outcome is Outcome.PASSED, result.failure


def test_run_suite_dispatches_two_concurrent_tests_without_cross_contaminating_capture() -> None:
    """Same property as `test_capture_isolates_concurrent_tests_stdout`, but through the real
    `run_suite` dispatch path end to end (Router install, per-test `Sink`/`TestContext`,
    `velox.capture` provider) rather than driving `Router` directly.

    Forced into lockstep with a shared `asyncio.Barrier(2)`, same reasoning as the lower-level
    test above: without it, both isolation assertions would hold just as well if `test_alpha` ran
    to completion before `test_beta` ever started, which would leave a regression that serialized
    dispatch (or that handed every test the session sink) undetected.
    """
    order: list[str] = []
    barrier = asyncio.Barrier(2)

    def make(label: str, other: str) -> Callable[..., object]:
        async def test_func(cap: velox.Capture = velox.Depends(velox.capture)) -> None:
            for i in range(15):
                print(f"{label}-{i}")
                order.append(label)
                await barrier.wait()
            assert cap.out.count(f"{label}-") == 15
            assert other not in cap.out

        test_func.__name__ = f"test_{label}"
        return test_func

    test_a = make("alpha", "beta")
    test_b = make("beta", "alpha")
    records = [
        _record(0, test_a, "test_alpha", plan=plan_for(test_a)),
        _record(1, test_b, "test_beta", plan=plan_for(test_b)),
    ]

    results = run_suite(records, concurrency=2)

    assert [r.outcome for r in results] == [Outcome.PASSED, Outcome.PASSED], [
        r.failure for r in results
    ]
    # Genuine interleaving, not two sequential runs: both labels appear within each of the
    # first two rounds -- the exact resumption order within a round isn't guaranteed (asyncio
    # doesn't promise which of two barrier-released tasks resumes first), so this checks "both
    # showed up" per round rather than one fixed alternating sequence.
    assert set(order[:2]) == {"alpha", "beta"}
    assert set(order[2:4]) == {"alpha", "beta"}


def test_failing_result_carries_captured_output_but_a_passing_one_does_not() -> None:
    """spec/09 §6: captured output is attached to `TestResult` only for a failing outcome."""

    async def test_pass() -> None:
        print("pass-output")

    async def test_fail() -> None:
        print("fail-output")
        raise AssertionError("boom")

    passed, failed = run_suite(
        [_record(0, test_pass, "test_pass"), _record(1, test_fail, "test_fail")]
    )

    assert passed.outcome is Outcome.PASSED
    assert passed.captured_stdout == ""
    assert passed.captured_stderr == ""
    assert passed.log_records == ()

    assert failed.outcome is Outcome.FAILED
    assert "fail-output" in failed.captured_stdout


def test_run_suite_reports_stray_output_from_a_detached_thread_as_unattributed() -> None:
    """spec/09 §3's thread-attribution table: a raw `threading.Thread` has its own, empty
    `contextvars.Context`, so its output cannot be attributed to any test and lands in the
    session sink instead -- exercised here through `run_suite`'s `unattributed_output`
    out-parameter, the real end-to-end path (spec/09 §9 Q4, "tee it in")."""
    done = threading.Event()

    async def test_func() -> None:
        def worker() -> None:
            print("stray-from-a-raw-thread")
            done.set()

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=5)
        assert done.is_set()

    unattributed: list[str] = []
    results = run_suite([_record(0, test_func, "test_func")], unattributed_output=unattributed)

    assert results[0].outcome is Outcome.PASSED, results[0].failure
    assert any("stray-from-a-raw-thread" in section for section in unattributed)


# ------------------------------------------------------------------------------------------
# Logging: structured LogRecords, set_level.
# ------------------------------------------------------------------------------------------


def test_log_records_captures_structured_records_and_set_level_expands_visibility() -> None:
    logger_name = "velox_test_capture_app"

    async def test_func(records: velox.LogRecords = velox.Depends(velox.log_records)) -> None:
        logger = logging.getLogger(logger_name)
        logger.warning("warn-message")
        # DEBUG wouldn't be captured at all without raising this logger's own level first --
        # the root handler always accepts everything it's handed (spec/09 §2), but nothing is
        # handed to it unless the *originating* logger's effective level allows it through.
        logger.debug("not-yet-visible")
        with records.set_level(logging.DEBUG, logger=logger_name):
            logger.debug("debug-message")

        assert "warn-message" in records.messages
        assert "not-yet-visible" not in records.messages
        assert "debug-message" in records.messages
        assert any(r.levelno == logging.DEBUG and r.name == logger_name for r in records.records)

    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])
    assert result.outcome is Outcome.PASSED, result.failure


def test_log_records_are_isolated_between_concurrent_tests() -> None:
    """Same "forced lockstep, not just logically concurrent" reasoning as the stdout isolation
    tests above — see `test_capture_isolates_concurrent_tests_stdout`'s docstring."""
    order: list[str] = []
    barrier = asyncio.Barrier(2)

    def make(label: str, other: str) -> Callable[..., object]:
        async def test_func(records: velox.LogRecords = velox.Depends(velox.log_records)) -> None:
            logger = logging.getLogger(f"velox_test_capture_{label}")
            for i in range(10):
                logger.warning("%s-%d", label, i)
                order.append(label)
                await barrier.wait()
            assert len(records.records) == 10
            assert all(other not in m for m in records.messages)

        test_func.__name__ = f"test_{label}"
        return test_func

    test_a = make("alpha", "beta")
    test_b = make("beta", "alpha")
    results = run_suite(
        [
            _record(0, test_a, "test_alpha", plan=plan_for(test_a)),
            _record(1, test_b, "test_beta", plan=plan_for(test_b)),
        ],
        concurrency=2,
    )
    assert [r.outcome for r in results] == [Outcome.PASSED, Outcome.PASSED], [
        r.failure for r in results
    ]
    assert set(order[:2]) == {"alpha", "beta"}
    assert set(order[2:4]) == {"alpha", "beta"}


def test_set_level_is_not_isolated_under_concurrency_a_siblings_level_can_starve_a_capture() -> (
    None
):
    """spec/09 §2's documented hazard, made concrete (see `LogRecords.set_level`'s own docstring
    in `_builtins.py`): logger levels are process-global, so this is *not* a bug to fix, it is the
    actual, documented contract — a concurrent sibling *lowering* the same logger's level (an
    ordinary use of this API: silencing a noisy dependency) can make this test's own
    `set_level(DEBUG)` block capture nothing at all, even though nothing about this test's own
    code is wrong.

    Ordered precisely with a shared `asyncio.Barrier(2)` so the sequence is deterministic rather
    than a flaky race: `test_loud` sets DEBUG, `test_quiet` then overwrites the same logger to
    CRITICAL (last write wins — `logging.Logger.setLevel` has no notion of "highest wins"),
    `test_loud` logs while CRITICAL is still in effect, and only then does `test_quiet` restore
    its own previous level.
    """
    logger_name = "velox_test_capture_hazard"
    barrier = asyncio.Barrier(2)

    async def test_loud(records: velox.LogRecords = velox.Depends(velox.log_records)) -> None:
        with records.set_level(logging.DEBUG, logger=logger_name):
            await barrier.wait()  # cp1: DEBUG is set; let test_quiet overwrite it
            await barrier.wait()  # cp2: test_quiet's CRITICAL is now in effect
            logging.getLogger(logger_name).debug("should-have-been-captured")
            await barrier.wait()  # cp3: let test_quiet see the log landed before it restores
        # The hazard itself: a sibling's CRITICAL window starved this block of its own DEBUG
        # record, even though this block's own `set_level(DEBUG)` call was never undone.
        assert records.messages == ()

    async def test_quiet(records: velox.LogRecords = velox.Depends(velox.log_records)) -> None:
        await barrier.wait()  # cp1
        with records.set_level(logging.CRITICAL, logger=logger_name):
            await barrier.wait()  # cp2
            await barrier.wait()  # cp3

    results = run_suite(
        [
            _record(0, test_quiet, "test_quiet", plan=plan_for(test_quiet)),
            _record(1, test_loud, "test_loud", plan=plan_for(test_loud)),
        ],
        concurrency=2,
    )
    assert [r.outcome for r in results] == [Outcome.PASSED, Outcome.PASSED], [
        r.failure for r in results
    ]


# ------------------------------------------------------------------------------------------
# The context-propagating default executor (spec/09 §3).
# ------------------------------------------------------------------------------------------


def test_context_propagating_executor_propagates_the_submitters_contextvars() -> None:
    """A plain `ThreadPoolExecutor` would run `var.get(None)` in a fresh, empty context and get
    back `None` -- `ContextPropagatingExecutor` must instead see the submitting task's value."""

    async def scenario() -> object:
        var: contextvars.ContextVar[str] = contextvars.ContextVar("v")
        var.set("hello-from-the-submitting-task")
        executor = _capture.ContextPropagatingExecutor(max_workers=1)
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(executor, var.get, None)
        finally:
            executor.shutdown(wait=True)

    assert asyncio.run(scenario()) == "hello-from-the-submitting-task"


def test_run_suite_installs_a_context_propagating_default_executor() -> None:
    """`loop.run_in_executor(None, ...)` (the *default* executor, installed once per run by
    `run_all`) attributes its output to the calling test's sink -- spec/09 §3's table entry."""

    async def test_func(cap: velox.Capture = velox.Depends(velox.capture)) -> None:
        loop = asyncio.get_running_loop()

        def blocking() -> None:
            print("from-the-default-executor-thread")

        await loop.run_in_executor(None, blocking)
        assert "from-the-default-executor-thread" in cap.out

    (result,) = run_suite([_record(0, test_func, "test_func", plan=plan_for(test_func))])
    assert result.outcome is Outcome.PASSED, result.failure


# ------------------------------------------------------------------------------------------
# WorkerSlots / test_info.worker.
# ------------------------------------------------------------------------------------------


def test_worker_slots_is_a_bounded_free_list() -> None:
    slots = _capture.WorkerSlots(3)
    acquired = {slots.acquire() for _ in range(3)}
    assert acquired == {0, 1, 2}
    with pytest.raises(IndexError):
        slots.acquire()

    slots.release(1)
    assert slots.acquire() == 1


def test_test_info_worker_stays_within_concurrency_bound_under_real_dispatch() -> None:
    concurrency = 4
    seen: list[int] = []

    async def test_func(info: velox.TestInfo = velox.Depends(velox.test_info)) -> None:
        seen.append(info.worker)
        assert 0 <= info.worker < concurrency
        await asyncio.sleep(0.01)

    records = [_record(i, test_func, f"test_func_{i}", plan=plan_for(test_func)) for i in range(12)]

    results = run_suite(records, concurrency=concurrency)

    assert all(r.outcome is Outcome.PASSED for r in results), [r.failure for r in results]
    assert len(seen) == 12
    assert all(0 <= w < concurrency for w in seen)
    # 12 tests, only 4 slots, each sleeping: more than one distinct slot index must actually have
    # been handed out, or this would only be proving the trivial "0 is in range" case. This alone
    # would still pass against a broken round-robin `acquire()`, though — see the sibling test
    # below for the property that actually matters.
    assert len(set(seen)) > 1


def test_test_info_worker_is_never_held_by_two_tests_at_once() -> None:
    """The property `WorkerSlots` actually exists for (spec/09 §7: a future reporter's per-lane
    layout) is not "every worker index is in range" — `list(range(concurrency))` guarantees that
    on its own, and would survive `acquire()` degenerating to a constant `0` — it's "no two tests
    hold the same index *at the same time*". Checked directly here with a `live` set each test
    inserts into on entry and removes itself from on exit, asserting no collision at either point,
    with `concurrency=4` and enough tests that the free list must genuinely cycle."""
    concurrency = 4
    live: dict[int, str] = {}

    async def test_func(info: velox.TestInfo = velox.Depends(velox.test_info)) -> None:
        assert info.worker not in live, (
            f"worker {info.worker} already held by {live.get(info.worker)!r}"
        )
        live[info.worker] = info.id
        try:
            await asyncio.sleep(0.01)
        finally:
            del live[info.worker]

    records = [_record(i, test_func, f"test_func_{i}", plan=plan_for(test_func)) for i in range(12)]

    results = run_suite(records, concurrency=concurrency)

    assert all(r.outcome is Outcome.PASSED for r in results), [r.failure for r in results]
    assert live == {}


def test_test_info_reports_tags_and_the_suite_wide_timeout() -> None:
    async def test_func(info: velox.TestInfo = velox.Depends(velox.test_info)) -> None:
        assert info.timeout == 5.0
        assert info.id.endswith("test_func")

    (result,) = run_suite(
        [_record(0, test_func, "test_func", plan=plan_for(test_func))], timeout=5.0
    )
    assert result.outcome is Outcome.PASSED, result.failure


# ------------------------------------------------------------------------------------------
# tmp_path / tmp_path_factory: uniqueness by construction, retention, sanitization.
# ------------------------------------------------------------------------------------------


def test_sanitize_test_id_is_injective_for_ids_that_collide_after_escaping() -> None:
    """`a/b` and `a b` both escape to `a_b` -- the digest suffix is what keeps them apart, per
    spec/09 §5's own "hash-suffix anything that needed escaping"."""
    a = _capture.sanitize_test_id("tests/test_x.py::test_foo[a/b]")
    b = _capture.sanitize_test_id("tests/test_x.py::test_foo[a b]")
    assert a != b
    assert "/" not in a
    assert "/" not in b


def test_sanitize_test_id_appends_a_digest_even_when_no_escaping_is_needed() -> None:
    """Regression test: an earlier implementation appended the digest only when escaping changed
    something, so an already-safe id came back verbatim -- and could then collide with a
    *different* id's escaped-and-hashed output, since that output is itself a valid, already-safe
    string. `sanitize_test_id("a/b")` used to equal the literal string `"a_b_82badf67"`, so an
    already-safe id spelled exactly that way collided with `"a/b"`'s sanitized form. Appending the
    digest to every output, always computed over the true original, closes that."""
    result = _capture.sanitize_test_id("already_safe_id")
    assert result != "already_safe_id"
    assert result.startswith("already_safe_id_")

    escaped_and_hashed = _capture.sanitize_test_id("a/b")
    already_safe_that_used_to_collide = _capture.sanitize_test_id("a_b_82badf67")
    assert escaped_and_hashed != already_safe_that_used_to_collide


def test_sanitize_test_id_truncates_very_long_ids_with_a_fresh_digest_suffix() -> None:
    """The other half of the injectivity claim, previously untested: two long ids that share a
    truncated prefix must not collide either. Both `"x" * 500` and `"x" * 499 + "y"` need no
    escaping, so both blow straight past `_MAX_COMPONENT_LEN` on the digest-appended form alone
    and hit the truncation branch — which must still tell them apart."""
    long_id = "x" * 500
    other_long_id = "x" * 499 + "y"

    result = _capture.sanitize_test_id(long_id)
    assert len(result) <= _capture._MAX_COMPONENT_LEN
    assert _capture.sanitize_test_id(other_long_id) != result


def test_tmp_path_is_unique_per_test_and_lives_under_basetemp(tmp_path: Path) -> None:
    paths: dict[str, Path] = {}

    async def test_a(p: Path = velox.Depends(velox.tmp_path)) -> None:
        paths["a"] = p
        assert p.is_dir()

    async def test_b(p: Path = velox.Depends(velox.tmp_path)) -> None:
        paths["b"] = p
        assert p.is_dir()

    basetemp = tmp_path / "base"
    results = run_suite(
        [
            _record(0, test_a, "test_a", plan=plan_for(test_a)),
            _record(1, test_b, "test_b", plan=plan_for(test_b)),
        ],
        concurrency=2,
        basetemp=basetemp,
    )

    assert [r.outcome for r in results] == [Outcome.PASSED, Outcome.PASSED], [
        r.failure for r in results
    ]
    assert paths["a"] != paths["b"]
    assert paths["a"].is_relative_to(basetemp)
    assert paths["b"].is_relative_to(basetemp)


def test_tmp_path_factory_mktemp_numbers_by_construction(tmp_path: Path) -> None:
    """`mktemp` names are `sanitize_test_id(basename)` plus the numbered suffix — since
    `sanitize_test_id` always appends a digest (injectivity, see the `sanitize_test_id` tests
    above), the directory names carry a hash suffix even for an already-safe `basename` like
    `"data"`; what's pinned here is the numbering and uniqueness, not the exact literal name."""
    made: list[Path] = []

    async def test_func(
        factory: velox.TmpPathFactory = velox.Depends(velox.tmp_path_factory),
    ) -> None:
        made.append(factory.mktemp("data"))
        made.append(factory.mktemp("data"))
        made.append(factory.mktemp("fixed", numbered=False))

    (result,) = run_suite(
        [_record(0, test_func, "test_func", plan=plan_for(test_func))],
        basetemp=tmp_path / "base",
    )

    assert result.outcome is Outcome.PASSED, result.failure
    assert made[0] != made[1]
    data_sanitized = _capture.sanitize_test_id("data")
    assert made[0].name == f"{data_sanitized}0"
    assert made[1].name == f"{data_sanitized}1"
    assert made[2].name == _capture.sanitize_test_id("fixed")
    assert all(p.is_dir() for p in made)


def test_basetemp_override_is_cleared_before_use_when_it_looks_like_a_previous_basetemp(
    tmp_path: Path,
) -> None:
    """spec/09 §5's documented `--basetemp` warning: "this directory is cleared" — but only for a
    directory that already carries `_capture.BASETEMP_MARKER_NAME`, i.e. one velox itself made on
    an earlier run. That marker is the belt-and-braces half of the `--basetemp` safety story (the
    other half is `cli.py`'s own path-shape validation, tested in `test_cli.py`) — see the sibling
    test below for what happens without it."""
    override = tmp_path / "reused"
    override.mkdir()
    (override / _capture.BASETEMP_MARKER_NAME).write_text("")
    (override / "stale.txt").write_text("leftover from a previous run")

    async def test_func(p: Path = velox.Depends(velox.tmp_path)) -> None:
        assert p.is_dir()

    (result,) = run_suite(
        [_record(0, test_func, "test_func", plan=plan_for(test_func))], basetemp=override
    )

    assert result.outcome is Outcome.PASSED, result.failure
    assert not (override / "stale.txt").exists()
    assert (override / _capture.BASETEMP_MARKER_NAME).exists()


def test_basetemp_override_refuses_to_clear_a_directory_without_the_marker(tmp_path: Path) -> None:
    """The other half of the belt-and-braces guard: an existing directory `--basetemp` points at
    that does *not* carry the marker is refused rather than silently `rmtree`'d — the ordinary
    mistake this guards against is pointing `--basetemp` at a directory that merely happens to
    already exist (a typo, a directory the user actually cares about), not one velox itself made
    on a previous run."""
    not_a_basetemp = tmp_path / "definitely_not_ours"
    not_a_basetemp.mkdir()
    (not_a_basetemp / "important.txt").write_text("do not delete me")

    with pytest.raises(ValueError, match="does not look like a previous velox basetemp"):
        _capture.install(basetemp=not_a_basetemp)

    assert (not_a_basetemp / "important.txt").exists()
    assert _capture.installed() is None
    _capture.uninstall()  # still a safe no-op


def test_basetemp_retention_keeps_only_the_last_few_previous_roots(tmp_path: Path) -> None:
    parent = tmp_path / "velox-of-someone"
    roots = [_capture._allocate_session_root(parent, retention=2) for _ in range(5)]

    remaining = sorted(p.name for p in parent.iterdir())
    # 5 allocations, retention=2 -> the newest 3 survive (2 previous + the one just made).
    assert remaining == sorted(p.name for p in roots[-3:])


# ------------------------------------------------------------------------------------------
# install/uninstall: idempotency, restoring exactly what was replaced.
# ------------------------------------------------------------------------------------------


def test_install_is_idempotent_and_uninstall_restores_the_real_streams(tmp_path: Path) -> None:
    real_out, real_err = sys.stdout, sys.stderr
    assert _capture.installed() is None
    try:
        first = _capture.install(basetemp=tmp_path / "one")
        # Asking for exactly the same thing twice is genuinely idempotent.
        second = _capture.install(basetemp=tmp_path / "one")
        assert first is second
        assert sys.stdout is first.router_out
        assert sys.stderr is first.router_err
        assert first.basetemp_root == (tmp_path / "one").expanduser()
    finally:
        _capture.uninstall()

    assert sys.stdout is real_out
    assert sys.stderr is real_err
    assert _capture.installed() is None
    # Idempotent the other way too.
    _capture.uninstall()


def test_install_raises_rather_than_silently_ignoring_a_mismatched_second_call(
    tmp_path: Path,
) -> None:
    """A second `install()` call that disagrees with the live setup (a different `basetemp`, or a
    different `passthrough`) is a real conflict, not a harmless no-op — silently keeping the
    *first* call's arguments and discarding the second's would be exactly the kind of quiet
    degrade I6 argues against elsewhere in this module (`_require_test_context`'s loud
    `RuntimeError` is the model here)."""
    try:
        _capture.install(basetemp=tmp_path / "one")
        with pytest.raises(RuntimeError, match="already called with"):
            _capture.install(basetemp=tmp_path / "two")
        with pytest.raises(RuntimeError, match="already called with"):
            _capture.install(passthrough=True)
        # Leaving an argument at its default ("no opinion") is not a conflict.
        again = _capture.install()
        assert again is _capture.installed()
    finally:
        _capture.uninstall()


def test_install_rolls_back_cleanly_when_basetemp_resolution_fails(tmp_path: Path) -> None:
    """spec/09's I1 claim ("no state survives a `run_suite` call that raises") must hold even when
    the one fallible step inside `install()` — resolving `basetemp` — is what raises. Regression:
    an earlier version of `install()` swapped `sys.stdout`/`sys.stderr` and added the root log
    handler *before* this step, so a failure here left the process permanently mis-routed with no
    way to recover (`uninstall()` was a no-op forever, since `_installed` was never set)."""
    not_a_basetemp = tmp_path / "not_ours"
    not_a_basetemp.mkdir()  # exists, but carries no BASETEMP_MARKER_NAME -- install() must refuse

    real_out, real_err = sys.stdout, sys.stderr
    handlers_before = len(logging.getLogger().handlers)

    with pytest.raises(ValueError):
        _capture.install(basetemp=not_a_basetemp)

    assert sys.stdout is real_out
    assert sys.stderr is real_err
    assert len(logging.getLogger().handlers) == handlers_before
    assert _capture.installed() is None
    _capture.uninstall()  # still a safe no-op


def test_run_suite_uninstalls_capture_even_when_a_sibling_is_pending_during_an_interrupt() -> None:
    """Regression: `runner.close()` (in `run_suite`'s own teardown) can re-raise
    `KeyboardInterrupt`/`SystemExit` itself, not just the documented `RuntimeError`, whenever
    sibling tasks are still pending when the interrupt lands — which used to skip
    `_capture.uninstall()` entirely, leaving `sys.stdout`/`sys.stderr` permanently wrapped in
    `Router`s after a Ctrl-C hit a real, multi-test run."""

    async def _raises_keyboard_interrupt() -> None:
        raise KeyboardInterrupt

    async def _sleeps() -> None:
        await asyncio.sleep(1)

    real_out, real_err = sys.stdout, sys.stderr
    records = [
        _record(0, _raises_keyboard_interrupt, "test_ki"),
        _record(1, _sleeps, "test_sleep_a"),
        _record(2, _sleeps, "test_sleep_b"),
    ]

    with pytest.raises(KeyboardInterrupt):
        run_suite(records, concurrency=4)

    assert _capture.installed() is None
    assert sys.stdout is real_out
    assert sys.stderr is real_err


def test_unattributed_sections_formats_stdout_stderr_and_log_records() -> None:
    sink = _capture.Sink("<unattributed>")
    sink.write_out("stray stdout\n")
    sink.write_err("stray stderr\n")
    sink.log_records.append(
        logging.LogRecord("app", logging.WARNING, __file__, 1, "stray log", (), None)
    )

    sections = _capture.unattributed_sections(sink)

    assert any("stray stdout" in s for s in sections)
    assert any("stray stderr" in s for s in sections)
    assert any("stray log" in s for s in sections)


def test_unattributed_sections_is_empty_for_an_untouched_sink() -> None:
    assert _capture.unattributed_sections(_capture.Sink("<unattributed>")) == []
