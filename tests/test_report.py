"""Tests for `velox._report.Reporter`: per-file scrollback blocks, end-of-run sections
(failure details, short summary, unattributed output, wall-vs-Σ), and path elision --
exercised directly against hand-built `TestResult`s and a `StringIO` stream.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest
from velox._collect import TestRecord as Record  # `Test*` makes pytest try to collect it itself
from velox._fixtures import ResolutionPlan
from velox._report import Reporter, _elide_middle, _failure_reason
from velox._run import Outcome
from velox._run import TestResult as Result

_EMPTY_PLAN = ResolutionPlan(steps=(), root_args=())


async def _noop() -> None:
    pass


def _test_record(id: str, path: Path, index: int = 0) -> Record:
    """A minimal, real `TestRecord` for feeding `Reporter`'s constructor. `Reporter` only ever
    reads `.id`/`.path` off each record; the rest of the fields are filler."""
    return Record(
        id=id,
        index=index,
        path=path,
        lineno=1,
        qualname=id.rsplit("::", 1)[-1],
        func=_noop,
        plan=_EMPTY_PLAN,
    )


_TRACEBACK_FAILURE = (
    "Traceback (most recent call last):\n"
    '  File "test_sample.py", line 3, in test_fail\n'
    "    assert x == y\n"
    "AssertionError: assert 2 == 3\n"
)

_TIMEOUT_FAILURE = "test exceeded its 1.0s timeout budget"

_TIMEOUT_FAILURE_WITH_TRACEBACK = (
    f"{_TIMEOUT_FAILURE}\n\nTraceback (most recent call last):\n  ...\nCancelledError\n"
)


def _result(
    id: str,
    index: int,
    outcome: Outcome = Outcome.PASSED,
    duration: float = 0.0,
    failure: str | None = None,
    failure_summary: str | None = None,
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
        failure_summary=failure_summary,
        captured_stdout=captured_stdout,
        captured_stderr=captured_stderr,
        log_records=log_records,
    )


def _log_record(message: str, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="myapp", level=level, pathname="mod.py", lineno=1, msg=message, args=(), exc_info=None
    )


class _FakeTTYStream(io.StringIO):
    def isatty(self) -> bool:
        return True


# ------------------------------------------------------------------------------------------
# Per-file scrollback blocks (on_result)
# ------------------------------------------------------------------------------------------


def test_file_block_only_prints_once_every_test_of_that_file_has_reported() -> None:
    path = Path("tests/test_sample.py")
    records = [
        _test_record(f"{path}::test_a", path, index=0),
        _test_record(f"{path}::test_b", path, index=1),
    ]
    stream = io.StringIO()
    reporter = Reporter(records=records, capture_passthrough=False, stream=stream)

    reporter.on_result(_result(f"{path}::test_a", 0, duration=0.1))
    assert stream.getvalue() == ""  # not yet -- test_b hasn't reported

    reporter.on_result(_result(f"{path}::test_b", 1, duration=0.2))
    out = stream.getvalue()
    assert out != ""
    assert "PASS" in out
    assert str(path) in out
    assert "2 tests" in out
    # Summed, not spanned: concurrent dispatch has no single attributable wall-clock span for
    # "the file", so durations are labeled Σ.
    assert "Σ 0.30s" in out


def test_file_block_reports_fail_and_failed_count_when_any_test_failed() -> None:
    path = Path("tests/test_sample.py")
    records = [
        _test_record(f"{path}::test_a", path, index=0),
        _test_record(f"{path}::test_b", path, index=1),
        _test_record(f"{path}::test_c", path, index=2),
    ]
    stream = io.StringIO()
    reporter = Reporter(records=records, capture_passthrough=False, stream=stream)

    reporter.on_result(_result(f"{path}::test_a", 0))
    reporter.on_result(_result(f"{path}::test_b", 1, outcome=Outcome.FAILED, failure="boom"))
    assert stream.getvalue() == ""
    reporter.on_result(_result(f"{path}::test_c", 2, outcome=Outcome.ERROR, failure="oops"))

    out = stream.getvalue()
    assert "FAIL" in out
    assert "3 tests" in out
    assert "(2 failed)" in out


def test_file_block_stays_pass_when_the_only_non_passed_results_are_xfail() -> None:
    """XFAILED and XPASSED both mean the test behaved exactly as its `xfail` mark said it
    would -- neither should flip the file's block to FAIL."""
    path = Path("tests/test_sample.py")
    records = [
        _test_record(f"{path}::test_a", path, index=0),
        _test_record(f"{path}::test_b", path, index=1),
        _test_record(f"{path}::test_c", path, index=2),
    ]
    stream = io.StringIO()
    reporter = Reporter(records=records, capture_passthrough=False, stream=stream)

    reporter.on_result(_result(f"{path}::test_a", 0))
    reporter.on_result(_result(f"{path}::test_b", 1, outcome=Outcome.XFAILED, failure="boom"))
    reporter.on_result(_result(f"{path}::test_c", 2, outcome=Outcome.XPASSED))

    out = stream.getvalue()
    assert "PASS" in out
    assert "FAIL" not in out


