"""Tests for velox._run.safety: the loop watchdog, un-awaited coroutine collection, the
call-phase misuse messages, and naming a sync test stuck in a worker thread."""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
import warnings
from collections.abc import Iterator
from types import CodeType

import pytest
from _support import make_record as _record
from _support import run_async

from velox._run import safety


@pytest.fixture
def installed_hook() -> Iterator[None]:
    """`safety.install()` for one test, undone whatever the test does."""
    safety.install()
    try:
        yield
    finally:
        safety.uninstall()


async def _helper() -> int:
    return 1


# `call_misuse`: a call phase that raised nothing and still tested nothing.
# ------------------------------------------------------------------------


def test_a_clean_call_phase_is_no_misuse() -> None:
    assert safety.call_misuse(None, []) is None


def test_a_returned_value_is_reported_with_its_repr() -> None:
    misuse = safety.call_misuse(42, [])

    assert misuse is not None
    assert "42" in misuse.summary
    assert "assert" in misuse.detail


def test_a_returned_coroutine_is_named_and_closed() -> None:
    """Named rather than repr'd (a coroutine's repr carries an address), and closed as it is
    reported -- leaving it to be collected later would raise a duplicate un-awaited warning
    against whichever test is running by then."""
    coro = _helper()

    misuse = safety.call_misuse(coro, [])

    assert misuse is not None
    assert "coroutine '_helper'" in misuse.summary
    assert "await" in misuse.summary
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED


def test_unawaited_coroutines_are_listed_with_where_they_were_dropped() -> None:
    entries = [
        safety.Unawaited(what="coroutine 'fetch'", where="tests/test_x.py:12"),
        safety.Unawaited(what="coroutine 'store'", where="tests/test_x.py:13"),
    ]

    misuse = safety.call_misuse(None, entries)

    assert misuse is not None
    assert "2 coroutines were never awaited" in misuse.summary
    assert "tests/test_x.py:12" in misuse.detail
    assert "tests/test_x.py:13" in misuse.detail


def test_a_returned_value_and_an_unawaited_coroutine_are_reported_together() -> None:
    misuse = safety.call_misuse("value", [safety.Unawaited(what="coroutine 'f'", where="x.py:1")])

    assert misuse is not None
    assert "value" in misuse.summary
    assert "coroutine 'f'" in misuse.summary


# The warning hook: which test an un-awaited coroutine is filed against.
# ---------------------------------------------------------------------


def test_a_coroutine_dropped_inside_the_window_is_collected(installed_hook: None) -> None:
    with safety.watch_unawaited() as collected:
        _helper()  # pyrefly: ignore[unused-coroutine]  -- the whole point of these tests

    assert len(collected) == 1
    assert collected[0].what == "coroutine '_helper'"
    assert "test_safety.py:" in str(collected[0])


def test_the_same_line_is_collected_every_time_it_runs(installed_hook: None) -> None:
    """Python's default filter shows one warning per source location; a parametrized test
    forgets its `await` at the same line in every case, so the filter is set to `always`."""
    for _ in range(3):
        with safety.watch_unawaited() as collected:
            _helper()  # pyrefly: ignore[unused-coroutine]  -- the whole point of these tests
        assert len(collected) == 1


def test_a_coroutine_dropped_with_no_test_running_goes_where_it_was_going(
    installed_hook: None,
) -> None:
    seen: list[str] = []
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.showwarning = lambda message, *args, **kwargs: seen.append(str(message))
        _helper()  # pyrefly: ignore[unused-coroutine]  -- the whole point of these tests

    assert any("was never awaited" in message for message in seen)


def test_install_reports_whether_it_was_the_one_that_installed() -> None:
    assert safety.install() is True
    try:
        assert safety.install() is False
    finally:
        safety.uninstall()


def test_uninstall_restores_the_previous_showwarning() -> None:
    before = warnings.showwarning
    safety.install()
    assert warnings.showwarning is not before
    safety.uninstall()
    assert warnings.showwarning is before


# `code_index`: which test a blocked frame belongs to.
# ---------------------------------------------------


def test_code_index_maps_a_test_function_to_its_id() -> None:
    async def test_func() -> None:
        pass

    index = safety.code_index([_record(0, test_func, "test_func")])

    assert index[test_func.__code__] == "mod.py::test_func"


def test_code_index_leaves_out_a_code_object_two_records_share() -> None:
    """Every case of a parametrized test is a separate record over one function: naming
    either of them for a stack that could be either is worse than naming none."""

    async def test_func() -> None:
        pass

    index = safety.code_index(
        [_record(0, test_func, "test_func[a]"), _record(1, test_func, "test_func[b]")]
    )

    assert index == {}


# The watchdog.
# -------------


def _watchdog(
    threshold: float, reports: list[str], test_ids: dict[CodeType, str] | None = None
) -> safety.LoopWatchdog:
    return safety.LoopWatchdog(threshold, report=reports.append, test_ids=test_ids)


def test_the_watchdog_names_the_call_blocking_the_loop() -> None:
    reports: list[str] = []

    async def blocks() -> None:
        watchdog = _watchdog(0.05, reports)
        watchdog.start()
        try:
            time.sleep(0.4)
            # Back on the loop: gives the heartbeat a chance to notice it is alive again.
            await asyncio.sleep(0.2)
        finally:
            watchdog.stop()

    run_async(blocks())

    assert any("event loop has been blocked" in report for report in reports)
    assert any("time.sleep(0.4)" in report for report in reports)
    assert any("running again after" in report for report in reports)


def test_the_watchdog_says_nothing_about_a_loop_that_keeps_running() -> None:
    reports: list[str] = []

    async def healthy() -> None:
        watchdog = _watchdog(0.2, reports)
        watchdog.start()
        try:
            for _ in range(10):
                await asyncio.sleep(0.02)
        finally:
            watchdog.stop()

    run_async(healthy())

    assert reports == []


def test_the_watchdog_names_the_test_the_blocking_frame_belongs_to() -> None:
    reports: list[str] = []

    def blocking_body() -> None:
        time.sleep(0.3)

    async def blocks() -> None:
        watchdog = _watchdog(
            0.05, reports, test_ids={blocking_body.__code__: "mod.py::test_blocks"}
        )
        watchdog.start()
        try:
            blocking_body()
            await asyncio.sleep(0)
        finally:
            watchdog.stop()

    run_async(blocks())

    assert any("mod.py::test_blocks is holding it" in report for report in reports)


def test_stopping_a_watchdog_that_never_started_is_a_no_op() -> None:
    safety.LoopWatchdog(1.0, report=lambda _text: None).stop()


# Sync calls that outlive the run.
# -------------------------------


def test_a_running_sync_call_is_named_while_it_runs() -> None:
    entered = threading.Event()
    release = threading.Event()

    def body() -> None:
        entered.set()
        release.wait(5)

    thread = threading.Thread(target=safety.track_sync_call("mod.py::test_stuck", body))
    thread.start()
    try:
        assert entered.wait(5)
        report = safety.stuck_calls()
    finally:
        release.set()
        thread.join(5)

    assert report is not None
    assert "mod.py::test_stuck" in report
    assert "release.wait(5)" in report
    assert safety.stuck_calls() is None


def test_a_finished_sync_call_leaves_nothing_behind() -> None:
    safety.track_sync_call("mod.py::test_quick", lambda: None)()

    assert safety.stuck_calls() is None
