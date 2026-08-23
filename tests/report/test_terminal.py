"""Tests for `velox._report.terminal.Reporter`: per-file scrollback blocks, end-of-run sections
(failure details, short summary, unattributed output, wall-vs-concurrency), and path elision --
exercised directly against hand-built `TestResult`s and a `StringIO` stream.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest

from velox._collection.collect import Skipped
from velox._collection.collect import TestRecord as Record
from velox._di.fixtures import ResolutionPlan
from velox._marks import marks_of
from velox._report.terminal import Reporter, _elide_middle, _failure_reason
from velox._run.run import Outcome
from velox._run.run import TestResult as Result

_EMPTY_PLAN = ResolutionPlan(steps=(), root_args=())


async def _noop() -> None:
    pass


def _test_record(id: str, path: Path, index: int = 0, patches: tuple[str, ...] = ()) -> Record:
    """A minimal, real `TestRecord` for feeding `Reporter`'s constructor. `Reporter` only ever
    reads `.id`/`.path`/`.patches` off each record; the rest of the fields are filler."""
    return Record(
        id=id,
        index=index,
        path=path,
        lineno=1,
        qualname=id.rsplit("::", 1)[-1],
        func=_noop,
        params=None,
        plan=_EMPTY_PLAN,
        marks=marks_of(_noop),
        patches=patches,
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


def _skipped(id: str, path: Path, reason: str = "not ready") -> Skipped:
    return Skipped(id=id, reason=reason, path=path)


def _reporter(
    records: list[Record],
    *,
    skipped: list[Skipped] | None = None,
    capture_passthrough: bool = False,
    verbosity: int = 0,
    durations: int = 0,
) -> tuple[Reporter, io.StringIO]:
    """A Reporter over a fresh StringIO, returned alongside it."""
    stream = io.StringIO()
    return (
        Reporter(
            records=records,
            skipped=skipped or [],
            capture_passthrough=capture_passthrough,
            stream=stream,
            verbosity=verbosity,
            durations=durations,
        ),
        stream,
    )


# ------------------------------------------------------------------------------------------
# Per-file scrollback blocks (on_result)
# ------------------------------------------------------------------------------------------


def test_file_block_only_prints_once_every_test_of_that_file_has_reported() -> None:
    path = Path("tests/test_sample.py")
    records = [
        _test_record(f"{path}::test_a", path, index=0),
        _test_record(f"{path}::test_b", path, index=1),
    ]
    reporter, stream = _reporter(records)

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
    reporter, stream = _reporter(records)

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
    reporter, stream = _reporter(records)

    reporter.on_result(_result(f"{path}::test_a", 0))
    reporter.on_result(_result(f"{path}::test_b", 1, outcome=Outcome.XFAILED, failure="boom"))
    reporter.on_result(_result(f"{path}::test_c", 2, outcome=Outcome.XPASSED))

    out = stream.getvalue()
    assert "PASS" in out
    assert "FAIL" not in out


def test_file_block_counts_its_skipped_tests_rather_than_listing_them() -> None:
    path = Path("tests/test_sample.py")
    records = [_test_record(f"{path}::test_a", path)]
    reporter, stream = _reporter(
        records, skipped=[_skipped(f"{path}::test_b", path, reason="not ready")]
    )

    reporter.on_result(_result(f"{path}::test_a", 0))

    out = stream.getvalue()
    assert "PASS" in out
    assert "2 tests" in out  # the file's own total, whether each test ran or not
    assert "(1 skipped)" in out
    assert "not ready" not in out


def test_file_block_counts_a_runtime_skip_alongside_a_marked_one() -> None:
    """A `velox.Skipped` raised mid-run reaches `on_result` like any other result -- it's
    already in `results`, not a separate list -- so it folds into the same `(N skipped)`
    annotation a `skip` mark's does."""
    path = Path("tests/test_sample.py")
    records = [_test_record(f"{path}::test_a", path), _test_record(f"{path}::test_b", path)]
    reporter, stream = _reporter(
        records, skipped=[_skipped(f"{path}::test_c", path, reason="not ready")]
    )

    reporter.on_result(_result(f"{path}::test_a", 0))
    reporter.on_result(_result(f"{path}::test_b", 1, outcome=Outcome.SKIPPED, failure="no backend"))

    out = stream.getvalue()
    assert "PASS" in out
    assert "3 tests" in out
    assert "(2 skipped)" in out
    assert "no backend" not in out


