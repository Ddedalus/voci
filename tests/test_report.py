"""Unit tests for `velox._report.Reporter` (spec/10 §2).

Exercises `Reporter` directly against hand-built `TestResult`s and a `StringIO` stream, rather
than going through `main`/`run_suite` end to end — the same "unit-test the piece in isolation,
cover the wiring separately" split `tests/test_run.py` already uses for `_run_one`/`run_suite`
(see its own `_record` helper, mirrored here by `_result`).
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

from velox._report import Reporter, _failure_reason
from velox._run import Outcome
from velox._run import TestResult as Result

_TRACEBACK_FAILURE = (
    "Traceback (most recent call last):\n"
    '  File "test_sample.py", line 3, in test_fail\n'
    "    assert x == y\n"
    "AssertionError: assert 2 == 3\n"
)

_TIMEOUT_FAILURE = "test exceeded the --timeout=1.0s budget"

_TIMEOUT_FAILURE_WITH_TRACEBACK = (
    f"{_TIMEOUT_FAILURE}\n\nTraceback (most recent call last):\n  ...\nCancelledError\n"
)


def _result(
    id: str,
    index: int,
    outcome: Outcome = Outcome.PASSED,
    duration: float = 0.0,
    failure: str | None = None,
    captured_stdout: str = "",
    captured_stderr: str = "",
    log_records: tuple[logging.LogRecord, ...] = (),
) -> Result:
    return Result(
        id=id,
        index=index,
        outcome=outcome,
        duration=duration,
        failure=failure,
        captured_stdout=captured_stdout,
        captured_stderr=captured_stderr,
        log_records=log_records,
    )


def _log_record(message: str, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="myapp", level=level, pathname="mod.py", lineno=1, msg=message, args=(), exc_info=None
    )


# ------------------------------------------------------------------------------------------
# Per-file scrollback blocks (on_result)
# ------------------------------------------------------------------------------------------


# Review (test quality, missing case): nothing in this section covers `paths_by_id` disagreeing
# with what `on_result` is actually fed, which is the one way this bookkeeping is currently wrong
# in production. `Reporter(paths_by_id={id_a: p}, ...)` then `on_result` for two results sharing
# `id_a` — reachable for real, since `_collect` builds ids from `func.__qualname__` and a
# factory-generated test repeats it (see the note in `_report.Reporter.__post_init__`) — prints
# `1 tests` / `PASS` for a file that had two tests and a failure, and silently swallows the second
# result. Whatever the chosen fix is, the assertion belongs here. A second, cheaper case with the
# same shape: a result whose id is absent from `paths_by_id` entirely, which today raises
# `KeyError` out of the callback and (through `run_suite`) discards the whole run.
def test_file_block_only_prints_once_every_test_of_that_file_has_reported() -> None:
    path = Path("tests/test_sample.py")
    paths_by_id = {
        f"{path}::test_a": path,
        f"{path}::test_b": path,
    }
    stream = io.StringIO()
    reporter = Reporter(paths_by_id=paths_by_id, capture_passthrough=False, stream=stream)

    reporter.on_result(_result(f"{path}::test_a", 0, duration=0.1))
    assert stream.getvalue() == ""  # not yet -- test_b hasn't reported

    reporter.on_result(_result(f"{path}::test_b", 1, duration=0.2))
    out = stream.getvalue()
    assert out != ""
    assert "PASS" in out
    assert str(path) in out
    assert "2 tests" in out
    # Summed, not spanned (concurrent dispatch has no single attributable wall-clock span for
    # "the file" -- spec/10 §2 / `on_result`'s own docstring).
    assert "0.30s" in out


def test_file_block_reports_fail_and_failed_count_when_any_test_failed() -> None:
    path = Path("tests/test_sample.py")
    paths_by_id = {
        f"{path}::test_a": path,
        f"{path}::test_b": path,
        f"{path}::test_c": path,
    }
    stream = io.StringIO()
    reporter = Reporter(paths_by_id=paths_by_id, capture_passthrough=False, stream=stream)

    reporter.on_result(_result(f"{path}::test_a", 0))
    reporter.on_result(_result(f"{path}::test_b", 1, outcome=Outcome.FAILED, failure="boom"))
    assert stream.getvalue() == ""
    reporter.on_result(_result(f"{path}::test_c", 2, outcome=Outcome.ERROR, failure="oops"))

    out = stream.getvalue()
    assert "FAIL" in out
    assert "3 tests" in out
    assert "(2 failed)" in out


def test_blocks_print_in_completion_order_not_paths_by_id_insertion_order() -> None:
    file_a = Path("tests/test_a.py")
    file_b = Path("tests/test_b.py")
    # `paths_by_id` inserted file_a first -- if block order followed insertion order rather than
    # completion order, file_a's block would print first regardless of what finishes when.
    paths_by_id = {
        f"{file_a}::test_1": file_a,
        f"{file_b}::test_1": file_b,
    }
    stream = io.StringIO()
    reporter = Reporter(paths_by_id=paths_by_id, capture_passthrough=False, stream=stream)

    # Review (test quality): one test per file is the weakness. With a single test per file,
    # "flush when the file's last test reports" and "flush when the file's *first* test reports"
    # and "emit in the order results arrive" are all the same behaviour, so this cannot see the
    # property the block design actually rests on: that a file whose first test finished *earliest*
    # still flushes *last*, because it is the completion of the file's final test that orders
    # blocks, not the file's first sighting. That is the case where the jest model differs from
    # xdist's per-test dribble, and it is untested. Three lines more: file_a with two tests,
    # file_b with one, delivered `a::test_1`, `b::test_1`, `a::test_2` — correct output is b's
    # block then a's, and an implementation keyed on first-arrival would print a's first. Worth
    # having also because the reversed-insertion premise this test does check is satisfied by any
    # implementation that prints during `on_result` at all.
    #
    # file_b's only test finishes first.
    reporter.on_result(_result(f"{file_b}::test_1", 1))
    reporter.on_result(_result(f"{file_a}::test_1", 0))

    lines = [line for line in stream.getvalue().splitlines() if line]
    assert len(lines) == 2
    assert str(file_b) in lines[0]
    assert str(file_a) in lines[1]


# ------------------------------------------------------------------------------------------
# End-of-run sections (finish) -- logical order
# ------------------------------------------------------------------------------------------


def test_finish_orders_by_results_list_not_on_result_order() -> None:
    path = Path("tests/test_sample.py")
    result0 = _result(
        f"{path}::test_a", 0, outcome=Outcome.FAILED, failure="AssertionError: a broke"
    )
    result1 = _result(
        f"{path}::test_b", 1, outcome=Outcome.FAILED, failure="AssertionError: b broke"
    )
    paths_by_id = {result0.id: path, result1.id: path}
    stream = io.StringIO()
    reporter = Reporter(paths_by_id=paths_by_id, capture_passthrough=False, stream=stream)

    # Review (test quality): these four lines are decorative — the test's name promises
    # "orders by results list, *not* on_result order", but `finish` never reads any `on_result`
    # state at all (it iterates the `results` argument; `_buffered_by_path` was already popped and
    # is documented as write-only for this purpose), so there is no coupling for the reversal to
    # break. Verified by deleting exactly this block and rerunning: 15 passed. As written the test
    # establishes only "`finish` iterates its argument in order", which is true of the one-line
    # implementation it is testing.
    #
    # To test the stated property you have to make completion order *visible* to `finish`, which
    # today means going through `run_suite`: two tests in one file with descending sleeps and
    # `concurrency=2`, asserting the failure-details section is in `index` order while a
    # completion-order list collected from the same `on_result` callback is the reverse. That also
    # covers the wiring — see the note in `_run.run_suite`'s `on_result` docstring, which nothing
    # currently exercises. Failing that, at minimum assert what this test *can* see: that
    # `finish([result1, result0], ...)` emits test_b before test_a, i.e. that the argument order
    # is genuinely what decides, not the ids' natural sort.
    #
    # Completion order is reversed relative to logical (index) order.
    reporter.on_result(result1)
    reporter.on_result(result0)
    stream.truncate(0)
    stream.seek(0)

    # `results` (the caller's authoritative, logical-order list) is [result0, result1].
    reporter.finish([result0, result1], wall_clock=1.0)

    out = stream.getvalue()
    assert out.index("test_a") < out.index("test_b")
    # And it holds for both sections, not just whichever comes first.
    first_summary_a = out.index("test_a", out.index("--- short test summary ---"))
    first_summary_b = out.index("test_b", out.index("--- short test summary ---"))
    assert first_summary_a < first_summary_b


# ------------------------------------------------------------------------------------------
# Short-summary "reason" extraction
# ------------------------------------------------------------------------------------------


# Review (test quality — this is the gap that let the `_failure_reason` bug through): every case
# in this section is fed a hand-written `failure` string, and `_TRACEBACK_FAILURE` above was
# written to end in `AssertionError: assert 2 == 3`, i.e. to match the heuristic. No test in this
# file has ever seen text `_run._run_one` actually produced. Four shapes it produces routinely all
# return something useless, each reproduced by running `velox` on a scratch directory (details in
# the note on `_report._failure_reason`):
#
#   assert total == 4, "expected four widgets"        -> "assert 3 == 4"   (message lost; the
#                                                        rewriter's explanation follows the
#                                                        exception line, under the *default* mode)
#   async with asyncio.TaskGroup(): ...               -> "+------------------------------------"
#   any fixture teardown raising                      -> "+------------------------------------"
#                                                        (`_di._release_all` always raises a
#                                                         BaseExceptionGroup, spec/04 §5)
#   raise ValueError("line one\nline two")            -> "line two"
#
# The teardown one is the reason this is a coverage gap and not a nitpick: it is not an exotic
# input, it is what *every* `Outcome.ERROR` from teardown looks like in this codebase. The fix for
# the tests is the same either way — build the fixtures from real output. `traceback.format_exc()`
# inside a small helper that actually raises (an `ExceptionGroup`, an exception with a note, an
# exception with a multi-line message) costs a few lines and cannot drift from what `_run_one`
# emits, and one end-to-end case in `test_cli.py` asserting the short-summary line for
# `assert x == y, "msg"` would pin the shape users copy most.
def test_short_summary_reason_for_failed_is_the_last_traceback_line() -> None:
    result = _result("f.py::test_fail", 0, outcome=Outcome.FAILED, failure=_TRACEBACK_FAILURE)
    assert _failure_reason(result) == "AssertionError: assert 2 == 3"


def test_short_summary_reason_for_timeout_is_the_first_line() -> None:
    result = _result(
        "f.py::test_hangs", 0, outcome=Outcome.TIMEOUT, failure=_TIMEOUT_FAILURE_WITH_TRACEBACK
    )
    assert _failure_reason(result) == _TIMEOUT_FAILURE


def test_short_summary_reason_for_bare_timeout_with_no_traceback() -> None:
    result = _result("f.py::test_hangs", 0, outcome=Outcome.TIMEOUT, failure=_TIMEOUT_FAILURE)
    assert _failure_reason(result) == _TIMEOUT_FAILURE


def test_finish_short_summary_lines_match_pytest_kept_verbatim_shape() -> None:
    path = Path("f.py")
    result = _result(f"{path}::test_fail", 0, outcome=Outcome.FAILED, failure=_TRACEBACK_FAILURE)
    stream = io.StringIO()
    reporter = Reporter(paths_by_id={result.id: path}, capture_passthrough=False, stream=stream)

    reporter.finish([result], wall_clock=1.0)

    out = stream.getvalue()
    assert f"FAILED {result.id} - AssertionError: assert 2 == 3" in out


def test_finish_short_summary_line_for_timeout() -> None:
    path = Path("f.py")
    result = _result(
        f"{path}::test_hangs",
        0,
        outcome=Outcome.TIMEOUT,
        failure=_TIMEOUT_FAILURE_WITH_TRACEBACK,
    )
    stream = io.StringIO()
    reporter = Reporter(paths_by_id={result.id: path}, capture_passthrough=False, stream=stream)

    reporter.finish([result], wall_clock=1.0)

    out = stream.getvalue()
    assert f"TIMEOUT {result.id} - {_TIMEOUT_FAILURE}" in out


# ------------------------------------------------------------------------------------------
# Captured stdout/stderr/log_records in failure details
# ------------------------------------------------------------------------------------------


def test_captured_stdout_stderr_shown_when_not_passthrough() -> None:
    path = Path("f.py")
    result = _result(
        f"{path}::test_fail",
        0,
        outcome=Outcome.FAILED,
        failure="boom",
        captured_stdout="hello from stdout",
        captured_stderr="hello from stderr",
    )
    stream = io.StringIO()
    reporter = Reporter(paths_by_id={result.id: path}, capture_passthrough=False, stream=stream)

    reporter.finish([result], wall_clock=1.0)

    out = stream.getvalue()
    assert "--- captured stdout ---" in out
    assert "hello from stdout" in out
    assert "--- captured stderr ---" in out
    assert "hello from stderr" in out


def test_captured_stdout_stderr_not_duplicated_under_passthrough() -> None:
    path = Path("f.py")
    result = _result(
        f"{path}::test_fail",
        0,
        outcome=Outcome.FAILED,
        failure="boom",
        captured_stdout="hello from stdout",
        captured_stderr="hello from stderr",
    )
    stream = io.StringIO()
    reporter = Reporter(paths_by_id={result.id: path}, capture_passthrough=True, stream=stream)

    reporter.finish([result], wall_clock=1.0)

    out = stream.getvalue()
    assert "--- captured stdout ---" not in out
    assert "hello from stdout" not in out
    assert "--- captured stderr ---" not in out
    assert "hello from stderr" not in out


def test_log_records_always_shown_regardless_of_passthrough() -> None:
    path = Path("f.py")
    record = _log_record("something happened")
    result = _result(
        f"{path}::test_fail", 0, outcome=Outcome.FAILED, failure="boom", log_records=(record,)
    )
    for passthrough in (True, False):
        stream = io.StringIO()
        reporter = Reporter(
            paths_by_id={result.id: path}, capture_passthrough=passthrough, stream=stream
        )
        reporter.finish([result], wall_clock=1.0)
        out = stream.getvalue()
        assert "--- captured log records ---" in out
        assert "something happened" in out


# ------------------------------------------------------------------------------------------
# Unattributed output
# ------------------------------------------------------------------------------------------


def test_unattributed_output_section_appears_only_when_non_empty() -> None:
    stream = io.StringIO()
    reporter = Reporter(paths_by_id={}, capture_passthrough=False, stream=stream)

    reporter.finish([], wall_clock=1.0, unattributed_output=None)
    assert "unattributed" not in stream.getvalue()

    stream2 = io.StringIO()
    reporter2 = Reporter(paths_by_id={}, capture_passthrough=False, stream=stream2)
    reporter2.finish([], wall_clock=1.0, unattributed_output=[])
    assert "unattributed" not in stream2.getvalue()

    stream3 = io.StringIO()
    reporter3 = Reporter(paths_by_id={}, capture_passthrough=False, stream=stream3)
    reporter3.finish([], wall_clock=1.0, unattributed_output=["stray output from a thread"])
    out3 = stream3.getvalue()
    assert "unattributed" in out3
    assert "stray output from a thread" in out3


# ------------------------------------------------------------------------------------------
# Wall-vs-Σ final line
# ------------------------------------------------------------------------------------------


def test_wall_vs_sigma_line_arithmetic() -> None:
    path = Path("f.py")
    results = [
        _result(f"{path}::test_a", 0, duration=1.0),
        _result(f"{path}::test_b", 1, duration=3.0),
    ]
    stream = io.StringIO()
    reporter = Reporter(
        paths_by_id={r.id: path for r in results}, capture_passthrough=False, stream=stream
    )

    # Σ = 1.0 + 3.0 = 4.0; wall_clock = 2.0 -> ratio = 2.0x.
    reporter.finish(results, wall_clock=2.0)

    out = stream.getvalue()
    assert "2 tests" in out
    assert "0 failed" in out
    assert "2.00s wall" in out
    assert "Σ 4.00s" in out
    assert "2.0x concurrency" in out


def test_wall_vs_sigma_line_guards_zero_wall_clock() -> None:
    path = Path("f.py")
    results = [_result(f"{path}::test_a", 0, duration=1.0)]
    stream = io.StringIO()
    reporter = Reporter(paths_by_id={results[0].id: path}, capture_passthrough=False, stream=stream)

    reporter.finish(results, wall_clock=0.0)

    out = stream.getvalue()
    assert "0.00s wall" in out
    # Review (test quality): the second assertion is vacuous twice over. A `ZeroDivisionError`
    # would have been *raised* out of `finish`, erroring the test before this line rather than
    # failing it, so the string can never appear in `out` regardless of the implementation — and
    # nothing here pins what the guard actually prints. Verified by replacing
    # `concurrency = "n/a concurrency"` with `"999.9x concurrency"`: 15 passed. The contract worth
    # asserting is `"n/a concurrency" in out` (a fabricated ratio must not be printed) plus a
    # `wall_clock` that is small-but-nonzero taking the other branch, since the guard's stated
    # justification is about empty suites and — see the note in `_report.finish` — an empty suite
    # never actually reaches it (measured: `run_suite([])` takes ~1ms of `install`/`Runner`
    # overhead, so `wall_clock > 0`). Also uncovered anywhere in this file: `finish([], ...)` with
    # a zero-test run, which is the shape `velox` on an empty directory produces on every exit-5
    # run.
    assert "ZeroDivisionError" not in out
