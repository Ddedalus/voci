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
  downstream of `_run_one` has access to once its text is all `TestResult` carries. The one piece
  of structure this slice *does* add is `TestResult.failure_summary` — a short, one-line exception
  summary captured directly at `_run_one`'s catch sites (see its own docstring) rather than parsed
  back out of `failure`'s rendered text, which is what makes the short-summary section below
  actually reliable instead of a text-parsing heuristic.
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
- **A block for a file whose tests are all skipped, or which failed to import.** `paths_by_id`
  today only ever contains ids for records `run_suite` actually dispatches (`cli.py` builds it
  from `collected.records`), so a file with no dispatched test never has an entry, never reaches
  `_remaining_by_path`, and simply has no block — it is not deferred to the end, it is absent from
  the scrollback entirely, while its `SKIPPED`/`COLLECTION ERROR` lines print later, inline in
  `cli.py`. `Outcome` has no `skipped` member yet (spec/05 §4), so there is nothing to build a
  `SKIP`-verdict block out of this slice; a real fix needs that member to exist first.
- **Coordinating block lines with `-s`/`--capture=no` passthrough output.** Both write to the same
  real stdout (`Reporter.stream` and `_capture.Router`'s passthrough echo are, by construction,
  the same underlying stream object — see `Reporter`'s own docstring on why `stream` is bound
  eagerly), but neither knows about the other: `Router` tracks per-`Sink` line-start state for its
  own id-prefixing, and a block line printed here mid-write can land in the middle of a test's
  still-open, not-yet-newline-terminated line, corrupting it and desynchronizing the next prefix.
  This is new surface introduced by this slice (before it, the reporter printed nothing at all
  while tests were still running); a real fix means either asking `Router` to break the current
  line first or routing block lines through the same prefixing writer so both share line-start
  state, and it is a coordination change to `_capture.py`'s write path, not a `_report.py`-local
  one, so it is named here rather than attempted.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from velox._collect import TestRecord
from velox._run import Outcome, TestResult

__all__ = ["Reporter"]

#: Width of `_print_file_block`'s path column (spec/10 §2's own example is aligned to a fixed
#: column). A path longer than this is elided in the middle, not truncated from one end — see
#: `_elide_middle` — so both the (usually more identifying) leaf filename and enough of the
#: directory prefix to disambiguate stay visible.
_PATH_COLUMN_WIDTH = 32