def test_a_file_whose_every_dispatched_test_skips_at_runtime_reads_as_skip_not_pass() -> None:
    """Unlike a mixed file (previous test), a file with results but none of them anything but
    `velox.Skipped` has nothing to call `PASS`: it reads as `SKIP`, the same status a wholly
    collection-time-skipped file gets, and does not also repeat the count as `(N skipped)` --
    the status word has already said it."""
    path = Path("tests/test_sample.py")
    records = [_test_record(f"{path}::test_a", path), _test_record(f"{path}::test_b", path)]
    reporter, stream = _reporter(records)

    reporter.on_result(_result(f"{path}::test_a", 0, outcome=Outcome.SKIPPED, failure="a"))
    reporter.on_result(_result(f"{path}::test_b", 1, outcome=Outcome.SKIPPED, failure="b"))

    out = stream.getvalue()
    assert "SKIP" in out
    assert "PASS" not in out
    assert "2 tests" in out
    assert "skipped)" not in out


def test_quiet_marks_a_wholly_runtime_skipped_file_with_an_s_not_a_dot() -> None:
    path = Path("tests/test_sample.py")
    records = [_test_record(f"{path}::test_a", path)]
    reporter, stream = _reporter(records, verbosity=-1)

    reporter.on_result(_result(f"{path}::test_a", 0, outcome=Outcome.SKIPPED, failure="no backend"))

    assert stream.getvalue() == "s"


def test_a_wholly_skipped_file_gets_a_skip_block_from_flush_pending() -> None:
    """No test of the file ever reports in, so `on_result` never reaches its block -- without
    `flush_pending` printing it, the file would vanish from the run's output entirely."""
    path = Path("tests/test_sample.py")
    reporter, stream = _reporter(
        [], skipped=[_skipped(f"{path}::test_a", path), _skipped(f"{path}::test_b", path)]
    )
    assert stream.getvalue() == ""

    reporter.flush_pending()

    line = stream.getvalue().strip()
    assert line.startswith("SKIP")
    assert "2 tests" in line
    # `SKIP` has already said it: the suffix is there to qualify a mixed file.
    assert "skipped)" not in line


def test_flush_pending_prints_a_skip_only_file_once() -> None:
    path = Path("tests/test_sample.py")
    reporter, stream = _reporter([], skipped=[_skipped(f"{path}::test_a", path)])

    reporter.flush_pending()
    reporter.flush_pending()

    assert stream.getvalue().count("SKIP") == 1


def test_duplicate_ids_across_records_are_each_counted_not_collapsed() -> None:
    """Two records sharing an id (as a factory-generated test's repeated `func.__qualname__`
    would produce) must each count toward the file's total, not collapse into one entry."""
    path = Path("tests/test_dup.py")
    shared_id = f"{path}::test_generated"
    records = [
        _test_record(shared_id, path, index=0),
        _test_record(shared_id, path, index=1),
    ]
    reporter, stream = _reporter(records)

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
    reporter, stream = _reporter(records)

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
    reporter, stream = _reporter(records)

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
    reporter, stream = _reporter(records)

    reporter.finish([passed, xfailed, xpassed], wall_clock=1.0)

    out = stream.getvalue()
    assert "--- short test summary ---" not in out
    assert "boom" not in out
    assert "3 tests · 1 passed · 1 xfailed · 1 xpassed · 1.00s wall" in out


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
    reporter, stream = _reporter(records)

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
    reporter, stream = _reporter(records)

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
    reporter, stream = _reporter(records)

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
    reporter, stream = _reporter(records, capture_passthrough=True)

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
        reporter, stream = _reporter(records, capture_passthrough=passthrough)
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
    reporter, stream = _reporter(records)

    reporter.finish([result], wall_clock=1.0)

    lines = stream.getvalue().splitlines()
    # "boom" immediately followed by "--- short test summary ---" -- no blank line between them.
    boom_index = lines.index("boom")
    assert lines[boom_index + 1] == "--- short test summary ---"


