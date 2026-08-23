"""Terminal reporter: jest-style per-file scrollback blocks, failure details, and the
short test summary.

`Reporter.on_result` is wired as `_run.run_suite`'s streaming callback: a file's block
prints the moment every one of its tests has finished, in real completion order, so
output starts appearing before the whole suite is done. `Reporter.finish` runs once,
after `run_suite` returns, and prints everything that belongs in logical (collection)
order instead: failure details, the short summary, unattributed output, the slowest
tests (`--durations`), the `unittest.mock` solo-scheduling cost, and the counts the run
ends on.

`verbosity` scales what the streaming half prints, and nothing else: `-v` adds a line
per test as it finishes plus the reason behind every skip, `-q` reduces each file to one
character. Failure detail and every end-of-run section are printed at any verbosity --
what a run found is never what gets quieter.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from velox._collection.collect import Skipped, TestRecord
from velox._report import color as _color
from velox._run.run import FAILING_OUTCOMES, Outcome, TestResult, solo_for_patching

__all__ = ["Reporter"]

#: Bounds on `_print_file_block`'s path column, which is otherwise sized to the longest
#: path the run collected. The floor keeps a shallow tree's short paths from pulling the
#: columns to its right up against the status word; past the ceiling a path is elided in
#: the middle, not truncated from one end, so both the leaf filename and enough of the
#: directory prefix to disambiguate stay visible.
_PATH_COLUMN_MIN_WIDTH = 40
_PATH_COLUMN_MAX_WIDTH = 72

#: Width of the test-id column `-v` and `--durations` line their durations up against. A
#: longer id simply pushes its own duration right rather than being elided: unlike a file
#: block's path column, an id is what the reader copies back into the next `velox` invocation.
_ID_COLUMN_WIDTH = 56


@dataclass
class Reporter:
    """Streams per-file blocks as tests finish (`on_result`) and prints the
    end-of-run sections once, after `run_suite` returns (`finish`).

    `records` seeds per-file counts by walking the sequence directly, not by
    reducing it to an `{id: path}` dict. `stream` is bound once, at construction.
    """

    records: Sequence[TestRecord]
    capture_passthrough: bool
    stream: TextIO
    #: The tests a skip mark kept out of `records`. Counted into their own file's block and
    #: into the final summary, and listed with their reasons under `-v`.
    skipped: Sequence[Skipped] = ()
    #: `-v` (1) prints a line per test as it finishes on top of the per-file blocks; `-q` (-1)
    #: replaces each block with a single `.`/`F`. 0 is the default per-file block.
    verbosity: int = 0
    #: `--durations N`: how many of the slowest tests `finish` lists. 0 (the default) prints no
    #: such section at all.
    durations: int = 0

    #: Resolved once, at construction, rather than re-checked on every print.
    is_tty: bool = field(init=False)
    #: Whether printed lines get ANSI color -- `is_tty` plus the `NO_COLOR` opt-out
    #: (see `color.color_enabled`). Resolved once, alongside `is_tty`, for the same
    #: reason: a stream's tty-ness and the user's color preference don't change
    #: mid-run.
    _color_enabled: bool = field(init=False)
    #: id -> path, seeded from `records` alongside `_remaining_by_path` below.
    _path_by_id: dict[str, Path] = field(init=False, default_factory=dict)
    #: path -> tests of that file not yet seen by `on_result`, seeded from `records`
    #: up front. The file's block prints the instant its count reaches zero.
    _remaining_by_path: dict[Path, int] = field(init=False, default_factory=dict)
    #: path -> results of that file seen so far, drained (printed and popped) the
    #: moment its count reaches zero. `finish` reads the caller's own `results` list
    #: instead of this buffer.
    _buffered_by_path: dict[Path, list[TestResult]] = field(init=False, default_factory=dict)
    #: path -> how many of that file's tests a skip mark kept from running, seeded from
    #: `skipped`. Reported as part of the file's own block rather than a line per skip.
    _skipped_by_path: dict[Path, int] = field(init=False, default_factory=dict)
    #: Files with nothing but skips, which `on_result` therefore never hears about. They get
    #: their block from `flush_pending`, so a wholly skipped file is still accounted for.
    _skip_only_paths: set[Path] = field(init=False, default_factory=set)
    #: Ids of the tests `unittest.mock` patching forced to run alone, for `finish`'s cost
    #: line. Seeded from `records` alongside the per-file counts below.
    _patching_ids: set[str] = field(init=False, default_factory=set)
    #: Width of the block path column, sized to the longest path this run collected and
    #: clamped to `_PATH_COLUMN_MIN_WIDTH`/`_PATH_COLUMN_MAX_WIDTH`.
    _path_column_width: int = field(init=False, default=_PATH_COLUMN_MIN_WIDTH)
    #: Whether `-q`'s per-file characters have been written without a newline after them, so
    #: `finish` knows to close that line before printing anything of its own.
    _open_progress_line: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        """Seed per-file bookkeeping from `records` and `skipped`: an id->path lookup, each
        file's outstanding and skipped test counts, an empty results buffer, and the width
        every block's path column lines up on."""
        self.is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self._color_enabled = _color.color_enabled(self.stream)
        for record in self.records:
            self._path_by_id[record.id] = record.path
            self._remaining_by_path[record.path] = self._remaining_by_path.get(record.path, 0) + 1
            if solo_for_patching(record):
                self._patching_ids.add(record.id)
        for skip in self.skipped:
            self._skipped_by_path[skip.path] = self._skipped_by_path.get(skip.path, 0) + 1
        self._skip_only_paths = set(self._skipped_by_path) - set(self._remaining_by_path)
        # Every path is known before the first block prints, so the column can be measured
        # once here instead of widening mid-run and leaving the blocks above it ragged.
        longest = max(
            (len(str(path)) for path in (*self._remaining_by_path, *self._skipped_by_path)),
            default=0,
        )
        self._path_column_width = min(max(longest, _PATH_COLUMN_MIN_WIDTH), _PATH_COLUMN_MAX_WIDTH)

    def on_result(self, result: TestResult) -> None:
        """Wired as `_run.run_suite`'s `on_result`. Buffers `result` under its file;
        once every one of that file's tests has reported, prints its block:

            PASS  tests/api/test_users.py          12 tests   Σ 0.84s   (2 skipped)
            FAIL  tests/api/test_billing.py         8 tests   Σ 2.10s   (1 failed)

        `PASS`/`FAIL` on whether every result for that file has an outcome in
        `FAILING_OUTCOMES` -- an `XFAILED`/`XPASSED` result doesn't flip the file to
        `FAIL`, since either means the test behaved exactly as its `xfail` mark said it
        would. Duration is the sum of each test's own `duration`, labeled Σ since
        concurrent dispatch means no single wall-clock span is attributable to "the
        file". Under `-v` each result also gets its own line here, as it finishes; under
        `-q` the block collapses to one character. This body has no `await`, so it runs
        atomically with respect to every other `dispatch_one` under asyncio's
        cooperative scheduling -- nothing here needs a lock.
        """
        path = self._path_by_id[result.id]
        self._buffered_by_path.setdefault(path, []).append(result)
        self._remaining_by_path[path] -= 1
        if self.verbosity >= 1:
            self._print_test_line(result)
        if self._remaining_by_path[path] == 0:
            self._print_file_block(path, self._buffered_by_path.pop(path))

    def _print_test_line(self, result: TestResult) -> None:
        """`-v`'s line for one finished test, printed in completion order:

        PASSED    tests/api/test_users.py::test_create             0.08s
        """
        if result.outcome in FAILING_OUTCOMES:
            outcome_color = _color.RED
        elif result.outcome in (Outcome.CANCELLED, Outcome.SKIPPED):
            # Neither green nor red: the run stopped this test (CANCELLED) or the test itself
            # said to stop (SKIPPED), so neither reported a pass nor a failure.
            outcome_color = _color.YELLOW
        else:
            outcome_color = _color.GREEN
        label = _color.paint(
            f"{result.outcome.value.upper():<9}", outcome_color, enabled=self._color_enabled
        )
        duration = _color.paint(f"{result.duration:.2f}s", _color.GRAY, enabled=self._color_enabled)
        print(f"{label} {result.id:<{_ID_COLUMN_WIDTH}} {duration}", file=self.stream, flush=True)

    def _print_file_block(self, path: Path, results: list[TestResult]) -> None:
        """One scrollback line for `path`, once every one of its tests has reported
        in -- or one character under `-q`. `results` is in completion order, not logical
        order -- irrelevant here, since this only counts and sums them; logical order
        matters only once `finish` reads the caller's own `results` list.

        The count is what this run accounted for in that file -- its tests that ran, plus
        its tests a skip mark kept from running -- and `SKIP` is the status of a file with
        nothing in the first group. `STOP` is the status of a file the run was stopped in
        the middle of: nothing in it failed, but not everything in it got to answer."""
        failed = sum(1 for result in results if result.outcome in FAILING_OUTCOMES)
        cancelled = sum(1 for result in results if result.outcome is Outcome.CANCELLED)
        # A `velox.Skipped` raised mid-run, folded into the same annotation as a skip mark's:
        # both mean this file has that many fewer tests to show a pass/fail for. Unlike a
        # skip mark's, it's already counted in `results`/`collected` below, since the test
        # did run through setup (and maybe the call) before it skipped.
        runtime_skipped = sum(1 for result in results if result.outcome is Outcome.SKIPPED)
        skipped = self._skipped_by_path.get(path, 0)
        if failed:
            status, character, status_color = "FAIL", "F", _color.RED
        elif cancelled:
            status, character, status_color = "STOP", "!", _color.YELLOW
        elif results:
            status, character, status_color = "PASS", ".", _color.GREEN
        else:
            status, character, status_color = "SKIP", "s", _color.YELLOW
        if self.verbosity <= -1:
            # No newline: `-q` builds one line of characters across the whole run, which
            # `finish` closes before printing anything else.
            print(
                _color.paint(character, status_color, enabled=self._color_enabled),
                end="",
                file=self.stream,
                flush=True,
            )
            self._open_progress_line = True
            return
        # A sum, not a span: under concurrency a file's summed durations can exceed
        # the whole run's wall clock, so it's labeled Σ to avoid reading like "this
        # file took N seconds".
        duration = sum(result.duration for result in results)
        path_column = _elide_middle(str(path), self._path_column_width)
        # Padded to their column width first, colored after: an ANSI escape is
        # invisible ink to a human but not to `str.format`'s width count, so coloring
        # first would throw off every column to its right.
        status_column = _color.paint(f"{status:<5}", status_color, enabled=self._color_enabled)
        collected = len(results) + skipped
        line = (
            f"{status_column} {path_column:<{self._path_column_width}} "
            f"{collected:>4} {_plural(collected, 'test'):<5}  "
            f"{_color.paint(f'Σ {duration:.2f}s', _color.GRAY, enabled=self._color_enabled)}"
        )
        if failed:
            line += "   " + _color.paint(
                f"({failed} failed)", _color.RED, enabled=self._color_enabled
            )
        if cancelled:
            line += "   " + _color.paint(
                f"({cancelled} cancelled)", _color.YELLOW, enabled=self._color_enabled
            )
        # Only where it qualifies the count: on a wholly skipped file `SKIP` has said it.
        noted_skips = skipped + runtime_skipped
        if noted_skips and results:
            line += "   " + _color.paint(
                f"({noted_skips} skipped)", _color.YELLOW, enabled=self._color_enabled
            )
        # flush=True: a tty's stdout is line-buffered, but piped to a file or a CI log
        # collector it's block-buffered, so nothing would surface a block until the
        # whole run ended -- defeating the point of streaming per file.
        print(line, file=self.stream, flush=True)

    def flush_pending(self) -> None:
        """Close out what the streaming half left open, once `run_suite` has returned.

        A file whose tests didn't all report -- the run stopped partway through it -- never
        reached its own block in `on_result`; it prints here, in path order, so what did run
        is still accounted for file by file. A file holding nothing but skips
        prints here too, for the same reason: `on_result` never hears about it at all.
        `-q`'s line of characters gets its closing newline the same way.

        `cli.main` calls this before printing any section of its own, so nothing lands on the
        end of an unfinished progress line; `finish` calls it too, and calling it twice prints
        nothing the second time.
        """
        for path in sorted(self._buffered_by_path):
            self._print_file_block(path, self._buffered_by_path.pop(path))
        for path in sorted(self._skip_only_paths):
            self._print_file_block(path, [])
        self._skip_only_paths.clear()
        if self._open_progress_line:
            print(file=self.stream)
            self._open_progress_line = False

    def finish(
        self,
        results: list[TestResult],
        *,
        wall_clock: float,
        unattributed_output: list[str] | None = None,
        not_run: int = 0,
        not_run_label: str = "--maxfail",
        deselected: int = 0,
        collection_errors: int = 0,
    ) -> None:
        """Called once, after `_run.run_suite` returns. `results` is the caller's
        full, authoritative list, already in logical order -- not whatever
        `on_result` buffered internally. Prints, in order: failure details (one block
        per `FAILING_OUTCOMES` result, traceback plus captured sections), the short
        test summary (one line per `FAILING_OUTCOMES` result), unattributed output if
        any, the reasons behind the run's skips under `-v`, what `unittest.mock` patching
        cost in drained wall clock, and the counts the run ends on.
        `captured_stdout`/`captured_stderr` are shown only when `capture_passthrough` is
        off, since passthrough already echoed them live; `log_records` are always shown,
        since they're never echoed live. `not_run` (tests a stopped run never started),
        `not_run_label` (what stopped it: `--maxfail`, or an interruption), `deselected`
        and `collection_errors` are caller-supplied because none of them ever reaches
        `results` itself.
        """
        self.flush_pending()
        failing = [result for result in results if result.outcome in FAILING_OUTCOMES]

        if failing:
            print(file=self.stream)
            for result in failing:
                label = _color.paint(
                    result.outcome.value.upper(), _color.RED, enabled=self._color_enabled
                )
                print(f"{label} {result.id}", file=self.stream)
                if result.failure:
                    # result.failure already ends in a newline (traceback.format_exc's
                    # own convention); a bare print would add a second and leave a
                    # stray blank line.
                    print(result.failure.rstrip("\n"), file=self.stream)
                if not self.capture_passthrough and result.captured_stdout:
                    print("--- captured stdout ---", file=self.stream)
                    print(result.captured_stdout, file=self.stream)
                if not self.capture_passthrough and result.captured_stderr:
                    print("--- captured stderr ---", file=self.stream)
                    print(result.captured_stderr, file=self.stream)
                if result.log_records:
                    print("--- captured log records ---", file=self.stream)
                    for record in result.log_records:
                        print(
                            f"{record.levelname} {record.name}: {record.getMessage()}",
                            file=self.stream,
                        )

            print("--- short test summary ---", file=self.stream)
            for result in failing:
                reason = _failure_reason(result)
                label = _color.paint(
                    result.outcome.value.upper(), _color.RED, enabled=self._color_enabled
                )
                print(f"{label} {result.id} - {reason}", file=self.stream)

        if unattributed_output:
            print(file=self.stream)
            print(
                "--- unattributed output (produced outside any test's context) ---",
                file=self.stream,
            )
            for section in unattributed_output:
                print(section, file=self.stream)

        self._print_skip_reasons(results)
        self._print_durations(results)
        self._print_patching_cost(results, wall_clock=wall_clock)
        print(file=self.stream)
        self._print_counts(
            results,
            wall_clock=wall_clock,
            not_run=not_run,
            not_run_label=not_run_label,
            deselected=deselected,
            collection_errors=collection_errors,
        )
        self.stream.flush()

    def _print_counts(
        self,
        results: list[TestResult],
        *,
        wall_clock: float,
        not_run: int,
        not_run_label: str,
        deselected: int,
        collection_errors: int,
    ) -> None:
        """The counts a run ends on: everything that went wrong on its own line, then one
        line of totals.

            2 failed · 1 errored
            25 tests · 20 passed · 2 skipped · 1.77s wall (4.1x concurrency)

        A category with nothing to report is left out entirely, so the totals line of a clean
        run carries only what that run actually found and the failure line above it doesn't
        appear at all.
        """
        counted = Counter(result.outcome for result in results)
        reported = (
            Outcome.FAILED,
            Outcome.ERROR,
            Outcome.TIMEOUT,
            Outcome.CANCELLED,
            Outcome.PASSED,
            Outcome.XFAILED,
            Outcome.XPASSED,
            Outcome.SKIPPED,
        )
        # A guard, not a category: an Outcome member reaching results without a field of its
        # own below lands in `other` rather than vanishing from a total that then silently
        # stops adding up. Counted against `reported`, the outcomes this method actually
        # prints, so adding a member to `Outcome` alone is enough to trip it.
        other = len(results) - sum(counted[outcome] for outcome in reported)

        wrong = self._counts(
            (counted[Outcome.FAILED], "failed", _color.RED),
            (counted[Outcome.ERROR], "errored", _color.RED),
            (counted[Outcome.TIMEOUT], "timed out", _color.RED),
            # Yellow, on the same line as the failures: a cancelled test is not a failure,
            # but it is one more thing this run couldn't tell the reader.
            (counted[Outcome.CANCELLED], "cancelled", _color.YELLOW),
            (other, "other", _color.RED),
            (collection_errors, _plural(collection_errors, "collection error"), _color.RED),
        )
        if wrong:
            print(wrong, file=self.stream)

        # Every test collection found, whether it ran or not: skips never reach run_suite,
        # and neither do the tests a stopped run never started, so `results` alone would
        # report a smaller suite than the one the user selected.
        total = len(results) + len(self.skipped) + not_run
        totals = [
            f"{_color.paint(str(total), _color.PRIMARY, enabled=self._color_enabled)} "
            f"{_plural(total, 'test')}",
            self._counts(
                (counted[Outcome.PASSED], "passed", _color.GREEN),
                # Collection-time skips (a `skip`/`skipif` mark) and runtime ones
                # (`velox.Skipped`, mid-setup or mid-call) read as one count: both mean the
                # same thing to whoever is reading the totals line.
                (len(self.skipped) + counted[Outcome.SKIPPED], "skipped", _color.YELLOW),
                (counted[Outcome.XFAILED], "xfailed", _color.GRAY),
                (counted[Outcome.XPASSED], "xpassed", _color.YELLOW),
                # The user's own filter rather than an outcome, so it never earns an alarm
                # color however many tests it took out.
                (deselected, "deselected", _color.GRAY),
                (not_run, f"not run ({not_run_label})", _color.YELLOW),
            ),
            _color.paint(
                f"{wall_clock:.2f}s wall ({_concurrency(results, wall_clock)})",
                _color.GRAY,
                enabled=self._color_enabled,
            ),
        ]
        print(" · ".join(part for part in totals if part), file=self.stream)

    def _counts(self, *fields: tuple[int, str, str]) -> str:
        """`color.counts` against this reporter's own color setting."""
        return _color.counts(*fields, enabled=self._color_enabled)

    def _print_skip_reasons(self, results: list[TestResult]) -> None:
        """`-v`'s section for every skipped test, each with its reason -- a `skip` mark's
        (kept `results` out entirely, listed from `self.skipped`) and a `velox.Skipped` raised
        at runtime (reached setup or the call phase, listed from `results` itself) read as one
        list, in that order:

        --- skipped 2 tests ---
        tests/api/test_users.py::test_list - pagination is not implemented yet
        tests/api/test_users.py::test_create - no backend configured
        """
        runtime = [result for result in results if result.outcome is Outcome.SKIPPED]
        total = len(self.skipped) + len(runtime)
        if self.verbosity < 1 or not total:
            return
        print(file=self.stream)
        print(f"--- skipped {total} {_plural(total, 'test')} ---", file=self.stream)
        for skip in self.skipped:
            reason = _color.paint(skip.reason, _color.GRAY, enabled=self._color_enabled)
            print(f"{skip.id} - {reason}", file=self.stream)
        for result in runtime:
            reason = _color.paint(result.failure or "", _color.GRAY, enabled=self._color_enabled)
            print(f"{result.id} - {reason}", file=self.stream)

    def _print_durations(self, results: list[TestResult]) -> None:
        """`--durations N`: the N slowest tests of the run, printed only when asked for.

            --- slowest 3 tests ---
            2.41s  tests/api/test_users.py::test_create
            0.88s  tests/api/test_users.py::test_delete

        Under concurrency this is what a run is actually bound by: the wall clock can't drop
        below the slowest test, however much concurrency the rest of the suite gets, so this
        is the list to read before reaching for `--concurrency`. Ties break on logical order,
        so two equally slow tests print in the same order on every run.
        """
        if self.durations <= 0 or not results:
            return
        slowest = sorted(results, key=lambda result: (-result.duration, result.index))
        slowest = slowest[: self.durations]
        print(file=self.stream)
        print(f"--- slowest {len(slowest)} {_plural(len(slowest), 'test')} ---", file=self.stream)
        for result in slowest:
            duration = _color.paint(
                f"{result.duration:6.2f}s", _color.GRAY, enabled=self._color_enabled
            )
            print(f"{duration}  {result.id}", file=self.stream)

    def _print_patching_cost(self, results: list[TestResult], *, wall_clock: float) -> None:
        """What `unittest.mock` patching cost this run, printed only when something patched:

            unittest.mock: 2 tests ran solo · Σ 1.20s of 3.40s wall

        The sum is each solo test's own duration -- the stretch of the run during which the
        suite was drained to that one test -- against the whole run's wall clock, so a suite
        drifting into serial reads it here rather than off a stopwatch.
        """
        solo = [result for result in results if result.id in self._patching_ids]
        if not solo:
            return
        drained = sum(result.duration for result in solo)
        count = _color.paint(
            f"{len(solo)} {_plural(len(solo), 'test')} ran solo",
            _color.YELLOW,
            enabled=self._color_enabled,
        )
        cost = _color.paint(
            f"Σ {drained:.2f}s of {wall_clock:.2f}s wall", _color.GRAY, enabled=self._color_enabled
        )
        print(file=self.stream)
        print(f"unittest.mock: {count} · {cost}", file=self.stream)