def test_duplicate_ids_across_records_are_each_counted_not_collapsed() -> None:
    """Two records sharing an id (as a factory-generated test's repeated `func.__qualname__`
    would produce) must each count toward the file's total, not collapse into one entry."""
    path = Path("tests/test_dup.py")
    shared_id = f"{path}::test_generated"
    records = [
        _test_record(shared_id, path, index=0),
        _test_record(shared_id, path, index=1),
    ]
    stream = io.StringIO()
    reporter = Reporter(records=records, capture_passthrough=False, stream=stream)

    reporter.on_result(_result(shared_id, 0, outcome=Outcome.FAILED, failure="boom"))
    assert stream.getvalue() == ""  # only one of the two records has reported in

    reporter.on_result(_result(shared_id, 1))
    out = stream.getvalue()
    assert "2 tests" in out
    assert "FAIL" in out
    assert "(1 failed)" in out


def test_on_result_for_an_id_outside_records_raises_key_error() -> None:
    """A result whose id was never in the `records` `Reporter` was constructed with raises
    `KeyError` loudly, rather than dropping the result silently."""
    reporter = Reporter(records=[], capture_passthrough=False, stream=io.StringIO())
    with pytest.raises(KeyError):
        reporter.on_result(_result("nope.py::test_x", 0))


def test_blocks_flush_on_a_files_last_test_not_its_first() -> None:
    """`file_a` has two tests, `file_b` has one; `file_a`'s first test finishes earliest,
    `file_b`'s only test next, and `file_a`'s second (and last) test finishes last. Correct
    output is `file_b`'s block, then `file_a`'s."""
    file_a = Path("tests/test_a.py")
    file_b = Path("tests/test_b.py")
    records = [
        _test_record(f"{file_a}::test_1", file_a, index=0),
        _test_record(f"{file_a}::test_2", file_a, index=1),
        _test_record(f"{file_b}::test_1", file_b, index=2),
    ]
    stream = io.StringIO()
    reporter = Reporter(records=records, capture_passthrough=False, stream=stream)

    reporter.on_result(_result(f"{file_a}::test_1", 0))
    assert stream.getvalue() == ""  # file_a still has test_2 outstanding

    reporter.on_result(_result(f"{file_b}::test_1", 2))
    lines = [line for line in stream.getvalue().splitlines() if line]
    assert len(lines) == 1
    assert str(file_b) in lines[0]  # file_b flushed even though file_a was seen first

    reporter.on_result(_result(f"{file_a}::test_2", 1))
    lines = [line for line in stream.getvalue().splitlines() if line]
    assert len(lines) == 2
    assert str(file_b) in lines[0]
    assert str(file_a) in lines[1]


# ------------------------------------------------------------------------------------------
# End-of-run sections (finish) -- logical order
# ------------------------------------------------------------------------------------------


def test_finish_orders_by_the_results_argument_not_ids_natural_sort() -> None:
    """`finish` orders by `results`'s own position, not by re-deriving an order from the data --
    ids are chosen here so that alphabetical order is the *opposite* of the intended (index)
    order."""
    path = Path("tests/test_sample.py")
    result0 = _result(f"{path}::test_z", 0, outcome=Outcome.FAILED, failure="AssertionError: a")
    result1 = _result(f"{path}::test_a", 1, outcome=Outcome.FAILED, failure="AssertionError: b")
    records = [_test_record(result0.id, path, index=0), _test_record(result1.id, path, index=1)]
    stream = io.StringIO()
    reporter = Reporter(records=records, capture_passthrough=False, stream=stream)

    # `results` (the caller's authoritative, logical-order list) puts test_z before test_a --
    # opposite of what an id sort would produce.
    reporter.finish([result0, result1], wall_clock=1.0)

    out = stream.getvalue()
    assert out.index("test_z") < out.index("test_a")
    first_summary_z = out.index("test_z", out.index("--- short test summary ---"))
    first_summary_a = out.index("test_a", out.index("--- short test summary ---"))
    assert first_summary_z < first_summary_a