# ------------------------------------------------------------------------------------------
# Unattributed output
# ------------------------------------------------------------------------------------------


def test_unattributed_output_section_appears_only_when_non_empty() -> None:
    reporter, stream = _reporter([])

    reporter.finish([], wall_clock=1.0, unattributed_output=None)
    assert "unattributed" not in stream.getvalue()

    reporter2, stream2 = _reporter([])
    reporter2.finish([], wall_clock=1.0, unattributed_output=[])
    assert "unattributed" not in stream2.getvalue()

    reporter3, stream3 = _reporter([])
    reporter3.finish([], wall_clock=1.0, unattributed_output=["stray output from a thread"])
    out3 = stream3.getvalue()
    assert "unattributed" in out3
    assert "stray output from a thread" in out3


# ------------------------------------------------------------------------------------------
# Wall-vs-concurrency final line
# ------------------------------------------------------------------------------------------


def test_wall_vs_sigma_line_arithmetic() -> None:
    path = Path("f.py")
    results = [
        _result(f"{path}::test_a", 0, duration=1.0),
        _result(f"{path}::test_b", 1, duration=3.0),
    ]
    records = [_test_record(r.id, path, index=i) for i, r in enumerate(results)]
    reporter, stream = _reporter(records)

    # Σ = 1.0 + 3.0 = 4.0 (not printed, but drives the ratio); wall_clock = 2.0 -> 2.0x.
    reporter.finish(results, wall_clock=2.0)

    out = stream.getvalue()
    assert "2 tests · 2 passed · 2.00s wall (2.0x concurrency)" in out
    # Dropped deliberately: two numbers plus a ratio in one line was one number too many.
    assert "Σ" not in out


def test_totals_line_omits_every_category_with_nothing_to_report() -> None:
    """A clean run's one line carries what that run found and nothing else -- no zero counts,
    and no failure line above it."""
    path = Path("f.py")
    results = [_result(f"{path}::test_a", 0, duration=1.0)]
    records = [_test_record(results[0].id, path)]
    reporter, stream = _reporter(records)

    reporter.finish(results, wall_clock=1.0)

    tail = stream.getvalue().strip().splitlines()
    assert tail[-1] == "1 test · 1 passed · 1.00s wall (1.0x concurrency)"
    assert len(tail) == 1


def test_failures_get_their_own_line_above_the_totals() -> None:
    path = Path("f.py")
    results = [
        _result(f"{path}::test_a", 0, duration=1.0),
        _result(f"{path}::test_b", 1, outcome=Outcome.FAILED, duration=1.0),
        _result(f"{path}::test_c", 2, outcome=Outcome.ERROR, duration=1.0),
        _result(f"{path}::test_d", 3, outcome=Outcome.TIMEOUT, duration=1.0),
    ]
    records = [_test_record(r.id, path, index=i) for i, r in enumerate(results)]
    reporter, stream = _reporter(records)

    reporter.finish(results, wall_clock=2.0, collection_errors=1)

    tail = stream.getvalue().strip().splitlines()
    assert tail[-2] == "1 failed · 1 errored · 1 timed out · 1 collection error"
    assert tail[-1] == "4 tests · 1 passed · 2.00s wall (2.0x concurrency)"


def test_finish_folds_skipped_into_the_leading_count() -> None:
    """Skipped tests never reach `results` -- they are never run -- so the leading count reads
    them off the reporter's own `skipped`, which is also what the file blocks counted."""
    path = Path("f.py")
    results = [_result(f"{path}::test_a", 0, duration=1.0)]
    records = [_test_record(results[0].id, path)]
    reporter, stream = _reporter(
        records, skipped=[_skipped(f"{path}::test_b", path), _skipped(f"{path}::test_c", path)]
    )

    reporter.finish(results, wall_clock=1.0)

    out = stream.getvalue()
    assert "3 tests · 1 passed · 2 skipped" in out