def _plural(count: int, noun: str) -> str:
    """`noun`, with an `s` unless `count` is exactly one."""
    return noun if count == 1 else f"{noun}s"


def _concurrency(results: list[TestResult], wall_clock: float) -> str:
    """How many tests the run held in flight on average, as `4.1x concurrency` -- the summed
    test durations over the run's wall clock. `n/a concurrency` for a non-positive wall clock,
    which only a direct caller of `finish` can produce."""
    if wall_clock <= 0:
        return "n/a concurrency"
    return f"{sum(result.duration for result in results) / wall_clock:.1f}x concurrency"


def _elide_middle(text: str, width: int) -> str:
    """`text`, unchanged if it already fits in `width`; otherwise its middle replaced
    with `...` so both ends survive -- for a path, that keeps the leaf filename and
    enough of the leading directory to disambiguate, rather than truncating from one
    end alone, which for a deep tree can leave nothing but a repeated
    `.../test_x.py`."""
    if len(text) <= width:
        return text
    keep = max(width - 3, 0)  # 3 == len("...")
    head = keep // 2
    tail = keep - head
    return f"{text[:head]}...{text[len(text) - tail :]}" if tail else text[:width]


def _failure_reason(result: TestResult) -> str:
    """The one-line "why" for a non-`PASSED` result's short-summary entry:
    `result.failure_summary` if set, else `""`."""
    return result.failure_summary or ""
