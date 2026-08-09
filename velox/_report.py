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

    # Review (good, and worth stating because the reasoning is load-bearing and non-obvious):
    # binding `stream` eagerly, at construction, is not merely a style choice here -- resolving
    # `sys.stdout` lazily inside `on_result` would have been silently, spectacularly wrong. By the
    # time `on_result` fires, `run_suite`'s `_capture.install()` has rebound `sys.stdout` to a
    # `Router`, *and* `dispatch_one` has already called `current_test_context.reset(token)` a few
    # lines earlier -- so a lazily-resolved `print(line)` would have gone to the Router with no
    # test context set, i.e. straight into the **session sink**, and every per-file scrollback
    # block would have come back out at the end of the run inside the "unattributed output"
    # section. Nothing about the symptom would have pointed at the reporter. The class docstring
    # gets the conclusion right; this is the concrete failure it avoids.
    #
    # Review (low): a file every one of whose tests is skipped, or which failed to import, has no
    # entry in `paths_by_id` at all and therefore never gets a block -- it is simply absent from
    # the scrollback, and its skip/error lines print after the supposedly-final wall-vs-Σ line
    # (see `finish`). `Outcome` has no `skipped` member yet so a `SKIP` block is genuinely not
    # buildable this slice, but "the file vanishes" is a different answer from "the file is
    # deferred", and it is worth naming in the module docstring's out-of-scope list alongside the
    # live footer rather than being discovered.
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
        # Review (low + documentation): two small things. (a) The class docstring says `is_tty` is
        # `stream.isatty()`; this is a `getattr` fallback, which quietly makes a stream that has no
        # `isatty` non-tty rather than raising. Fine, but say so where the claim is made. (b)
        # `is_tty` has no coverage anywhere: verified by hardcoding it to `False` and running the
        # whole suite -- 391 passed. Nothing branches on it yet (deliberate, module docstring), so
        # this is only worth noting because the field is being carried specifically so a later
        # colour pass is additive, and today nothing would notice if it were computed wrongly.
        self.is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        # Review (must fix): this counts `paths_by_id`'s *values*, i.e. one per distinct test **id**
        # -- but `run_suite` dispatches one task per `TestRecord`, and `cli.py` builds the map with
        # `{record.id: record.path for record in ...}`, a dict comprehension that silently collapses
        # duplicate ids. The two counts diverge the moment a suite has two records sharing an id,
        # and then this file's block prints early, with a wrong test count and a wrong PASS/FAIL
        # verdict, and every later result for that path vanishes from the scrollback entirely
        # (`on_result` recreates a `_buffered_by_path` entry that nothing will ever flush, since the
        # counter has gone negative and `== 0` never fires again).
        #
        # That is reachable today, with a completely ordinary file. `_collect.collect` builds
        # `id` as `f"{display_path}::{func.__qualname__}"`, and a factory-generated test has the
        # *same* `__qualname__` for every instance. Reproduced end to end:
        #
        #     def _make(label):
        #         async def test_generated():
        #             assert label != "b", "the second one fails"
        #         return test_generated
        #     test_alpha = _make("a")
        #     test_beta = _make("b")
        #
        # `velox <dir>` prints `PASS  .../test_dup.py    1 tests   0.00s` -- for a file where two
        # tests ran and one of them FAILED. The failure details and short summary below are correct
        # (they read the caller's `results` list, not this bookkeeping), so the scrollback block and
        # the end-of-run sections actively contradict each other, which is worse than either being
        # wrong alone.
        #
        # The seam is `cli.py`'s map, not this loop, so the fix belongs on both sides: `Reporter`
        # should be seeded from a `Sequence[TestRecord]`-shaped source (or an explicit
        # `counts_by_path`) that cannot lose duplicates, and `_collect` arguably owes ids that are
        # unique by construction (a `#N` suffix on repeats, the way it already hash-suffixes lossy
        # module segments). Until one of those lands, `on_result` at minimum should not silently
        # drop results for a path whose counter has already reached zero.
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
        # Review (verified negatives, both worth stating because they are the two things this
        # method's placement inside `dispatch_one` makes look risky and neither actually is).
        #
        # 1. **No threading assumption.** This body has no `await`, so under asyncio's cooperative
        #    single-threaded scheduling it runs to completion atomically with respect to every other
        #    `dispatch_one` -- the dict lookup, the append, the decrement and the flush cannot
        #    interleave with a sibling's. Nothing here would need a lock even if two tests from
        #    different files finished back to back with no suspension between them, which is exactly
        #    what happens for a batch of trivially fast tests. Traced, and the whole method is
        #    call-graph-free apart from `print`.
        # 2. **`_remaining_by_path` and `run_suite`'s own `remaining_by_module` stay coherent.**
        #    They are separate counters over the same partition, decremented in different places, so
        #    it is fair to ask whether a file's block can print before or after the module-scope
        #    fixture teardown that logically belongs to it. It cannot get out of order: in
        #    `dispatch_one` there is no `await` between `_run_one` returning and this callback
        #    *except* on the one path where `remaining_by_module[path]` hit zero and the module
        #    flush is awaited -- and only the module's last-finishing test can take that path, by
        #    which point every sibling has already run this callback synchronously. So the zero
        #    crossings of the two counters always happen in the same task, module-flush first,
        #    block second. Confirmed empirically with a `scope="module"` yield-fixture printing
        #    `MODULE-TEARDOWN`: that text lands in the last test's `captured_stdout` and the file's
        #    block prints after it, never before.
        #
        # Review (low, latent): `self.paths_by_id[result.id]` is a bare subscript, and `run_suite`
        # deliberately does not catch what `on_result` raises. A `KeyError` here therefore does not
        # degrade the report -- it aborts the entire run through the `TaskGroup` and *every* result
        # is lost. Verified by handing `Reporter` a map missing one id: `run_suite` raises
        # `ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)` wrapping
        # `KeyError: 'test_y.py::test_b'`, and `results` is never returned. Unreachable from
        # `cli.main` as written (the map is built from the same `collected.records` list that is
        # then dispatched, so every dispatched id is a key -- I checked, this really is airtight
        # today), which is why this is latent rather than a bug. But the blast radius is total, and
        # `Reporter` is constructed by the caller with a map the caller assembles, so a `.get(...)`
        # with a "no path known" fallback bucket costs nothing and turns a future wiring mistake
        # into a cosmetically wrong block instead of a lost run.
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
        # Review (low): this number is a Σ, and nothing on the line says so. Under concurrency a
        # file's summed durations routinely exceed the *entire run's* wall clock, so a suite that
        # finished in 0.5s can print `FAIL  tests/api/test_billing.py  8 tests  2.10s` -- which
        # reads, to anyone who has ever used pytest or jest, as "this file took 2.10 seconds". The
        # docstring above explains the choice and it is the right one (there is no attributable
        # span), but the *output* is the thing that has to carry it, and it is sitting a few lines
        # above a final line that goes to some trouble to distinguish wall from Σ. `Σ 2.10s`, or a
        # column header, would cost nothing. Related and smaller: `duration` here is `_run_one`'s
        # measurement, which stops before `dispatch_one` awaits the module-scope fixture flush, so
        # a file whose module fixtures are slow to tear down under-reports by exactly that amount
        # while the run's wall clock includes it.
        duration = sum(result.duration for result in results)
        # Review (low): `{path!s:<32}` is a fixed column with no truncation, so any path longer
        # than 32 characters pushes the count and duration out of alignment and the table stops
        # being a table -- which happens immediately for an explicit-path invocation, where
        # `_collect._display_path` falls back to the *absolute* resolved path (verified: a run under
        # a tmp dir prints a ~90-character first column). spec/10 §2's own example is aligned;
        # eliding the middle of an over-long path (`tests/…/test_billing.py`) keeps it so.
        line = f"{status:<5} {path!s:<32} {len(results):>4} tests   {duration:.2f}s"
        if failed:
            line += f"   ({failed} failed)"
        # Review (should fix): nothing ever flushes `self.stream`, and that silently defeats the
        # entire streaming design in exactly the mode spec/10 §2 specifies it for. On a tty stdout
        # is line-buffered and blocks appear as they are computed; piped to a file or a CI log
        # collector it is *block*-buffered, so every block sits in an 8 KiB buffer until the
        # process exits. Verified: two files, one with a 2.0s test and one instant, output read
        # line by line from a pipe -- the fast file's block is computed at t≈0.00s and does not
        # reach the reader until t+2.08s, together with everything else. spec/10 §2's non-tty
        # requirement is literally "one line per file as it completes", and spec/10 §2 sells the
        # per-file block as the fix for xdist's incoherent scrollback; in CI, where that matters
        # most, this currently behaves exactly like buffering the whole run. `print(line,
        # file=self.stream, flush=True)` is the whole fix.
        #
        # Review (should fix, and it is the same line): under `-s`/`--capture=no` this writes to
        # the same real stdout that `_capture.Router` is echoing live test output to, with no
        # coordination between the two. `Router`'s per-line id prefixing tracks line-start state on
        # each *`Sink`*, so a block printed here in the middle of a test's unterminated line both
        # corrupts that line and desynchronizes the prefix. Verified: a test doing
        # `print("PARTIAL-NO-NEWLINE", end="")`, an `await`, then `print("...rest of the line")`,
        # with a second file finishing during the await, produces
        #   [.../test_p1.py::test_partial_line] PARTIAL-NO-NEWLINEPASS  .../test_p2.py  1 tests ...
        #   ...rest of the line
        # -- the block spliced into the middle of a test's output line, and the continuation
        # orphaned from its test-id prefix. This is new surface: before this slice the reporter
        # printed nothing at all while tests were running. Cheapest honest fix is to have the block
        # writer ask the session `Router` to break the line first (or, under passthrough, route
        # block lines through the same prefixing writer so the two share line-start state).
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
                    # Review (low, cosmetic): `result.failure` is `traceback.format_exc()` output,
                    # which already ends in `"\n"`, so `print` adds a second and every failure block
                    # gains a stray blank line. Harmless on its own, but it makes the section
                    # spacing inconsistent in a way a reader will read as meaningful: a failure with
                    # captured output gets a blank line before `--- captured stdout ---` while one
                    # without gets a blank line before `--- short test summary ---`, and a
                    # `TIMEOUT` whose `failure` is the bare one-line budget message (no trailing
                    # newline) gets neither. `print(result.failure.rstrip("\n"), ...)` plus one
                    # deliberate separator would make the spacing a property of the layout rather
                    # than of what `format_exc` happened to end with.
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
            # Review (documentation): both stated causes are wrong, which matters because they are
            # what a future reader will use to decide whether this branch can be deleted.
            # "An empty suite" does not produce `wall_clock == 0` -- `cli.py` starts its clock
            # before `run_suite`, which unconditionally does `_capture.install()` (a `mkdir`, a
            # marker write and a retention sweep), builds an `asyncio.Runner`, runs it, and tears
            # it all down. Measured: `run_suite([])` takes 0.9-1.5ms, and `velox` on an empty
            # directory prints `0.00s wall` with a live `0.0x concurrency` ratio, i.e. it takes the
            # *other* branch. And "negative" is unreachable from this codebase at all: `wall_clock`
            # is a difference of two `time.monotonic()` readings, which the stdlib guarantees never
            # decreases. What actually reaches this branch is a direct caller passing a literal
            # `0.0` -- which is exactly what the one test covering it does, and nothing else. Worth
            # keeping the guard (it is free, and `finish` is callable from outside `cli.py`), but
            # say "a caller that measured nothing", not "an empty suite".
            concurrency = "n/a concurrency"
        print(file=self.stream)
        # Review (should fix): spec/10 §2 calls this "the final line", and it is not -- `cli.main`
        # prints three more things after `finish` returns: one line per `collected.skipped`, a full
        # traceback per `collected.errors`, and its own `N tests: X passed, Y failed, ...` summary.
        # Verified on a directory with one skipped test and one unimportable file: the wall-vs-Σ
        # line lands *above* a 15-line `ModuleNotFoundError` dump, which is precisely the "buried
        # in scrollback" outcome having a designated final line exists to prevent. Worse, the two
        # summary lines now disagree about the same run: this one reports `1 failed` (every
        # non-`PASSED` outcome) while `cli.py`'s reports `0 failed, 1 errored` for the identical
        # results -- verified with a fixture whose teardown raises. Either `finish` should own the
        # whole tail (take `skipped`/`errors` and emit one reconciled summary), or `cli.py` should
        # print its sections before calling `finish`; two independently-maintained summaries of one
        # run, adjacent, using "failed" to mean two different things, is the shape that gets
        # reported as a velox bug.
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
        # Review (verified negative, worth stating): the `TIMEOUT` branch is correct for both
        # shapes `_run_one` actually produces. `failure` is either the bare synthesized message or
        # that message + `"\n\n"` + the substituted exception's traceback, so `lines[0]` is the
        # budget message in both cases -- confirmed end to end (`TIMEOUT <id> - test exceeded the
        # --timeout=0.05s budget`). It is the `else` branch below that is wrong.
        return lines[0].strip()
    # Review (must fix): "the last non-blank line is always `ExceptionType: message`" is false for
    # most of the failures velox itself produces, and the short summary is, per spec/10 §2, "the
    # most-copied line in pytest's output". Four shapes, each reproduced end to end through
    # `velox <dir>` rather than inferred:
    #
    # 1. `assert x == y, "message"` -- the single most idiomatic assert form -- under the *default*
    #    `--assert=rewrite`. The vendored rewriter appends its explanation *after* the exception
    #    line, so `format_exc()` ends `AssertionError: the real reason\nassert 3 == 4`, and this
    #    returns `assert 3 == 4`: the user's own message, the only part carrying intent, is the one
    #    thing dropped. pytest's line for the identical input is `AssertionError: the real reason`.
    #    Measured: `assert total == 4, "expected four widgets"` -> `- assert 3 == 4`.
    # 2. Any `ExceptionGroup`/`BaseExceptionGroup`. `TracebackException`'s group rendering ends
    #    with the closing rule, so this returns the literal string
    #    `+------------------------------------`. Measured with an ordinary
    #    `async with asyncio.TaskGroup():` in a test body -- which in an async-only runner is not
    #    an exotic shape but the recommended one.
    # 3. **Every fixture teardown error**, which is the case that turns 2 from a curiosity into a
    #    must-fix: `_di._release_all` raises `BaseExceptionGroup("fixture teardown", errors)`
    #    unconditionally (spec/04 §5, by design), so *every* `Outcome.ERROR` that came from
    #    teardown renders as the same row of dashes. Measured: a yield-fixture raising
    #    `RuntimeError("teardown exploded")` gives
    #    `ERROR <id> - +------------------------------------`.
    # 4. `add_note` and multi-line messages. Notes render *after* the exception line, and
    #    `_di.setup`'s partial-failure cleanup adds one routinely ("Additionally, tearing down
    #    already-acquired fixtures failed: ..."), so a setup error with a failing cleanup also
    #    reports the note's last line. Same for `raise ValueError("first line\nsecond line")`,
    #    which reports `second line`.
    #
    # Only the plainest shape (`assert 1 == 2`, no message, no group, no notes) actually lands on
    # `ExceptionType: message`, and that is the one every test below covers.
    #
    # Text-parsing cannot really be made right here: the correct source is the exception object,
    # not its rendering. The cheap structural fix, well short of spec/10 §1's full `FailureRepr`,
    # is for `_run_one` to capture `f"{type(exc).__name__}: {exc}"` (its first line) at the moment
    # it catches the exception and hand it over as a second `TestResult` field alongside the flat
    # `failure` text -- one line at each of the three `except BaseException` sites, and it gets all
    # four shapes above right by construction, including picking the group's own summary
    # ("ExceptionGroup: fixture teardown (1 sub-exception)") rather than its bottom border. Failing
    # that, the text heuristic wants to be "the last line at zero indentation that is not part of a
    # frame entry and not a group rule", with the rewriter-explanation and note cases still
    # unsolved.
    for line in reversed(lines):
        if line.strip():
            return line.strip()
    return ""