def test_finish_folds_a_runtime_skip_into_the_same_leading_count_as_a_marked_one() -> None:
    """Unlike a `skip` mark's skip, a runtime one already ran through setup (and maybe the
    call), so it's counted off `results` rather than off `self.skipped` -- but the totals line
    reads the two as one number."""
    path = Path("f.py")
    results = [
        _result(f"{path}::test_a", 0, duration=1.0),
        _result(f"{path}::test_b", 1, outcome=Outcome.SKIPPED, failure="no backend"),
    ]
    records = [_test_record(results[0].id, path), _test_record(results[1].id, path)]
    reporter, stream = _reporter(records, skipped=[_skipped(f"{path}::test_c", path)])

    reporter.finish(results, wall_clock=1.0)

    out = stream.getvalue()
    assert "3 tests · 1 passed · 2 skipped" in out


def test_a_runtime_skip_is_not_treated_as_a_failure() -> None:
    """`Outcome.SKIPPED` never lands in `FAILING_OUTCOMES`: it gets neither failure detail nor
    a short-summary line, and doesn't push the process exit code to 1 (see `test_run.py` for
    the exit-code half)."""
    path = Path("f.py")
    result = _result(f"{path}::test_a", 0, outcome=Outcome.SKIPPED, failure="no backend")
    records = [_test_record(result.id, path)]
    reporter, stream = _reporter(records)

    reporter.finish([result], wall_clock=1.0)

    out = stream.getvalue()
    assert "no backend" not in out
    assert "short test summary" not in out


def test_finish_counts_deselected_and_not_run_tests() -> None:
    path = Path("f.py")
    results = [_result(f"{path}::test_a", 0, duration=1.0)]
    records = [_test_record(results[0].id, path)]
    reporter, stream = _reporter(records)

    reporter.finish(results, wall_clock=1.0, not_run=2, deselected=3)

    out = stream.getvalue()
    assert "3 tests · 1 passed · 3 deselected · 2 not run (--maxfail)" in out


def test_finish_with_zero_tests() -> None:
    """The shape `velox` prints on an empty directory, or any run with nothing collected."""
    reporter, stream = _reporter([])

    reporter.finish([], wall_clock=0.001)

    out = stream.getvalue()
    assert "0 tests · 0.00s wall" in out


def test_wall_vs_sigma_line_guards_non_positive_wall_clock() -> None:
    path = Path("f.py")
    results = [_result(f"{path}::test_a", 0, duration=1.0)]
    records = [_test_record(results[0].id, path)]
    reporter, stream = _reporter(records)

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
    reporter, stream = _reporter(records)

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


def test_file_block_path_column_is_elided_past_the_ceiling() -> None:
    long_path = Path(
        "tests/very/deeply/nested/package/of/an/enterprise/tree/test_billing_reconciliation.py"
    )
    records = [_test_record(f"{long_path}::test_a", long_path)]
    reporter, stream = _reporter(records)

    reporter.on_result(_result(records[0].id, 0))

    out = stream.getvalue()
    assert "..." in out
    assert str(long_path) not in out


def test_file_block_path_column_widens_to_the_longest_path_collected() -> None:
    """Sized once, from every path the run collected, so a deep tree's blocks line their
    counts up in one column instead of each pushing them right by a different amount."""
    short, long = Path("tests/test_a.py"), Path("tests/api/v2/billing/test_reconciliation.py")
    records = [_test_record(f"{short}::test_a", short), _test_record(f"{long}::test_b", long)]
    reporter, stream = _reporter(records)

    reporter.on_result(_result(records[0].id, 0))
    reporter.on_result(_result(records[1].id, 1))

    first, second = stream.getvalue().splitlines()
    assert str(long) in second  # in full: under the ceiling, nothing is elided
    assert first.index("1 test") == second.index("1 test")


# ------------------------------------------------------------------------------------------
# is_tty
# ------------------------------------------------------------------------------------------


def test_is_tty_reflects_the_streams_own_isatty() -> None:
    """`is_tty` is resolved once at construction via `stream.isatty()`, or `False` for a stream
    with no such method at all."""
    assert Reporter(records=[], capture_passthrough=False, stream=io.StringIO()).is_tty is False
    assert Reporter(records=[], capture_passthrough=False, stream=_FakeTTYStream()).is_tty is True


# ------------------------------------------------------------------------------------------
# unittest.mock solo-scheduling cost
# ------------------------------------------------------------------------------------------