@dataclass
class Reporter:
    """Owns the two things spec/10 §2 splits the reporter into: streamed per-file blocks (via
    `on_result`, wired as `_run.run_suite`'s callback) and the end-of-run sections (`finish`,
    called once after `run_suite` returns).

    `records` is every `TestRecord` this run will ever dispatch — the same `collected.records`
    list the caller already has before calling `run_suite`, handed here directly (not reduced to
    an `{id: path}` dict first, the earlier shape this constructor took: a dict comprehension keyed
    by `record.id` silently collapses two records sharing an id, and a factory-generated test
    repeats its `func.__qualname__` — hence its id, spec/03 §1's `f"{path}::{qualname}"` — for
    every instance it produces, so that collapse is reachable with a completely ordinary suite, not
    an edge case). `TestResult` itself carries no path (module docstring: `_run_one`/`dispatch_one`
    know nothing about files as a grouping concept, only `cli.py` and this module do), so `records`
    is also what seeds each file's expected test count — the id→path *lookup* built from it could
    still have been a plain dict without incident (colliding ids share the same file by
    construction, so last-write-wins there is harmless), but the *count* must come from iterating
    `records` itself, one increment per record, precisely because a dict cannot represent "two
    records, same id" at all.

    `stream` is written to directly (`print(..., file=self.stream)`), not through `sys.stdout` —
    same reasoning `_run.py`'s own `real_stderr` plumbing already uses elsewhere in this codebase:
    a reporter that resolved `sys.stdout` lazily at print time would risk writing into whatever
    `_capture.install()` has that name bound to at the moment, rather than the real stream the user
    is actually watching. Concretely, this is not a style preference: `cli.py` constructs `Reporter`
    with `stream=sys.stdout` *before* calling `run_suite`, while `sys.stdout` is still the real
    stream; by the time `on_result` actually fires, `run_suite`'s `_capture.install()` has already
    rebound the *name* `sys.stdout` to a `Router`, and `dispatch_one` has already reset
    `current_test_context` for this test — so a lazily-resolved `print(line)` inside `on_result`
    would reach the `Router` with no test context set, i.e. land in the **session sink**, and every
    per-file scrollback block would silently resurface at the end of the run inside "unattributed
    output" with nothing about the symptom pointing at the reporter. Binding `stream` once, at
    construction, is what avoids that.

    `is_tty` is resolved once, here, at construction time via `stream.isatty()` — or, for a stream
    with no `isatty` method at all (a plain `io.StringIO`, say), treated as non-tty rather than
    raising (a `getattr(..., lambda: False)` fallback, not a bare attribute access) — rather than
    re-checked on every print. Same "resolved once, not re-checked every print" shape as
    `_rewrite.plan`'s cold-start probing elsewhere in this codebase. Nothing branches on it yet
    (module docstring's "no color" cut), but the flag belongs on the object from the start so a
    later color pass is additive rather than a constructor-signature change.
    """

    records: Sequence[TestRecord]
    capture_passthrough: bool
    stream: TextIO

    #: `False` for `Outcome.PASSED`, `True` otherwise. Resolved once per file, not per print — see
    #: `is_tty` above for why fields the dataclass didn't already declare get the same treatment:
    #: computed in `__post_init__`, not left as bare instance attributes assigned ad hoc inside
    #: `on_result`, so every field this object carries is visible in one place.
    is_tty: bool = field(init=False)
    #: `id -> path`, seeded from `records` alongside `_remaining_by_path` below. A plain dict is
    #: fine for this half specifically (unlike the count) — two records sharing an id share the
    #: same file by construction (an id embeds its path, spec/03 §1), so last-write-wins here loses
    #: no information; see the class docstring for why the *count* below cannot be seeded the same
    #: way.
    _path_by_id: dict[str, Path] = field(init=False, default_factory=dict)
    #: `path -> tests of that file not yet seen by on_result`, seeded from `records` up front
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
        """Seed per-file bookkeeping from `records`: an id→path lookup, how many of each file's
        tests are still outstanding (one increment per record — see the class docstring for why
        this must walk `records` itself rather than count a dict's values), and an empty results
        buffer per file, filled in by `on_result` as each test completes."""
        self.is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        for record in self.records:
            self._path_by_id[record.id] = record.path
            self._remaining_by_path[record.path] = self._remaining_by_path.get(record.path, 0) + 1

    def on_result(self, result: TestResult) -> None:
        """Wired as `_run.run_suite`'s `on_result` (see that function's own docstring for the
        completion-order guarantee this relies on). Buffers `result` under its file; once that
        file's count reaches zero, prints its block — spec/10 §2's example shape:

            PASS  tests/api/test_users.py          12 tests   Σ 0.84s
            FAIL  tests/api/test_billing.py         8 tests   Σ 2.10s   (1 failed)

        `PASS`/`FAIL` on whether every one of that file's results is `Outcome.PASSED`; the file's
        reported duration is the sum of its tests' own `duration` (concurrent dispatch means no
        single wall-clock span is attributable to "the file" the way serial pytest could claim
        one — see `_print_file_block` for why that sum is labeled `Σ`); the trailing `(N failed)`
        only when `N > 0`. Nothing about *why* a test failed is printed here — that is `finish`'s
        failure-details section, not this one; a block is a one-line-per-file roll-up only
        (spec/10 §2: "atomic per file").

        This body has no `await`, so under asyncio's cooperative single-threaded scheduling it runs
        to completion atomically with respect to every other `dispatch_one` — the dict lookup, the
        append, the decrement and the flush cannot interleave with a sibling's, even for two tests
        from different files finishing back to back with no suspension between them (exactly what
        happens for a batch of trivially fast tests). Nothing here needs a lock for that reason.
        Separately, `_remaining_by_path` and `run_suite`'s own `remaining_by_module` counter stay
        coherent despite being independent counters over related partitions, decremented in
        different places: in `dispatch_one` there is no `await` between `_run_one` returning and
        this callback firing *except* on the one path where `remaining_by_module[path]` hit zero
        and the module-scope flush is awaited — and only the module's last-finishing test can take
        that path, by which point every sibling has already run this callback synchronously. So the
        zero-crossings of the two counters always happen in the same task, module-flush first,
        block second: a module fixture's teardown output is always folded into a test's
        `captured_stdout` before that file's block can print it, never after.

        `self._path_by_id[result.id]` is a bare subscript, and `run_suite` deliberately does not
        catch what `on_result` raises (its own docstring's "a reporter callback that raises aborts
        the run") — a `KeyError` here would therefore not merely degrade one file's block, it would
        abort the entire run through the `TaskGroup`, discarding every result collected so far. That
        is safe only because the invariant `Reporter` relies on genuinely holds: `cli.main` builds
        `records` from the exact same `collected.records` list it then dispatches to `run_suite`, so
        every id `on_result` is ever called with is a key here by construction. A caller that
        constructs `Reporter` from a different, narrower `records` list than the one it actually
        dispatches would violate that invariant — which is why it is stated here explicitly rather
        than silently relied on.
        """
        path = self._path_by_id[result.id]
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
        # A Σ, and labeled as one: under concurrency a file's summed durations routinely exceed the
        # *entire run's* wall clock, so an unlabeled number here reads, to anyone who has used
        # pytest or jest, as "this file took N seconds" — it is not a span, it is a sum, same
        # reasoning as `finish`'s own wall-vs-Σ line a few sections later. Related, and not fixed
        # here: this sum is `_run_one`'s own `duration` measurement, taken before `dispatch_one`
        # awaits that file's module-scope fixture flush (if it is the module's last file) — a file
        # whose module fixtures are slow to tear down under-reports by exactly that amount. Fixing
        # that means changing what `_run_one`/`dispatch_one` measure as a test's `duration`, which
        # is a `_run.py` change, not a `_report.py`-local one, so it is named rather than patched
        # here.
        duration = sum(result.duration for result in results)
        path_column = _elide_middle(str(path), _PATH_COLUMN_WIDTH)
        line = (
            f"{status:<5} {path_column:<{_PATH_COLUMN_WIDTH}} "
            f"{len(results):>4} tests   Σ {duration:.2f}s"
        )
        if failed:
            line += f"   ({failed} failed)"
        # `flush=True`: without it, nothing ever flushes `self.stream`, which silently defeats the
        # entire point of streaming this per file instead of buffering the whole run. A tty's stdout
        # is line-buffered so this made no visible difference there, but piped to a file or a CI log
        # collector it is *block*-buffered (typically 8 KiB) — every block would sit unread until
        # the process exits, which is exactly the "one line per file as it completes" behavior
        # spec/10 §2 specifies for non-tty mode, defeated.
        print(line, file=self.stream, flush=True)

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
           test_refund - AssertionError: ...`. `reason` is `result.failure_summary` (see
           `TestResult`'s own docstring for how `_run_one` builds it) — a direct field read, not a
           heuristic over `failure`'s rendered text; see `_failure_reason` below for why that
           distinction is load-bearing.
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
           itself — `cli.py` wraps the call in `time.monotonic()`; see `cli.main`'s own comment on
           what that measurement does and does not include); Σ is `sum(r.duration for r in
           results)`; the ratio is Σ / wall_clock, guarded against `wall_clock <= 0` — reachable
           only from a direct caller that measured nothing (or something nonsensical) itself, not
           from `cli.main`: `wall_clock` there is a difference of two `time.monotonic()` readings
           (never negative, by the stdlib's own guarantee) around a call that always does real work
           (`_capture.install()`'s `mkdir`/marker write/retention sweep alone measures in the
           low-single-digit milliseconds even for an empty suite), so it can't actually land here in
           practice — the guard exists for `finish`'s own callers other than `cli.main`, since
           `finish` is a public method. `failed` counts every non-`PASSED` outcome (matches the
           failure-details/short-summary sections above, not `_run.py`'s private
           `_FAILING_OUTCOMES` — this module intentionally doesn't import that name; see `Outcome`
           import above, its own enum is public).

        `self.stream.flush()` at the very end: `_print_file_block` above flushes every block as it
        prints (its own docstring), and this method's own output is the true last thing `cli.main`
        prints (see its call site's comment for the ordering that makes that actually true) — one
        explicit flush here, rather than `flush=True` on every `print` call in this method, is
        cheaper and just as effective since nothing here needs to be visible *before* the method
        returns, only by the time it does.
        """
        failing = [result for result in results if result.outcome is not Outcome.PASSED]

        if failing:
            print(file=self.stream)
            for result in failing:
                print(f"{result.outcome.value.upper()} {result.id}", file=self.stream)
                if result.failure:
                    # `.rstrip("\n")`: `result.failure` is `traceback.format_exc()` text, which
                    # already ends in a newline, so a bare `print` would add a second and leave a
                    # stray blank line whose presence depended on what `format_exc` happened to end
                    # with rather than on this method's own layout (a `TIMEOUT`'s bare, no-trailing-
                    # newline budget message wouldn't get one at all). Stripped here and not at the
                    # source (`_run_one`) since `failure` is also rendered as-is elsewhere (this is
                    # the one call site that prints it standalone, needing its own line boundary).
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
            # Reachable only from a direct caller of `finish` that passes a non-positive
            # `wall_clock` — not from `cli.main` (see this method's own docstring's point 4 for why
            # an empty or fast suite still can't land here in practice). Kept anyway: `finish` is a
            # public method, the guard is free, and printing a fabricated ratio for a measurement
            # that was never taken is a worse failure than naming that plainly.
            concurrency = "n/a concurrency"
        print(file=self.stream)
        print(
            f"{len(results)} tests · {len(failing)} failed · {wall_clock:.2f}s wall "
            f"(Σ {total:.2f}s, {concurrency})",
            file=self.stream,
        )
        self.stream.flush()


def _elide_middle(text: str, width: int) -> str:
    """`text`, unchanged if it already fits in `width`; otherwise its middle replaced with `...`
    so both ends survive — for a path, that keeps the (usually more identifying) leaf filename and
    enough of the leading directory to disambiguate, rather than pytest/shell-style truncation from
    one end alone, which for a deep tree can leave nothing but a repeated `.../test_x.py` prefix
    that all looks the same. ASCII `...`, not a single-character ellipsis glyph — same
    ambiguous-confusable reasoning `finish`'s own docstring gives for ASCII `x` over the literal
    multiplication-sign glyph (ruff RUF001/RUF002)."""
    if len(text) <= width:
        return text
    keep = max(width - 3, 0)  # 3 == len("...")
    head = keep // 2
    tail = keep - head
    return f"{text[:head]}...{text[len(text) - tail :]}" if tail else text[:width]


def _failure_reason(result: TestResult) -> str:
    """The one-line "why" for a non-`PASSED` result's short-summary entry: `result.failure_summary`
    if it's set, else `""`.

    This used to be a text heuristic over `result.failure` ("the last non-blank line is
    `ExceptionType: message`"), which was wrong for most failures velox actually produces: a
    rewritten `assert x == y, "message"`'s own message is followed by the rewriter's explanation
    (so the *last* line was the explanation, not the message — spec/07's vendored rewriter, see
    `_vendor/assertion/rewrite.py`'s `visit_Assert`), and `_di._release_all` raises an
    `ExceptionGroup`/`BaseExceptionGroup` on *every* teardown failure by design (spec/04 §5), whose
    rendered traceback ends in a box-drawing closing rule, not a message — so every `Outcome.ERROR`
    from teardown produced the same useless row of dashes. `TestResult.failure_summary` (this
    session) fixes this at the root by capturing the summary from the exception object directly, at
    each of `_run_one`'s catch sites, instead of trying to recover it from `failure`'s rendered
    text after the fact — see that field's own docstring for the exact rule, and
    `_run._summarize_exception` for why reading the object avoids both failure modes above by
    construction. `failure_summary` is `None` only for `Outcome.PASSED` (never reached from
    `Reporter.finish`, which only calls this for non-`PASSED` results); `""` is returned rather than
    `None` propagating into the printed line if it somehow were.
    """
    return result.failure_summary or ""
