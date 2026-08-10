"""Terminal reporter: jest-style per-file scrollback blocks, failure details, and the
short test summary.

`Reporter.on_result` is wired as `_run.run_suite`'s streaming callback: a file's block
prints the moment every one of its tests has finished, in real completion order, so
output starts appearing before the whole suite is done. `Reporter.finish` runs once,
after `run_suite` returns, and prints everything that belongs in logical (collection)
order instead: failure details, the short summary, unattributed output, and the
wall-vs-sum-of-durations concurrency line.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from velox._collect import TestRecord
from velox._run import Outcome, TestResult

__all__ = ["Reporter"]

#: Width of `_print_file_block`'s path column. A path longer than this is elided in
#: the middle, not truncated from one end, so both the leaf filename and enough of the
#: directory prefix to disambiguate stay visible.
_PATH_COLUMN_WIDTH = 32


@dataclass
class Reporter:
    """Streams per-file blocks as tests finish (`on_result`) and prints the
    end-of-run sections once, after `run_suite` returns (`finish`).

    `records` is walked directly to seed per-file counts, not reduced to an
    `{id: path}` dict first, since a factory-generated test can repeat its id across
    instances. `stream` is bound once at construction rather than read from
    `sys.stdout` lazily, since `_run.run_suite` replaces `sys.stdout` with a
    `_capture.Router` for the run's duration.
    """

    records: Sequence[TestRecord]
    capture_passthrough: bool
    stream: TextIO

    #: Resolved once, at construction, rather than re-checked on every print.
    is_tty: bool = field(init=False)
    #: id -> path, seeded from `records` alongside `_remaining_by_path` below.
    _path_by_id: dict[str, Path] = field(init=False, default_factory=dict)
    #: path -> tests of that file not yet seen by `on_result`, seeded from `records`
    #: up front. The file's block prints the instant its count reaches zero.
    _remaining_by_path: dict[Path, int] = field(init=False, default_factory=dict)
    #: path -> results of that file seen so far, drained (printed and popped) the
    #: moment its count reaches zero. `finish` reads the caller's own `results` list
    #: instead of this buffer.
    _buffered_by_path: dict[Path, list[TestResult]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        """Seed per-file bookkeeping from `records`: an id->path lookup, each file's
        outstanding test count, and an empty results buffer."""
        self.is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        for record in self.records:
            self._path_by_id[record.id] = record.path
            self._remaining_by_path[record.path] = self._remaining_by_path.get(record.path, 0) + 1

    def on_result(self, result: TestResult) -> None:
        """Wired as `_run.run_suite`'s `on_result`. Buffers `result` under its file;
        once every one of that file's tests has reported, prints its block:

            PASS  tests/api/test_users.py          12 tests   Σ 0.84s
            FAIL  tests/api/test_billing.py         8 tests   Σ 2.10s   (1 failed)

        `PASS`/`FAIL` on whether every result for that file is `Outcome.PASSED`;
        duration is the sum of each test's own `duration`, labeled Σ since concurrent
        dispatch means no single wall-clock span is attributable to "the file". This
        body has no `await`, so it runs atomically with respect to every other
        `dispatch_one` under asyncio's cooperative scheduling -- nothing here needs a
        lock.
        """
        path = self._path_by_id[result.id]
        self._buffered_by_path.setdefault(path, []).append(result)
        self._remaining_by_path[path] -= 1
        if self._remaining_by_path[path] == 0:
            self._print_file_block(path, self._buffered_by_path.pop(path))

    def _print_file_block(self, path: Path, results: list[TestResult]) -> None:
        """One scrollback line for `path`, once every one of its tests has reported
        in. `results` is in completion order, not logical order -- irrelevant here,
        since this only counts and sums them; logical order matters only once
        `finish` reads the caller's own `results` list."""
        failed = sum(1 for result in results if result.outcome is not Outcome.PASSED)
        status = "PASS" if failed == 0 else "FAIL"
        # A sum, not a span: under concurrency a file's summed durations can exceed
        # the whole run's wall clock, so it's labeled Σ to avoid reading like "this
        # file took N seconds".
        duration = sum(result.duration for result in results)
        path_column = _elide_middle(str(path), _PATH_COLUMN_WIDTH)
        line = (
            f"{status:<5} {path_column:<{_PATH_COLUMN_WIDTH}} "
            f"{len(results):>4} tests   Σ {duration:.2f}s"
        )
        if failed:
            line += f"   ({failed} failed)"
        # flush=True: a tty's stdout is line-buffered, but piped to a file or a CI log
        # collector it's block-buffered, so nothing would surface a block until the
        # whole run ended -- defeating the point of streaming per file.
        print(line, file=self.stream, flush=True)

    def finish(
        self,
        results: list[TestResult],
        *,
        wall_clock: float,
        unattributed_output: list[str] | None = None,
    ) -> None:
        """Called once, after `_run.run_suite` returns. `results` is the caller's
        full, authoritative list, already in logical order -- not whatever
        `on_result` buffered internally. Prints, in order: failure details (one block
        per non-`PASSED` result, traceback plus captured sections), the short test
        summary (one line per non-`PASSED` result), unattributed output if any, and
        the wall-vs-Σ concurrency line. `captured_stdout`/`captured_stderr` are shown
        only when `capture_passthrough` is off, since passthrough already echoed them
        live; `log_records` are always shown, since they're never echoed live.
        """
        failing = [result for result in results if result.outcome is not Outcome.PASSED]

        if failing:
            print(file=self.stream)
            for result in failing:
                print(f"{result.outcome.value.upper()} {result.id}", file=self.stream)
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
                print(f"{result.outcome.value.upper()} {result.id} - {reason}", file=self.stream)

        if unattributed_output:
            print(file=self.stream)
            print(
                "--- unattributed output (produced outside any test's context) ---",
                file=self.stream,
            )
            for section in unattributed_output:
                print(section, file=self.stream)

        total = sum(result.duration for result in results)
        if wall_clock > 0:
            concurrency = f"{total / wall_clock:.1f}x concurrency"
        else:
            # Only reachable from a direct caller of finish that passes a
            # non-positive wall_clock -- cli.main's own measurement can't land here.
            concurrency = "n/a concurrency"
        print(file=self.stream)
        print(
            f"{len(results)} tests · {len(failing)} failed · {wall_clock:.2f}s wall "
            f"(Σ {total:.2f}s, {concurrency})",
            file=self.stream,
        )
        self.stream.flush()


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