def test_the_cost_of_patching_is_reported_against_the_wall_clock() -> None:
    """A suite drifting into serial should read that off the summary rather than a stopwatch:
    the line sums the tests patching forced to run alone against the whole run's wall clock."""
    path = Path("f.py")
    records = [
        _test_record(f"{path}::test_patches", path, 0, patches=("getcwd",)),
        _test_record(f"{path}::test_plain", path, 1),
    ]
    reporter, stream = _reporter(records)

    reporter.finish(
        [
            _result(f"{path}::test_patches", 0, duration=1.2),
            _result(f"{path}::test_plain", 1, duration=0.1),
        ],
        wall_clock=3.4,
    )

    out = stream.getvalue()
    assert "unittest.mock: 1 test ran solo" in out
    assert "Σ 1.20s of 3.40s wall" in out


def test_a_run_with_no_patching_says_nothing_about_it() -> None:
    path = Path("f.py")
    records = [_test_record(f"{path}::test_plain", path)]
    reporter, stream = _reporter(records)

    reporter.finish([_result(f"{path}::test_plain", 0)], wall_clock=1.0)

    assert "unittest.mock" not in stream.getvalue()


# ------------------------------------------------------------------------------------------
# Verbosity (-v / -q) and --durations
# ------------------------------------------------------------------------------------------


def test_verbose_prints_a_line_per_test_as_it_finishes() -> None:
    path = Path("tests/test_sample.py")
    records = [
        _test_record(f"{path}::test_a", path, index=0),
        _test_record(f"{path}::test_b", path, index=1),
    ]
    reporter, stream = _reporter(records, verbosity=1)

    reporter.on_result(_result(f"{path}::test_a", 0, duration=0.5))
    # Printed as it finishes, not held back until the file's block.
    assert "PASSED" in stream.getvalue()
    assert f"{path}::test_a" in stream.getvalue()

    reporter.on_result(_result(f"{path}::test_b", 1, outcome=Outcome.FAILED, duration=0.25))
    out = stream.getvalue()
    assert "FAILED" in out
    # The per-file block still lands, after both of its tests.
    assert "FAIL " in out


def test_quiet_prints_one_character_per_file() -> None:
    first, second = Path("tests/test_a.py"), Path("tests/test_b.py")
    records = [_test_record(f"{first}::test_a", first), _test_record(f"{second}::test_b", second)]
    reporter, stream = _reporter(records, verbosity=-1)

    reporter.on_result(_result(f"{first}::test_a", 0))
    reporter.on_result(_result(f"{second}::test_b", 1, outcome=Outcome.FAILED))

    assert stream.getvalue() == ".F"


def test_quiet_marks_a_wholly_skipped_file_with_an_s() -> None:
    first, second = Path("tests/test_a.py"), Path("tests/test_b.py")
    records = [_test_record(f"{first}::test_a", first)]
    reporter, stream = _reporter(
        records, skipped=[_skipped(f"{second}::test_b", second)], verbosity=-1
    )

    reporter.on_result(_result(f"{first}::test_a", 0))
    reporter.flush_pending()

    assert stream.getvalue() == ".s\n"


def test_verbose_lists_every_skip_with_its_reason() -> None:
    path = Path("tests/test_a.py")
    records = [_test_record(f"{path}::test_a", path)]
    skipped = [_skipped(f"{path}::test_b", path, reason="pagination is not implemented yet")]
    reporter, stream = _reporter(records, skipped=skipped, verbosity=1)

    reporter.finish([_result(f"{path}::test_a", 0)], wall_clock=1.0)

    out = stream.getvalue()
    assert "--- skipped 1 test ---" in out
    assert f"{path}::test_b - pagination is not implemented yet" in out


def test_verbose_lists_a_runtime_skip_with_its_reason_after_the_marked_ones() -> None:
    path = Path("tests/test_a.py")
    records = [_test_record(f"{path}::test_a", path), _test_record(f"{path}::test_b", path)]
    skipped = [_skipped(f"{path}::test_a", path, reason="pagination is not implemented yet")]
    reporter, stream = _reporter(records, skipped=skipped, verbosity=1)
    result = _result(f"{path}::test_b", 0, outcome=Outcome.SKIPPED, failure="no backend")

    reporter.finish([result], wall_clock=1.0)

    out = stream.getvalue()
    assert "--- skipped 2 tests ---" in out
    assert f"{path}::test_a - pagination is not implemented yet" in out
    assert f"{path}::test_b - no backend" in out