def test_finish_omits_xfailed_and_xpassed_from_failure_details_and_short_summary() -> None:
    path = Path("f.py")
    passed = _result(f"{path}::test_a", 0)
    xfailed = _result(f"{path}::test_b", 1, outcome=Outcome.XFAILED, failure="boom")
    xpassed = _result(f"{path}::test_c", 2, outcome=Outcome.XPASSED)
    records = [_test_record(r.id, path, index=i) for i, r in enumerate([passed, xfailed, xpassed])]
    stream = io.StringIO()
    reporter = Reporter(records=records, capture_passthrough=False, stream=stream)

    reporter.finish([passed, xfailed, xpassed], wall_clock=1.0)

    out = stream.getvalue()
    assert "--- short test summary ---" not in out
    assert "boom" not in out
    assert "3 tests · 0 failed" in out


# Short-summary "reason" extraction: a direct read of `TestResult.failure_summary`.
# ------------------------------------------------------------------------------------------


def test_short_summary_reason_reads_the_failure_summary_field_verbatim() -> None:
    result = _result(
        "f.py::test_fail", 0, outcome=Outcome.FAILED, failure_summary="AssertionError: boom"
    )
    assert _failure_reason(result) == "AssertionError: boom"


def test_short_summary_reason_is_empty_string_when_failure_summary_is_none() -> None:
    result = _result("f.py::test_fail", 0, outcome=Outcome.FAILED, failure_summary=None)
    assert _failure_reason(result) == ""


def test_finish_short_summary_line_matches_pytest_kept_verbatim_shape() -> None:
    path = Path("f.py")
    result = _result(
        f"{path}::test_fail",
        0,
        outcome=Outcome.FAILED,
        failure=_TRACEBACK_FAILURE,
        failure_summary="AssertionError: assert 2 == 3",
    )
    records = [_test_record(result.id, path)]
    stream = io.StringIO()
    reporter = Reporter(records=records, capture_passthrough=False, stream=stream)

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
        failure_summary=_TIMEOUT_FAILURE,
    )
    records = [_test_record(result.id, path)]
    stream = io.StringIO()
    reporter = Reporter(records=records, capture_passthrough=False, stream=stream)

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
    records = [_test_record(result.id, path)]
    stream = io.StringIO()
    reporter = Reporter(records=records, capture_passthrough=False, stream=stream)

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
    records = [_test_record(result.id, path)]
    stream = io.StringIO()
    reporter = Reporter(records=records, capture_passthrough=True, stream=stream)

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
    records = [_test_record(result.id, path)]
    for passthrough in (True, False):
        stream = io.StringIO()
        reporter = Reporter(records=records, capture_passthrough=passthrough, stream=stream)
        reporter.finish([result], wall_clock=1.0)
        out = stream.getvalue()
        assert "--- captured log records ---" in out
        assert "something happened" in out


def test_failure_text_trailing_newline_does_not_leave_a_stray_blank_line() -> None:
    """`result.failure` is `traceback.format_exc()` text, which already ends in `"\\n"` -- a bare
    `print` would add a second, so the section spacing after it would depend on what `format_exc`
    happened to end with rather than being a property of this method's own layout."""
    path = Path("f.py")
    result = _result(
        f"{path}::test_fail", 0, outcome=Outcome.FAILED, failure="boom\n", log_records=()
    )
    records = [_test_record(result.id, path)]
    stream = io.StringIO()
    reporter = Reporter(records=records, capture_passthrough=False, stream=stream)

    reporter.finish([result], wall_clock=1.0)

    lines = stream.getvalue().splitlines()
    # "boom" immediately followed by "--- short test summary ---" -- no blank line between them.
    boom_index = lines.index("boom")
    assert lines[boom_index + 1] == "--- short test summary ---"


# ------------------------------------------------------------------------------------------
# Unattributed output
# ------------------------------------------------------------------------------------------


