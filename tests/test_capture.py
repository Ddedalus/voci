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
    # Review: the two writes here jump from 40 characters to 5040 against a limit of 100, so this
    # test never observes the interval where the implementation is actually wrong. `_CappedBuffer`
    # latches `_truncated` when the *head* budget (`limit // 2`) is exceeded, not when `limit` is,
    # so for any total in `51..100` it emits the "capture limit exceeded" marker having omitted
    # nothing -- see the note in `_capture._CappedBuffer.write`. Measured, `_CappedBuffer(100)`,
    # one write of N: N=50 no marker; N=51 marker with 0 lost; N=100 marker with 0 lost; N=101 the
    # first genuine loss. The companion test above (`..._under_the_cap_verbatim`) writes 11
    # characters against a limit of 1000, so it does not cover the interval either. A table-driven
    # case over N in `(limit // 2, limit // 2 + 1, limit, limit + 1)` asserting
    # `("omitted" in out) == (kept < N)` would pin the real contract -- "the marker appears iff
    # something was dropped" -- and fails today at N=51 and N=100.
    sink = _capture.Sink("t", limit=100)
    sink.write_out("A" * 40)  # fits entirely in the head
    sink.write_out("Z" * 5000)  # blows the cap wide open

    out = sink.out
    assert out.startswith("A" * 40)
    assert "bytes omitted" in out
    # The whole point: nowhere near the naive 5040-character concatenation.
    assert len(out) < 500
    # The tail is real trailing content, not just the marker.
    assert out.endswith("Z" * 10)


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
    `Sink`, never their sibling's -- proven with real `await`s between writes so the two tasks'
    output is genuinely interleaved at the `Router.write` level, not just logically concurrent."""

    async def scenario() -> None:
        session_sink = _capture.Sink("<unattributed>")
        router = _capture.Router(io.StringIO(), "stdout", session_sink, passthrough=False)
        sinks: dict[str, _capture.Sink] = {}

        async def run_as(label: str) -> None:
            sink = _capture.Sink(label)
            sinks[label] = sink
            token = _capture.current_test_context.set(
                _capture.TestContext(sink=sink, tags=(), timeout=None, worker=0)
            )
            try:
                for i in range(30):
                    router.write(f"{label}-{i}\n")
                    await asyncio.sleep(0)
            finally:
                _capture.current_test_context.reset(token)

        await asyncio.gather(run_as("alpha"), run_as("beta"), run_as("gamma"))

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


def _make_labeled_test(label: str, other: str) -> Callable[..., object]:
    async def test_func(cap: velox.Capture = velox.Depends(velox.capture)) -> None:
        for i in range(15):
            print(f"{label}-{i}")
            await asyncio.sleep(0)
        assert cap.out.count(f"{label}-") == 15
        assert other not in cap.out

    test_func.__name__ = f"test_{label}"
    return test_func


def test_run_suite_dispatches_two_concurrent_tests_without_cross_contaminating_capture() -> None:
    """Same property as `test_capture_isolates_concurrent_tests_stdout`, but through the real
    `run_suite` dispatch path end to end (Router install, per-test `Sink`/`TestContext`,
    `velox.capture` provider) rather than driving `Router` directly."""
    test_a = _make_labeled_test("alpha", "beta")
    test_b = _make_labeled_test("beta", "alpha")
    records = [
        _record(0, test_a, "test_alpha", plan=plan_for(test_a)),
        _record(1, test_b, "test_beta", plan=plan_for(test_b)),
    ]

    # Review: this passes unchanged against a purely sequential runner, which makes its docstring
    # ("through the real `run_suite` dispatch path end to end") stronger than what it proves. Each
    # body only ever inspects *its own* `cap.out` (`count(...) == 15`, `other not in ...`), and
    # both assertions hold trivially if `test_alpha` runs to completion before `test_beta` starts
    # -- nothing observes that the two were ever in flight at the same time, so a regression that
    # serialized dispatch, or one that gave every test the session sink while tests happened not
    # to overlap, would leave this green. `test_capture_isolates_concurrent_tests_stdout` above
    # has the same gap for the same reason. The cheap fix is the checkpoint pattern
    # `tests/test_run.py`'s concurrency tests already use and that this file's own module
    # docstring points at: a shared `asyncio.Event` (or an `order: list[str]` both bodies append
    # to) forcing each test to block until the other has written at least once, then asserting the
    # interleaving really happened -- `assert order[:4] == ["alpha", "beta", "alpha", "beta"]` or
    # similar -- *in addition* to the isolation assertions. Only the pair distinguishes "isolated
    # under real overlap" from "never overlapped".
    results = run_suite(records, concurrency=2)

    assert [r.outcome for r in results] == [Outcome.PASSED, Outcome.PASSED], [
        r.failure for r in results
    ]


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
    def make(label: str, other: str) -> Callable[..., object]:
        async def test_func(records: velox.LogRecords = velox.Depends(velox.log_records)) -> None:
            logger = logging.getLogger(f"velox_test_capture_{label}")
            for i in range(10):
                logger.warning("%s-%d", label, i)
                await asyncio.sleep(0)
            assert len(records.records) == 10
            assert all(other not in m for m in records.messages)

        test_func.__name__ = f"test_{label}"
        return test_func

    # Review: same shape as the stdout version above -- both bodies assert only about their own
    # `records`, so a sequential runner satisfies `len(records.records) == 10` and the `other not
    # in m` check just as well, and nothing here observes that the two tests overlapped. Worth
    # noting a second, sharper gap this file has no coverage for at all: `set_level` under real
    # interleaving is *not* isolated, and cannot be, because logger levels are process-global. A
    # forced-interleaving test (both tests inside their own `set_level` block at once, sibling
    # lowering the same logger the other raised) shows a test's own `set_level(DEBUG)` block
    # capturing nothing -- see the note on `LogRecords.set_level` in `_builtins.py`. Whatever
    # the intended contract is there, it deserves a test in this section that states it, rather
    # than only the single-test happy path `..._set_level_expands_visibility` covers today.
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
    # Review: the property that matters for `WorkerSlots` is not "every index is in range" (that
    # is guaranteed by `list(range(concurrency))` and would survive `acquire` returning a constant
    # `0`) but "no two tests hold the same index at the same time" -- which is what a reporter's
    # per-lane layout, the stated reason this exists, actually relies on. As written this test
    # passes against an implementation where `acquire()` is `return 0` and `release()` is a no-op,
    # save for the `len(set(seen)) > 1` line below, which a two-element round-robin would also
    # satisfy. The direct version costs three lines: keep a `live: dict[int, str]` in the test
    # body, assert `info.worker not in live` on entry, insert, `await asyncio.sleep(0.01)`, then
    # pop -- with `concurrency=4` and 12 tests that genuinely exercises reuse across the free
    # list. (I did check the implementation by hand and it is correct: there is no `await` in
    # `dispatch_one` between the semaphore admitting the task and the `try:` whose `finally`
    # releases the slot, so neither double-issue nor a cancellation leak is reachable. The point
    # is that this test is not what establishes that.)
    # 12 tests, only 4 slots, each sleeping: more than one distinct slot index must actually have
    # been handed out, or this would only be proving the trivial "0 is in range" case.
    assert len(set(seen)) > 1


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
    # Review: this covers the escaped-vs-escaped pair, which is the case the implementation
    # handles, and misses the escaped-vs-already-safe pair, which is the case it does not.
    # `sanitize_test_id` appends the digest only when escaping changed something, so an id that is
    # already safe comes back verbatim -- and an escaped id's *output* is itself a valid, already-
    # safe id. Verified: `sanitize_test_id("a/b") == sanitize_test_id("a_b_82badf67") ==
    # "a_b_82badf67"`. Worth adding here as a second case, especially because
    # `TmpPathFactory.mktemp(basename)` routes arbitrary user basenames through this same
    # function, where already-safe inputs are the norm rather than the exception (see the note in
    # `_capture.sanitize_test_id`). Also untested: the `_MAX_COMPONENT_LEN` truncation branch,
    # which is the other half of the docstring's injectivity claim and has no coverage at all.
    a = _capture.sanitize_test_id("tests/test_x.py::test_foo[a/b]")
    b = _capture.sanitize_test_id("tests/test_x.py::test_foo[a b]")
    assert a != b
    assert "/" not in a
    assert "/" not in b


def test_sanitize_test_id_leaves_an_already_safe_id_untouched() -> None:
    assert _capture.sanitize_test_id("already_safe_id") == "already_safe_id"


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
    assert made[0].name == "data0"
    assert made[1].name == "data1"
    assert made[2].name == "fixed"
    assert all(p.is_dir() for p in made)


def test_basetemp_override_is_cleared_before_use(tmp_path: Path) -> None:
    """spec/09 §5's documented `--basetemp` warning: "this directory is cleared"."""
    override = tmp_path / "reused"
    override.mkdir()
    (override / "stale.txt").write_text("leftover from a previous run")

    async def test_func(p: Path = velox.Depends(velox.tmp_path)) -> None:
        assert p.is_dir()

    (result,) = run_suite(
        [_record(0, test_func, "test_func", plan=plan_for(test_func))], basetemp=override
    )

    assert result.outcome is Outcome.PASSED, result.failure
    assert not (override / "stale.txt").exists()


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
    # Review: this covers the happy path and the double-`uninstall()` path, but not the one that
    # is actually broken -- `install()` raising *after* it has already swapped `sys.stdout`/
    # `sys.stderr` and added the root handler, but before it assigns `_installed`. The reachable
    # trigger is an ordinary `--basetemp` typo: `_capture.install(basetemp=<an existing regular
    # file>)` raises `NotADirectoryError` from `shutil.rmtree`, and afterwards `sys.stdout` is
    # still a `Router`, the root logger still has the handler, `installed()` is still `None`, and
    # `uninstall()` is a permanent no-op. A test asserting `pytest.raises(OSError)` around that
    # call followed by the same `sys.stdout is real_out` / handler-count assertions this test
    # already makes would pin the contract the module docstring claims ("`uninstall()` always
    # restores exactly what was there before ... no state survives past one `run_suite` call").
    # Worth a sibling case for the `run_suite`-level version too: a `KeyboardInterrupt` from one
    # test with siblings still pending currently exits `run_suite` without ever reaching
    # `uninstall()` (see the note at `_run.py`'s `runner.close()`), which no test in either file
    # would notice -- `tests/test_run.py`'s interrupt tests only assert on the exception type.
    real_out, real_err = sys.stdout, sys.stderr
    assert _capture.installed() is None
    try:
        first = _capture.install(basetemp=tmp_path / "one")
        second = _capture.install(basetemp=tmp_path / "two")  # ignored -- already installed
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