def test_the_default_verbosity_counts_skips_without_their_reasons() -> None:
    path = Path("tests/test_a.py")
    records = [_test_record(f"{path}::test_a", path)]
    skipped = [_skipped(f"{path}::test_b", path, reason="pagination is not implemented yet")]
    reporter, stream = _reporter(records, skipped=skipped)

    reporter.finish([_result(f"{path}::test_a", 0)], wall_clock=1.0)

    out = stream.getvalue()
    assert "pagination is not implemented yet" not in out
    assert "1 skipped" in out


def test_quiet_closes_its_progress_line_before_anything_else_prints() -> None:
    path = Path("tests/test_a.py")
    records = [_test_record(f"{path}::test_a", path)]
    reporter, stream = _reporter(records, verbosity=-1)
    reporter.on_result(_result(f"{path}::test_a", 0))

    reporter.flush_pending()

    assert stream.getvalue().splitlines()[0] == "."
    # Idempotent: a second call (finish's own) adds no further blank line.
    reporter.flush_pending()
    assert stream.getvalue() == ".\n"


def test_quiet_still_prints_failure_detail() -> None:
    path = Path("tests/test_a.py")
    records = [_test_record(f"{path}::test_a", path)]
    reporter, stream = _reporter(records, verbosity=-1)
    result = _result(
        f"{path}::test_a",
        0,
        outcome=Outcome.FAILED,
        failure=_TRACEBACK_FAILURE,
        failure_summary="AssertionError: assert 2 == 3",
    )
    reporter.on_result(result)

    reporter.finish([result], wall_clock=1.0)

    out = stream.getvalue()
    assert "assert 2 == 3" in out
    assert "--- short test summary ---" in out


def test_flush_pending_prints_the_block_of_a_file_that_never_finished() -> None:
    """`--maxfail` stops the run mid-file, so that file's block never reached its own
    completion check -- what did run is still accounted for."""
    path = Path("tests/test_sample.py")
    records = [
        _test_record(f"{path}::test_a", path, index=0),
        _test_record(f"{path}::test_b", path, index=1),
    ]
    reporter, stream = _reporter(records)
    result = _result(f"{path}::test_a", 0, outcome=Outcome.FAILED)
    reporter.on_result(result)
    assert stream.getvalue() == ""

    reporter.finish([result], wall_clock=1.0, not_run=1)

    block_line = stream.getvalue().splitlines()[0]
    assert block_line.startswith("FAIL")
    assert "1 test " in block_line


def test_finish_folds_not_run_into_the_leading_count() -> None:
    path = Path("tests/test_sample.py")
    records = [_test_record(f"{path}::test_a", path)]
    reporter, stream = _reporter(records)
    result = _result(f"{path}::test_a", 0, outcome=Outcome.FAILED)

    reporter.finish([result], wall_clock=1.0, not_run=2)

    assert "3 tests · 2 not run (--maxfail)" in stream.getvalue()


def test_durations_lists_the_slowest_tests_in_order() -> None:
    path = Path("tests/test_sample.py")
    records = [_test_record(f"{path}::test_{name}", path) for name in ("a", "b", "c")]
    reporter, stream = _reporter(records, durations=2)
    results = [
        _result(f"{path}::test_a", 0, duration=0.10),
        _result(f"{path}::test_b", 1, duration=2.50),
        _result(f"{path}::test_c", 2, duration=1.00),
    ]

    reporter.finish(results, wall_clock=3.0)

    listed = stream.getvalue().split("--- slowest 2 tests ---\n")[1].splitlines()[:2]
    assert "::test_b" in listed[0]
    assert "2.50s" in listed[0]
    assert "::test_c" in listed[1]


def test_durations_asks_for_more_than_there_are() -> None:
    path = Path("tests/test_sample.py")
    records = [_test_record(f"{path}::test_a", path)]
    reporter, stream = _reporter(records, durations=10)

    reporter.finish([_result(f"{path}::test_a", 0, duration=0.5)], wall_clock=1.0)

    assert "--- slowest 1 test ---" in stream.getvalue()