def test_unattributed_output_section_appears_only_when_non_empty() -> None:
    stream = io.StringIO()
    reporter = Reporter(records=[], capture_passthrough=False, stream=stream)

    reporter.finish([], wall_clock=1.0, unattributed_output=None)
    assert "unattributed" not in stream.getvalue()

    stream2 = io.StringIO()
    reporter2 = Reporter(records=[], capture_passthrough=False, stream=stream2)
    reporter2.finish([], wall_clock=1.0, unattributed_output=[])
    assert "unattributed" not in stream2.getvalue()

    stream3 = io.StringIO()
    reporter3 = Reporter(records=[], capture_passthrough=False, stream=stream3)
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
    records = [_test_record(r.id, path, index=i) for i, r in enumerate(results)]
    stream = io.StringIO()
    reporter = Reporter(records=records, capture_passthrough=False, stream=stream)

    # Σ = 1.0 + 3.0 = 4.0; wall_clock = 2.0 -> ratio = 2.0x.
    reporter.finish(results, wall_clock=2.0)

    out = stream.getvalue()
    assert "2 tests" in out
    assert "0 failed" in out
    assert "2.00s wall" in out
    assert "Σ 4.00s" in out
    assert "2.0x concurrency" in out


def test_finish_with_zero_tests() -> None:
    """The shape `velox` prints on an empty directory, or any run with nothing collected."""
    stream = io.StringIO()
    reporter = Reporter(records=[], capture_passthrough=False, stream=stream)

    reporter.finish([], wall_clock=0.001)

    out = stream.getvalue()
    assert "0 tests" in out
    assert "0 failed" in out


def test_wall_vs_sigma_line_guards_non_positive_wall_clock() -> None:
    path = Path("f.py")
    results = [_result(f"{path}::test_a", 0, duration=1.0)]
    records = [_test_record(results[0].id, path)]
    stream = io.StringIO()
    reporter = Reporter(records=records, capture_passthrough=False, stream=stream)

    reporter.finish(results, wall_clock=0.0)

    out = stream.getvalue()
    assert "0.00s wall" in out
    # The guarded branch's actual output, pinned directly -- not just "doesn't raise"
    # (`ZeroDivisionError` would have propagated out of `finish` and errored the test before this
    # line, so asserting its absence from `out` was never a real check of the guard).
    assert "n/a concurrency" in out


def test_wall_vs_sigma_line_takes_the_real_ratio_for_a_small_nonzero_wall_clock() -> None:
    """A small-but-positive `wall_clock` takes the ratio branch, not the `n/a` guard."""
    path = Path("f.py")
    results = [_result(f"{path}::test_a", 0, duration=0.001)]
    records = [_test_record(results[0].id, path)]
    stream = io.StringIO()
    reporter = Reporter(records=records, capture_passthrough=False, stream=stream)

    reporter.finish(results, wall_clock=0.001)

    out = stream.getvalue()
    assert "n/a concurrency" not in out
    assert "concurrency" in out


# ------------------------------------------------------------------------------------------
# Path column elision (long paths)
# ------------------------------------------------------------------------------------------


def test_elide_middle_leaves_short_text_alone() -> None:
    assert _elide_middle("short.py", 32) == "short.py"


def test_elide_middle_keeps_both_ends_of_long_text() -> None:
    long_path = "tests/very/deeply/nested/package/test_billing_reconciliation.py"
    elided = _elide_middle(long_path, 32)
    assert len(elided) == 32
    assert elided.startswith("tests/very")
    assert elided.endswith(".py")
    assert "..." in elided


def test_file_block_path_column_is_elided_for_a_long_path() -> None:
    long_path = Path("tests/very/deeply/nested/package/test_billing_reconciliation.py")
    records = [_test_record(f"{long_path}::test_a", long_path)]
    stream = io.StringIO()
    reporter = Reporter(records=records, capture_passthrough=False, stream=stream)

    reporter.on_result(_result(records[0].id, 0))

    out = stream.getvalue()
    assert "..." in out
    assert str(long_path) not in out


# ------------------------------------------------------------------------------------------
# is_tty
# ------------------------------------------------------------------------------------------


def test_is_tty_reflects_the_streams_own_isatty() -> None:
    """`is_tty` is resolved once at construction via `stream.isatty()`, or `False` for a stream
    with no such method at all."""
    assert Reporter(records=[], capture_passthrough=False, stream=io.StringIO()).is_tty is False
    assert Reporter(records=[], capture_passthrough=False, stream=_FakeTTYStream()).is_tty is True
