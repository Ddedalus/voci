"""Unit tests for `velox._report.Reporter` (spec/10 §2).

Exercises `Reporter` directly against hand-built `TestResult`s and a `StringIO` stream, rather
than going through `main`/`run_suite` end to end — the same "unit-test the piece in isolation,
cover the wiring separately" split `tests/test_run.py` already uses for `_run_one`/`run_suite`
(see its own `_record` helper, mirrored here by `_test_record`).

`Reporter`'s own short-summary "reason" is now a direct read of `TestResult.failure_summary`
(`_run._summarize_exception`'s output), not a text heuristic over `TestResult.failure` — so the
"does this actually recover the right reason from a real traceback" coverage that used to live here
now lives in `tests/test_run.py`, next to the code that builds `failure_summary` in the first
place (`test_failure_summary_...`); what's left worth unit-testing here is just that `Reporter`
reads the field faithfully, which is what the tests in the "short-summary reason" section below do.
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
    reads `.id`/`.path` off each record, but it takes real `TestRecord`s now, not a lossy
    `{id: path}` dict (this session's fix for the duplicate-id undercount bug — see
    `Reporter.__post_init__`'s own docstring), so tests need to build them rather than a bare
    mapping. The rest of the fields are filler `test_run.py`'s own `_record` helper already
    establishes the pattern for."""
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
    # Summed, not spanned (concurrent dispatch has no single attributable wall-clock span for
    # "the file" -- spec/10 §2 / `on_result`'s own docstring), and labeled Σ as such.
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


def test_duplicate_ids_across_records_are_each_counted_not_collapsed() -> None:
    """Regression for the must-fix bug: `Reporter` used to seed its per-file counts from an
    `{id: path}` dict built by `cli.py`, which silently collapsed two records sharing an id into
    one entry -- reachable with a completely ordinary suite, since a factory-generated test repeats
    its `func.__qualname__` (hence its id, `f"{path}::{qualname}"`) for every instance it produces.
    Undercounting used to make the file's block flush one test early, with the wrong verdict, and
    silently drop the remaining result from the scrollback entirely."""
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
    """Documents the invariant `on_result`'s own docstring states rather than silently relies on:
    a result whose id was never in the `records` `Reporter` was constructed with is a caller bug
    (the caller must hand `Reporter` the same list it dispatches to `run_suite`), and it surfaces
    loudly as a `KeyError` -- which, since `run_suite` never catches what `on_result` raises,
    aborts the whole run rather than silently dropping one result. Unreachable from `cli.main` as
    wired (see `Reporter`'s own docstring), but real for a caller that gets the invariant wrong."""
    reporter = Reporter(records=[], capture_passthrough=False, stream=io.StringIO())
    with pytest.raises(KeyError):
        reporter.on_result(_result("nope.py::test_x", 0))


def test_blocks_flush_on_a_files_last_test_not_its_first() -> None:
    """A single test per file can't tell "flush on last test" apart from "flush on first" or
    "flush in arrival order" -- all three coincide when there is only one test to see. `file_a` has
    two tests, `file_b` has one; `file_a`'s first test finishes earliest, `file_b`'s only test next,
    and `file_a`'s second (and last) test finishes last. Correct output is `file_b`'s block, then
    `file_a`'s -- an implementation that flushed on a file's first-seen test instead would print
    `file_a` first, the moment its first result arrives."""
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
    """`finish` must order by `results`'s own position, not by re-deriving an order from the data
    (an id sort, an outcome grouping, ...) -- ids are chosen here so that alphabetical order is the
    *opposite* of the intended (index) order, so a `finish` that accidentally sorted by id instead
    of trusting its argument would fail this even though it would pass a naively-chosen id pair."""
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


# ------------------------------------------------------------------------------------------
# Short-summary "reason" extraction -- a direct field read (see module docstring for why the
# "does this recover the right reason from real traceback text" coverage lives in test_run.py now)
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
    """The shape `velox` on an empty directory (or any run with nothing collected) produces --
    `finish([], ...)` was uncovered anywhere in this file before."""
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
    """The guard's justification (`finish`'s own docstring) is that a real run -- even an empty or
    very fast one -- still doesn't land on the `n/a` branch in practice; pin that a small-but-
    positive `wall_clock` really does take the other branch, not merely that zero doesn't crash."""
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
    """Nothing branches on `is_tty` yet (module docstring's "no color" cut), but the value itself
    should still be right: resolved once at construction via `stream.isatty()`, or `False` for a
    stream with no such method at all (the class docstring's `getattr` fallback)."""
    assert Reporter(records=[], capture_passthrough=False, stream=io.StringIO()).is_tty is False
    assert Reporter(records=[], capture_passthrough=False, stream=_FakeTTYStream()).is_tty is True