def test_no_durations_section_without_the_option() -> None:
    path = Path("tests/test_sample.py")
    records = [_test_record(f"{path}::test_a", path)]
    reporter, stream = _reporter(records)

    reporter.finish([_result(f"{path}::test_a", 0, duration=0.5)], wall_clock=1.0)

    assert "slowest" not in stream.getvalue()


# ------------------------------------------------------------------------------------------
# Color
# ------------------------------------------------------------------------------------------


def test_file_block_is_colored_on_a_tty_and_plain_otherwise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-tty stream (the common case: piped output, a captured test) gets plain text --
    the same content either way, just without escape codes wrapping it."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    path = Path("f.py")
    records = [_test_record(f"{path}::test_a", path)]

    plain_reporter, plain_stream = _reporter(records)
    plain_reporter.on_result(_result(f"{path}::test_a", 0))
    assert "\x1b[" not in plain_stream.getvalue()

    tty_stream = _FakeTTYStream()
    tty_reporter = Reporter(records=records, capture_passthrough=False, stream=tty_stream)
    tty_reporter.on_result(_result(f"{path}::test_a", 0))
    colored = tty_stream.getvalue()
    assert "\x1b[" in colored
    # The escape codes are decoration, not a rename -- strip them and the line reads the
    # same as the plain-stream case.
    assert "PASS" in colored


# ------------------------------------------------------------------------------------------
# Cancelled tests: the run stopped while they were in flight
# ------------------------------------------------------------------------------------------


def test_a_cancelled_test_is_counted_on_the_wrong_line_without_being_a_failure() -> None:
    path = Path("tests/test_sample.py")
    records = [
        _test_record(f"{path}::test_a", path, index=0),
        _test_record(f"{path}::test_b", path, index=1),
    ]
    reporter, stream = _reporter(records)
    results = [
        _result(f"{path}::test_a", 0),
        _result(f"{path}::test_b", 1, outcome=Outcome.CANCELLED, failure="cancelled: stopped"),
    ]

    reporter.finish(results, wall_clock=1.0)

    out = stream.getvalue()
    assert "1 cancelled" in out
    assert "2 tests · 1 passed" in out
    # Not a failure: no detail block, no short-summary line, nothing in `other`.
    assert "--- short test summary ---" not in out
    assert "other" not in out


def test_a_file_the_run_was_stopped_in_reads_as_stopped_not_passed() -> None:
    path = Path("tests/test_sample.py")
    records = [
        _test_record(f"{path}::test_a", path, index=0),
        _test_record(f"{path}::test_b", path, index=1),
    ]
    reporter, stream = _reporter(records)

    reporter.on_result(_result(f"{path}::test_a", 0))
    reporter.on_result(_result(f"{path}::test_b", 1, outcome=Outcome.CANCELLED))

    line = stream.getvalue()
    assert line.startswith("STOP")
    assert "(1 cancelled)" in line


def test_a_file_with_a_failure_and_a_cancellation_still_reads_as_failed() -> None:
    path = Path("tests/test_sample.py")
    records = [
        _test_record(f"{path}::test_a", path, index=0),
        _test_record(f"{path}::test_b", path, index=1),
    ]
    reporter, stream = _reporter(records)

    reporter.on_result(_result(f"{path}::test_a", 0, outcome=Outcome.FAILED))
    reporter.on_result(_result(f"{path}::test_b", 1, outcome=Outcome.CANCELLED))

    line = stream.getvalue()
    assert line.startswith("FAIL")
    assert "(1 failed)" in line
    assert "(1 cancelled)" in line


def test_the_not_run_label_names_whatever_stopped_the_run() -> None:
    path = Path("tests/test_sample.py")
    records = [_test_record(f"{path}::test_{i}", path, index=i) for i in range(3)]
    reporter, stream = _reporter(records)

    reporter.finish(
        [_result(f"{path}::test_0", 0)], wall_clock=1.0, not_run=2, not_run_label="interrupted"
    )

    assert "2 not run (interrupted)" in stream.getvalue()
