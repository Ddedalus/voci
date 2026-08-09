"""Terminal reporter: jest-style per-file scrollback blocks, failure details, and the short test
summary (spec/10 §2). This is the piece spec/00 §7's MVP row calls out by name — "Reporter: tty
blocks, non-tty mode, short summary" — as distinct from the same row's *Deferred* column: live
footer, `--durations`, JUnit XML, `--report-json`, GH annotations, `--stream-failures`. None of
those are built here; see "Deliberately out of scope this slice" below for the specifics and why.

Before this module, `cli.main` printed one flat per-test line as `run_suite` returned its whole
`list[TestResult]` — no grouping, no ordering guarantee visible to a human scrolling a large run,
captured output interleaved with pass lines instead of confined to failures. This module replaces
that loop with spec/10 §2's model: **scrollback is atomic per file** (a file's block prints only
once every one of its tests has finished, so it is never interleaved with another file's output),
**ordering between blocks is completion order** (a fast file's block appears before a slow earlier
file's, Q20's proposed default), and **ordering within a block, and of everything printed after
the run, is logical order** (I2) — same collection order, byte-identical modulo timings and paths
across two runs of the same test set.

Streaming is what makes "flush the instant a file is done" possible without buffering the whole
suite: `_run.run_suite` gained an `on_result` callback (this slice, see its own docstring) invoked
once per test, synchronously, in real completion order, as each test actually finishes — distinct
from the `results` list `run_suite` still returns, which stays indexed by logical order regardless
of completion timing. `Reporter.on_result` is wired as that callback; `Reporter.finish` runs once,
after `run_suite` has returned the complete `results` list, and owns everything spec/10 §2 says
belongs "at the end, in logical order": failure details (one block per failing test, traceback +
captured sections) and the short test summary line.

Deliberately out of scope this slice (each is its own future session, named so this doesn't read
as an oversight):

- **`FailureRepr` / structured traceback rendering** (spec/10 §1). `TestResult.failure` is already
  a flat, pre-formatted string — `traceback.format_exc()` text, built by `_run._run_one` — and this
  module renders that text verbatim rather than re-parsing it into `FrameRepr`s. No
  `__tracebackhide__`, no cut-to-test-function, no runner-frame suppression, no rewriter-temp
  filtering yet; those all require walking real `TracebackException` frame objects, which nothing
  downstream of `_run_one` has access to once its text is all `TestResult` carries.
- **Live footer** (spec/10 §2, spec/00's MVP table marks it *Deferred*). No ~10 Hz redraw, no
  in-flight test list. `on_result` already gives a future footer everything timing-wise it would
  need; it is simply not built here.
- **`rich` / ANSI color output** (spec/10 §5, Q21). Both tty and non-tty modes render identical
  plain text for now, so `NO_COLOR`/`FORCE_COLOR`/`CI` (spec/10 §2) have nothing to select between
  yet — there is no color to suppress or force. `Reporter.is_tty` is still resolved once at
  construction (see `Reporter`'s own docstring) so a later color pass is additive rather than a
  constructor-signature change, but nothing branches on it yet.
- **Solo/unattributed/teardown-error sections beyond what already existed** (spec/10 §3). The
  solo-tier scheduler fact and module-teardown-to-one-test attribution these need don't exist yet
  (spec/06, M2). `unattributed_output` (spec/09 §9) already existed before this slice and keeps
  its existing rendering, just moved under this module instead of living inline in `cli.py`.
- **`--durations`, JUnit XML, `--report-json`, GH annotations** (spec/10 §4, roadmap §7). All
  spec/00 MVP-table *Deferred*.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from velox._run import Outcome, TestResult

__all__ = ["Reporter"]


@dataclass
class Reporter:
    """Owns the two things spec/10 §2 splits the reporter into: streamed per-file blocks (via
    `on_result`, wired as `_run.run_suite`'s callback) and the end-of-run sections (`finish`,
    called once after `run_suite` returns).

    `paths_by_id` maps every `TestResult.id` this run will ever produce to the file it belongs to
    (`TestRecord.path`) — built by the caller from the same `collected.records` list it already
    has before calling `run_suite`, since `TestResult` itself carries no path (module docstring:
    `_run_one`/`dispatch_one` know nothing about files as a grouping concept, only `cli.py` and
    this module do). This is also what seeds each file's expected test count, so `on_result` knows
    when a file's *last* test has finished without `_run.py` having to track or expose that itself.

    `stream` is written to directly (`print(..., file=self.stream)`), not through `sys.stdout` —
    same reasoning `_run.py`'s own `real_stderr` plumbing already uses elsewhere in this codebase:
    a reporter that resolved `sys.stdout` lazily at print time would risk writing into whatever
    `_capture.install()` has that name bound to at the moment, rather than the real stream the user
    is actually watching.

    `is_tty` is resolved once, here, at construction time (`stream.isatty()`) rather than
    re-checked on every print — same "resolved once, not re-checked every print" shape as
    `_rewrite.plan`'s cold-start probing elsewhere in this codebase. Nothing branches on it yet
    (module docstring's "no color" cut), but the flag belongs on the object from the start so a
    later color pass is additive rather than a constructor-signature change.
    """

    paths_by_id: Mapping[str, Path]
    capture_passthrough: bool
    stream: TextIO

    #: `False` for `Outcome.PASSED`, `True` otherwise. Resolved once per file, not per print — see
    #: `is_tty` above for why fields the dataclass didn't already declare get the same treatment:
    #: computed in `__post_init__`, not left as bare instance attributes assigned ad hoc inside
    #: `on_result`, so every field this object carries is visible in one place.
    is_tty: bool = field(init=False)
    #: `path -> tests of that file not yet seen by on_result`, seeded from `paths_by_id` up front
    #: (spec/10 §2: "flushed when all of that file's tests have finished" needs to know the total
    #: before any test of that file has actually run). Decremented as `on_result` sees each test;
    #: the file's block prints the instant a path's count reaches zero.
    _remaining_by_path: dict[Path, int] = field(init=False, default_factory=dict)
    #: `path -> results of that file seen so far`, drained (printed and popped) the same moment
    #: `_remaining_by_path[path]` reaches zero. Not read back by `finish` — that method uses the
    #: caller's own authoritative `results` list instead (see its docstring) — so this buffer's
    #: only job is deciding *when* to flush a block, never *what* the end-of-run sections say.
    _buffered_by_path: dict[Path, list[TestResult]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        """Seed per-file bookkeeping from `paths_by_id`: how many of each file's tests are still
        outstanding (a plain occurrence count over `paths_by_id.values()` — one entry per test id,
        so counting values counts tests per file) and an empty results buffer per file, filled in
        by `on_result` as each test completes."""
        self.is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        for path in self.paths_by_id.values():
            self._remaining_by_path[path] = self._remaining_by_path.get(path, 0) + 1

    def on_result(self, result: TestResult) -> None:
        """Wired as `_run.run_suite`'s `on_result` (see that function's own docstring for the
        completion-order guarantee this relies on). Buffers `result` under its file; once that
        file's count reaches zero, prints its block — spec/10 §2's example shape:

            PASS  tests/api/test_users.py          12 tests   0.84s
            FAIL  tests/api/test_billing.py         8 tests   2.10s   (1 failed)

        `PASS`/`FAIL` on whether every one of that file's results is `Outcome.PASSED`; the file's
        reported duration is the sum of its tests' own `duration` (concurrent dispatch means no
        single wall-clock span is attributable to "the file" the way serial pytest could claim
        one); the trailing `(N failed)` only when `N > 0`. Nothing about *why* a test failed is
        printed here — that is `finish`'s failure-details section, not this one; a block is a
        one-line-per-file roll-up only (spec/10 §2: "atomic per file").
        """
        path = self.paths_by_id[result.id]
        self._buffered_by_path.setdefault(path, []).append(result)
        self._remaining_by_path[path] -= 1
        if self._remaining_by_path[path] == 0:
            self._print_file_block(path, self._buffered_by_path.pop(path))

    def _print_file_block(self, path: Path, results: list[TestResult]) -> None:
        """One scrollback line for `path`, once every one of its tests has reported in. `results`
        is in completion order here (whatever order `on_result` happened to see them), not logical
        order — irrelevant for this roll-up, since nothing here reads individual results beyond
        counting and summing them; logical order only matters once `finish` gets to the failure
        details and short summary, which read from the caller's own `results` list instead of this
        buffer (see the class docstring's `_buffered_by_path` note)."""
        failed = sum(1 for result in results if result.outcome is not Outcome.PASSED)
        status = "PASS" if failed == 0 else "FAIL"
        duration = sum(result.duration for result in results)
        line = f"{status:<5} {path!s:<32} {len(results):>4} tests   {duration:.2f}s"
        if failed:
            line += f"   ({failed} failed)"
        print(line, file=self.stream)

    def finish(
        self,
        results: list[TestResult],
        *,
        wall_clock: float,
        unattributed_output: list[str] | None = None,
    ) -> None:
        """Called once, after `_run.run_suite` has returned. `results` is already in logical order
        (I2) — this is the caller's full, authoritative list, not whatever `on_result` buffered
        internally (streaming state above exists only to decide *when* to flush a block, not to be
        read back from). `unattributed_output` (spec/09 §9) is the same out-parameter list
        `cli.main` already builds and hands to `run_suite`; rendering it here (rather than inline
        in `cli.py`, where it lived before this slice) keeps every "what got printed and in what
        order" decision in one place. In this order, per spec/10 §2 "At the end, in logical order"
        plus §3's unattributed-output mention:

        1. **Failure details** — one block per non-`PASSED` result, in `results` order: the test
           id, its `outcome`, `result.failure` rendered verbatim (module docstring: no frame
           parsing this slice), then captured sections gated exactly like the code this replaces
           (`cli.py`, pre-this-slice) already did: `captured_stdout`/`captured_stderr` only when
           `not self.capture_passthrough` (passthrough already echoed them live, per line, as
           `-s`'s whole point — printing them again here would double it), `log_records`
           unconditionally (never echoed live, spec/09 §1). Skip a section that's empty, same as
           before.
        2. **Short test summary** — one line per non-`PASSED` result, pytest's kept-verbatim shape
           (spec/10 §2): `f"{OUTCOME} {id} - {reason}"`, e.g. `FAILED tests/api/test_billing.py::
           test_refund - AssertionError: ...`. `reason` comes from `_failure_reason` below.
        3. **Unattributed output** (spec/09 §9, spec/10 §3) — if `unattributed_output` is
           non-empty, a section naming it as such (the module docstring's own wording, "produced
           outside any test's context", is fine to reuse) followed by each of its entries. Skipped
           entirely when empty or `None` — this is the same "only sections with something to show"
           policy every other part of this method follows.
        4. **Wall-vs-Σ final line** — spec/10 §2's proof-of-value metric, e.g. `1204 tests · 42
           failed · 18.4s wall (Σ 214.7s, 11.7x concurrency)` — ASCII `x`, not spec/10's literal
           multiplication-sign glyph (ruff's RUF001/RUF002 flag it as an ambiguous confusable, and
           this codebase has no allowed-confusables override for it). `wall_clock` is the
           caller-measured real elapsed time of the `run_suite` call (this module can't measure it
           itself — `cli.py` wraps the call in `time.monotonic()`); Σ is `sum(r.duration for r in
           results)`; the ratio is Σ / wall_clock, guarded against `wall_clock == 0` (a suite with
           no tests, or a caller passing a bogus measurement, would otherwise raise
           `ZeroDivisionError` out of the reporter itself, which is a worse failure than just
           omitting the ratio). `failed` counts every non-`PASSED` outcome (matches the
           failure-details/short-summary sections above, not `_run.py`'s private
           `_FAILING_OUTCOMES` — this module intentionally doesn't import that name; see `Outcome`
           import above, its own enum is public).
        """
        failing = [result for result in results if result.outcome is not Outcome.PASSED]

        if failing:
            print(file=self.stream)
            for result in failing:
                print(f"{result.outcome.value.upper()} {result.id}", file=self.stream)
                if result.failure:
                    print(result.failure, file=self.stream)
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
            # No honest ratio to report (a zero or negative measurement — an empty suite, or a
            # caller passing a bogus `wall_clock`) — say so rather than raise or print a fabricated
            # number.
            concurrency = "n/a concurrency"
        print(file=self.stream)
        print(
            f"{len(results)} tests · {len(failing)} failed · {wall_clock:.2f}s wall "
            f"(Σ {total:.2f}s, {concurrency})",
            file=self.stream,
        )


def _failure_reason(result: TestResult) -> str:
    """The one-line "why" for a non-`PASSED` result's short-summary entry — a small, directly
    unit-testable helper (see `Reporter.finish`'s docstring) because `TestResult.failure` is free
    text, not a structured type this slice builds (module docstring), and its shape depends on the
    outcome:

    - `Outcome.TIMEOUT`'s `failure` is `_run_one`'s own synthesized `"test exceeded the
      --timeout=Ns budget"`, optionally followed by a blank line and a traceback (see
      `_run_one`'s docstring) — the reason is the message itself, i.e. the *first* line.
    - `Outcome.FAILED`/`Outcome.ERROR`'s `failure` is a real `traceback.format_exc()` string (or,
      for `ERROR`, possibly two concatenated — see `_run_one`'s aggregation rule), which always
      ends with `ExceptionType: message` — the reason is that *last* non-blank line.

    `failure` is `None` only for `Outcome.PASSED` (`TestResult`'s own docstring); this function is
    never called for that outcome (`finish` only calls it from the `failing` list), but an empty
    string is returned rather than raising if it somehow were, since a missing reason is a worse
    failure mode for a reporter than an unhelpful one.
    """
    failure = result.failure or ""
    if not failure:
        return ""
    lines = failure.splitlines()
    if result.outcome is Outcome.TIMEOUT:
        return lines[0].strip()
    for line in reversed(lines):
        if line.strip():
            return line.strip()
    return ""
