"""Capture: stdout/stderr routing, logging, and tmp_path (spec/09) — M1 slice.

*Pytest's default capture dup2's fds 1/2 into a shared temp file, with one global slot and
suspend/resume state asserts — structurally impossible under concurrency, because the byte
stream carries no attribution (spec/09's epigraph, R§7). velox routes by* context *instead:*

```
sys.stdout = Router(real_stdout)     # installed once, never swapped mid-run
sys.stderr = Router(real_stderr)

class Router:
    def write(self, s):
        sink = _current_sink.get()   # ContextVar
        (sink or self._session_sink).write(s)
```

**The whole attribution story lives in one place: `current_test_context`, a `ContextVar` set once
per test by `_run.run_suite`'s `dispatch_one`, around that test's entire setup/call/teardown
envelope.** Every piece this module builds — `Router.write`, `_RoutingHandler.emit`, the five
builtin-fixture providers, `ContextPropagatingExecutor` — does nothing more than read that one
ContextVar and fall back to a session-level `Sink` when it's unset. There is no per-phase
install/uninstall dance, no suspend/resume state machine, and no lock: `ContextVar.set()` inside
one `asyncio.Task` mutates only *that task's* copy of the context (every task gets an independent
`contextvars.copy_context()` snapshot at creation, per the stdlib's own contract), so two
concurrently-dispatched tests can never observe or clobber each other's sink no matter how their
`await`s interleave. That single fact is what makes every class below race-free by construction
rather than by careful locking (spec/09's own framing, "race-free by construction").

**What lands in the session sink under *this* runtime, precisely** — narrower than spec/09 §1's
own list, because this milestone's `_run.run_suite` has no separate "session setup" phase the way
a future scheduler might:

- Output produced by a genuinely detached background thread (a raw `threading.Thread`, or a
  `ThreadPoolExecutor` velox never installed as the default executor) — it has its own, empty
  `contextvars.Context`, never a copy of whatever task spawned it, so `current_test_context.get()`
  there always sees the default (spec/09 §3's thread-attribution table).
- Output produced by `ScopeStore.aclose()`'s end-of-run session-scope teardown, which
  `run_suite` runs *outside* any `dispatch_one` task, after every test has already finished.
- **Not** import-time output or session-scoped-fixture-construction output the way spec/09 §1
  frames it: `Router`/the log handler are installed for `run_suite`'s duration only (this
  module's own docstring for `install`/`uninstall` — collection happens *before* `run_suite` is
  even called, so an import-time `print` still goes straight to the real, unrouted stream, which
  is arguably more useful — the user watching the terminal sees it immediately, with nothing to
  attribute it to). And because `_di.ScopeStore` builds a session-scoped fixture lazily, inside
  whichever test's `setup()` first asks for it (spec/04 §4's single-flight cache), that
  construction's output is attributed to the *triggering* test's own sink here, not to the
  session sink — a real test id to (potentially wrongly) blame beats none, and nothing about this
  mechanism forecloses a future dedicated session-setup phase changing that without touching
  `Router`/`Sink` at all.

Fill list, against spec/09:

- §1 — `Router`, `Sink` (size-capped, head+tail truncation), the "unattributed output" section
  (`unattributed_sections`), `-s`/`--capture=no` passthrough with per-line test-id prefixing.
- §2 — `_RoutingHandler`, the one root `logging.Handler` for the whole run; structured
  `LogRecord`s retained on `Sink.log_records` for `_builtins.LogRecords`.
- §3 — `ContextPropagatingExecutor`, installed as `loop.set_default_executor(...)`.
- §5 — `TmpPathFactory` support: numbered session root + retention (`_resolve_basetemp_root`),
  `--basetemp` override, and `sanitize_test_id` for `tmp_path`'s own `basetemp/<id>` allocation.
- `TestInfo.worker` — `WorkerSlots`, a small free-list `_run.run_suite` acquires/releases around
  each test's envelope, sized to `concurrency`.

`install`/`uninstall` mirror `_rewrite.py`'s idempotent install/uninstall pattern: a module-global
records what's currently installed so a second `install()` in the same process (or the same
`run_suite` call being defensive) is a no-op, and `uninstall()` always restores exactly what was
there before and clears that global — no state survives past one `run_suite` call (I1).
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import getpass
import hashlib
import logging
import re
import shutil
import sys
import tempfile
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TextIO, cast, final

from velox import _builtins
from velox._fixtures import BuiltinContext

# Review (documentation): the module docstring's "what lands in the session sink under *this*
# runtime, precisely" list is missing two cases, one of which is neither exotic nor rare.
# 1. A **`module`-scope fixture's teardown**. `_run.dispatch_one` calls
#    `current_test_context.reset(token)` in the `finally` around `_run_one` and only *then*
#    awaits `_teardown_module_scope`, so a module fixture's own `print()` on the way out is
#    unattributed. Verified end to end: one module-scope async-generator fixture printing
#    `MODULE-FIXTURE-SETUP-OUTPUT` before its `yield` and `MODULE-FIXTURE-TEARDOWN-OUTPUT`
#    after, with one failing test -- setup output is in `result.captured_stdout` (True),
#    teardown output is not (False), and it lands in `unattributed_output` instead (True).
#    That also falsifies this docstring's own headline ("set once per test ... around that
#    test's entire setup/call/teardown envelope") and the identical claim in `TestContext`'s
#    docstring and in `run_suite`'s: module-scope teardown *is* the teardown phase for the last
#    test of a module, and is demonstrably outside the envelope. Either widen the `reset` to
#    cover `_teardown_module_scope` (it already runs inside that test's semaphore slot, so
#    attributing it to the module's last test costs nothing) or add the bullet and soften the
#    three "entire envelope" claims.
# 2. Output from a task the test spawned and never awaited does *not* land here either -- it
#    goes into the finished test's own `Sink`, which is dropped. See the note on `Sink`.

__all__ = [
    "DEFAULT_BASETEMP_RETENTION",
    "DEFAULT_CAPTURE_LIMIT",
    "CaptureSetup",
    "ContextPropagatingExecutor",
    "Router",
    "Sink",
    "TestContext",
    "WorkerSlots",
    "capture_provider",
    "current_test_context",
    "install",
    "installed",
    "log_records_provider",
    "sanitize_test_id",
    "test_info_provider",
    "tmp_path_factory_provider",
    "tmp_path_provider",
    "unattributed_sections",
    "uninstall",
]

# ------------------------------------------------------------------------------------- Sink


#: spec/09 §1: "a size cap (default 4 MiB/test, config `capture_limit`)". No `capture_limit` CLI
#: knob this session (explicitly not required for MVP, spec/09 §7) — a module constant stands in
#: for it, applied per stream (stdout and stderr are capped independently, so a chatty stderr
#: logger can't starve stdout's own budget or vice versa).
DEFAULT_CAPTURE_LIMIT = 4 * 1024 * 1024
# Review (low): "4 MiB/test" is 4 Mi *characters*, not 4 MiB of memory -- everything below
# measures `len(str)`. CPython's compact-str representation is 1, 2 or 4 bytes per character
# depending on the widest code point in the string, so a full buffer of astral-plane text costs
# 4x the documented figure: measured `sys.getsizeof("\U0001f600" * DEFAULT_CAPTURE_LIMIT)` ==
# 16.0 MiB, per stream, per test, times `concurrency`. The cap still bounds memory (I5's actual
# requirement), just not at the number written here; either say "characters" or budget in bytes.


@final
class _CappedBuffer:
    """One capped text stream: the first half of the budget kept as a permanent head, the last
    half as a rolling tail, with the middle dropped and replaced by a marker once the budget is
    exceeded (spec/09 §1: "switches to head+tail truncation with a marker... so a runaway print
    in a loop cannot OOM the run").

    Deliberately not "keep everything, truncate only at render time": that would defeat the whole
    point (I5 — bounded memory, not bounded *display*) since the point is to cap what's ever held
    in memory for one test, not just what's shown. The head is kept because the start of a
    runaway dump is usually the most useful part (the first assertion or the first few log
    lines); the tail is kept because it's usually where a loop's failure actually happened.
    """

    __slots__ = (
        "_head",
        "_head_limit",
        "_head_size",
        "_omitted",
        "_tail",
        "_tail_limit",
        "_tail_size",
        "_truncated",
    )

    def __init__(self, limit: int = DEFAULT_CAPTURE_LIMIT) -> None:
        self._head_limit = limit // 2
        self._tail_limit = limit - self._head_limit
        self._head: list[str] = []
        self._head_size = 0
        self._tail: list[str] = []
        self._tail_size = 0
        self._omitted = 0
        self._truncated = False

    def write(self, s: str) -> None:
        if not s:
            return
        if not self._truncated:
            room = self._head_limit - self._head_size
            if len(s) <= room:
                self._head.append(s)
                self._head_size += len(s)
                return
            if room > 0:
                self._head.append(s[:room])
                self._head_size += room
                s = s[room:]
            # Review (must fix): `_truncated` is latched here, i.e. as soon as more than
            # `limit // 2` characters have been written -- not when `limit` is exceeded. For any
            # total between `limit // 2 + 1` and `limit` inclusive, *nothing is ever dropped*
            # (the overflow all fits in the tail budget), yet `getvalue()` unconditionally splices
            # `"\n... [0 bytes omitted, capture limit exceeded] ...\n"` into the middle of output
            # that was retained in full. So a test that prints 2 MiB + 1 char under the 4 MiB
            # default gets a false "capture limit exceeded" banner wedged into its stdout, and the
            # banner lands mid-line, corrupting whatever the reporter (or a user grepping the
            # failure block) reads next. Verified, `_CappedBuffer(100)`, one write of N chars:
            #   N= 50  marker=False  kept= 50/ 50  lost=0
            #   N= 51  marker=True   kept= 51/ 51  lost=0   <-- marker, nothing omitted
            #   N=100  marker=True   kept=100/100  lost=0   <-- marker, nothing omitted
            #   N=101  marker=True   kept=100/101  lost=1   <-- first genuine truncation
            # The fix is to keep the latch but make `getvalue` emit the marker only when
            # `self._omitted > 0` (the head/tail split itself is fine and can stay eager) -- the
            # two concepts, "I have started splitting head from tail" and "I have actually thrown
            # something away", are conflated into one flag today. The existing tests can't see
            # this: `test_sink_truncates_with_head_and_tail_once_over_the_cap` jumps straight from
            # 40 to 5040 characters and `test_sink_keeps_everything_under_the_cap_verbatim` writes
            # 11 against a limit of 1000, so nothing probes `limit // 2 < n <= limit`.
            self._truncated = True
            if not s:
                return

        # Tail path. One `write()` call is not bounded in size (a single `print` of a huge repr
        # is exactly the runaway case this class exists to survive), so a chunk that alone is at
        # least the whole tail budget is handled directly — keep only *its own* last
        # `_tail_limit` characters and drop everything queued before it in one step — rather than
        # appending it whole and relying on the general pop-from-the-left loop below, which pops
        # one *list element* at a time and would otherwise discard this entire oversized chunk
        # (dropping the tail budget to empty) instead of keeping the trailing slice of it that
        # actually belongs there.
        if self._tail_limit > 0 and len(s) >= self._tail_limit:
            self._omitted += self._tail_size + (len(s) - self._tail_limit)
            self._tail = [s[-self._tail_limit :]]
            self._tail_size = self._tail_limit
            return

        self._tail.append(s)
        self._tail_size += len(s)
        while self._tail_size > self._tail_limit and self._tail:
            dropped = self._tail.pop(0)
            self._tail_size -= len(dropped)
            self._omitted += len(dropped)

    def getvalue(self) -> str:
        if not self._truncated:
            return "".join(self._head)
        # Review (low): "bytes" is wrong -- `_omitted` accumulates `len(str)`, i.e. characters.
        # Verified: `_CappedBuffer(100)` given 200 astral-plane emoji (200 characters, 800 UTF-8
        # bytes) reports "100 bytes omitted" when 100 *characters* / 400 bytes were dropped. Same
        # word, same conflation as `DEFAULT_CAPTURE_LIMIT` above; say "characters".
        marker = f"\n... [{self._omitted} bytes omitted, capture limit exceeded] ...\n"
        return "".join(self._head) + marker + "".join(self._tail)


@final
class Sink:
    """Everything captured for one test, or for the session (spec/09 §1): stdout, stderr, and the
    structured `LogRecord`s emitted while it was the active sink (spec/09 §2). One instance per
    test, created fresh in `_run.run_suite`'s `dispatch_one` and referenced only through
    `current_test_context` — never shared, never mutated from more than one task at a time by
    construction (see module docstring), so nothing here needs its own lock.

    `label` is the test id (or `"<unattributed>"` for the session sink) — used only for `-s`'s
    per-line prefixing and for a future reporter's section headers; it plays no role in
    attribution itself, which is entirely `current_test_context`'s job.
    """

    __slots__ = (
        "_err",
        "_err_at_line_start",
        "_out",
        "_out_at_line_start",
        "label",
        "log_records",
    )

    # Review (low, latent): the docstring above says this is "never shared, never mutated from
    # more than one task at a time by construction ... so nothing here needs its own lock". The
    # "by construction" argument is the single-threaded-event-loop one, and it is exactly the
    # argument `ContextPropagatingExecutor` below is built to break: that class exists so that
    # `loop.run_in_executor(None, fn)` runs `fn` in a *worker thread* under a copy of the test's
    # context, which means `fn`'s `print()` reaches this same `Sink` from a second OS thread while
    # the test's own task can still be writing to it from the loop thread. `_CappedBuffer.write`'s
    # `self._head_size += len(s)` is a non-atomic read-modify-write, as is `_omitted +=` and the
    # `_out_at_line_start` round trip `Router.write` does under `-s`; a lost update there silently
    # lets the head grow past `_head_limit`. Inert in practice today (the GIL makes the window
    # tiny and the consequence is a slightly-wrong byte count, not corruption), but the docstring
    # asserts a property the design deliberately does not have, and on a free-threaded build it
    # stops being theoretical. Either qualify the claim or give `Sink` a `threading.Lock`.
    def __init__(self, label: str, *, limit: int = DEFAULT_CAPTURE_LIMIT) -> None:
        self.label = label
        self._out = _CappedBuffer(limit)
        self._err = _CappedBuffer(limit)
        # Review (should fix): this list has no cap of any kind, so the OOM `_CappedBuffer` exists
        # to prevent is still wide open through the logging door. `_CappedBuffer`'s own docstring
        # states the goal as "so a runaway `print` in a loop cannot OOM the run" -- a runaway
        # `logger.info` in a loop still can, and it is the more likely of the two in a real suite.
        # Worse than plain text growth: a retained `LogRecord` pins `record.args` alive, so the
        # buffer holds strong references to arbitrary user objects (ORM rows, response bodies) for
        # as long as the test's `Sink` lives. Verified: `Sink("t", limit=64)` with 20000 records
        # appended -> `len(sink.out) == 0` (text correctly capped) but `len(sink.log_records) ==
        # 20000`, and `sink.log_records[0].args[0] is payload` -> True, i.e. the original object,
        # not a formatted copy. spec/09 §1's cap is written about text only, so this is a spec gap
        # as much as a code one, but I5 ("bounded memory") is stated over the run, not over stdout.
        # A cap here is awkward precisely *because* the records are structured (dropping the middle
        # of a record list is meaningful in a way dropping the middle of a char stream is not), so
        # this probably wants a deliberate decision -- ring buffer, or a count cap with a synthetic
        # "N records dropped" record -- rather than being left implicit.
        self.log_records: list[logging.LogRecord] = []
        self._out_at_line_start = True
        self._err_at_line_start = True

    def write_out(self, s: str) -> None:
        self._out.write(s)

    def write_err(self, s: str) -> None:
        self._err.write(s)

    @property
    def out(self) -> str:
        """This sink's stdout so far. Read live — `Capture.out` (spec/09) holds a reference to
        this `Sink`, not a snapshot, so it reflects writes made after the fixture was injected."""
        return self._out.getvalue()

    @property
    def err(self) -> str:
        return self._err.getvalue()


# ----------------------------------------------------------------------------------- Router

# One combined ContextVar (`TestContext`, below), not a bare `Sink` ContextVar: `TestInfo` needs
# tags/timeout/worker alongside the sink, and every reader (`Router`, `_RoutingHandler`, the five
# providers below) wants the same one value, so setting one combined value per test — rather than
# three or four independently, each needing its own reset bookkeeping — is what
# `_run.run_suite`'s `dispatch_one` actually does.


@final
@dataclass(frozen=True, slots=True)
class TestContext:
    """The ambient, per-test-task state a builtin-fixture provider needs beyond
    `_fixtures.BuiltinContext` (see that class's own docstring, which names this exact gap): the
    current `Sink`, this test's tags/timeout for `TestInfo`, and which concurrency slot it
    occupies.

    Set once per test by `_run.run_suite`'s `dispatch_one`, via
    `current_test_context.set(...)`/`.reset(token)` wrapped around that test's whole
    setup/call/teardown envelope. This is a `ContextVar.set` inside one `asyncio.Task`, not a
    module-global mutation (I1): every task gets its own independent copy of the context at
    creation (`asyncio.Task.__init__` calls `contextvars.copy_context()`), so `.set()` here can
    only ever be observed by `await`-reachable code in *this* test's own task tree, and
    `.reset(token)` in `dispatch_one`'s `finally` leaves nothing behind once the test finishes —
    there is no window where a later, unrelated task could see a stale value, because there is
    nothing shared for it to see.
    """

    sink: Sink
    tags: tuple[str, ...]
    timeout: float | None
    worker: int


current_test_context: ContextVar[TestContext | None] = ContextVar(
    "velox_current_test_context", default=None
)
# Review (should fix): `TestContext`'s docstring claims `.reset(token)` "leaves nothing behind
# once the test finishes -- there is no window where a later, unrelated task could see a stale
# value, because there is nothing shared for it to see". The first half of that is what makes
# cross-test isolation airtight and it is correct (I could not construct any leak *into another
# test's* `Sink`); the second half is not. A task the test spawned and did not await copied this
# ContextVar at creation, so it keeps seeing the finished test's `TestContext` for as long as it
# lives -- `.reset()` in the parent cannot reach a child context. The value is stale, and the
# consequence is silent data loss rather than misattribution: the orphan writes into a `Sink`
# nobody will ever read again. Verified with two concurrently dispatched tests -- test 0 spawns
# `create_task(bg())` that sleeps 20ms then prints and returns immediately (PASSED, so its `Sink`
# is dropped); test 1 sleeps 100ms then fails, so its capture *is* retained:
#     'LATE-OUTPUT-FROM-AN-ORPHANED-TASK' in results[1].captured_stdout -> False
#     'LATE-OUTPUT-FROM-AN-ORPHANED-TASK' in unattributed_output       -> False
# i.e. the output exists, is written through the installed `Router`, and reaches neither the
# report nor the unattributed section. spec/09 §3's table says a spawned task is attributed
# ("Yes -- context inherited at task creation"), which is true right up until its own test
# finishes; the table has no row for "after". Not fixable inside this module (the real answer is
# spec/05 §2's per-test `TaskGroup`, explicitly deferred in `_run.py`'s module docstring), but
# the docstring should stop claiming the window does not exist, and the module docstring's
# session-sink list should say where such output actually goes: nowhere.


def _echo(real: TextIO, label: str, text: str, at_line_start: bool) -> bool:
    """Write `text` to `real` with `[label] ` prefixed at the start of each line, for `-s`'s
    passthrough mode (spec/09 §1: "prefixed with the test id per line, so concurrent output stays
    readable"). Returns the updated "are we at a line boundary" state for the next call.

    A `Router.write` call is not guaranteed to be newline-aligned (a single `print` can arrive as
    one call, or a stream wrapper can split it across several) — tracking `at_line_start` across
    calls, rather than naively prefixing every `\\n`-split segment, is what keeps a chunk that
    lands mid-line from getting a spurious prefix in the middle of a line or missing the one it
    actually needs at the start of the next.
    """
    if not text:
        return at_line_start
    prefix = f"[{label}] "
    parts: list[str] = []
    lines = text.split("\n")
    last = len(lines) - 1
    for i, line in enumerate(lines):
        if i > 0:
            parts.append("\n")
            at_line_start = True
        if at_line_start and (i < last or line):
            parts.append(prefix)
            at_line_start = False
        parts.append(line)
    real.write("".join(parts))
    return at_line_start


@final
class Router:
    """`sys.stdout`/`sys.stderr`'s replacement for the whole run (spec/09 §1). Installed once by
    `install()`, restored exactly by `uninstall()` — no per-phase add/remove, no suspend/resume
    state, no lock: `write` does nothing but ask `current_test_context` which `Sink` is active
    right now and hand the bytes to it (module docstring).

    A small duck-typed stream, not a real `io.TextIOBase` subclass — `write`/`flush`/`writelines`/
    `isatty`/`encoding` cover what stdlib `print`/`logging`/most libraries actually call. Code
    that reaches for `sys.stdout.buffer` (binary-mode access) will not find one; that gap is the
    same one fd-level `--isolated` capture is roadmapped to close (spec/09 §8), not something this
    MVP's ContextVar-routed `Router` can offer without becoming a real fd proxy.
    """

    __slots__ = ("_passthrough", "_real", "_session_sink", "_which")

    def __init__(
        self,
        real: TextIO,
        which: Literal["stdout", "stderr"],
        session_sink: Sink,
        *,
        passthrough: bool,
    ) -> None:
        self._real = real
        self._which = which
        self._session_sink = session_sink
        self._passthrough = passthrough

    def _sink(self) -> Sink:
        tc = current_test_context.get()
        return tc.sink if tc is not None else self._session_sink

    def write(self, s: str) -> int:
        sink = self._sink()
        if self._which == "stdout":
            sink.write_out(s)
            if self._passthrough:
                sink._out_at_line_start = _echo(self._real, sink.label, s, sink._out_at_line_start)
        else:
            sink.write_err(s)
            if self._passthrough:
                sink._err_at_line_start = _echo(self._real, sink.label, s, sink._err_at_line_start)
        return len(s)

    def writelines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        if self._passthrough:
            self._real.flush()

    # Review (low): the docstring names `sys.stdout.buffer` as the one known gap, but `fileno()`
    # is missing too and is the more commonly reached of the two -- `subprocess.run(...,
    # stdout=sys.stdout)`, `os.isatty(sys.stdout.fileno())`, `faulthandler.enable()` and most
    # terminal-detection helpers call it. Verified under `velox`: a test printing
    # `hasattr(sys.stdout, "fileno")` reports `False`. Unlike `buffer`, `fileno` has a defensible
    # answer here (delegate to `self._real.fileno()`) -- writes through it bypass capture, but
    # that is already true of any direct fd write and is exactly what spec/09 §3's last table row
    # documents. At minimum add it to the docstring's gap list next to `buffer`.
    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return getattr(self._real, "encoding", "utf-8")


# ---------------------------------------------------------------------------------- Logging


@final
class _RoutingHandler(logging.Handler):
    """The one root `logging.Handler` for the whole run (spec/09 §2). `emit` retains the
    structured `LogRecord` on whichever `Sink` is active (`current_test_context`, same lookup as
    `Router`) and does nothing else — no formatting, no filtering beyond the ordinary
    `Logger.isEnabledFor`/handler-level machinery every `logging` call already goes through.

    Deliberately `level=logging.NOTSET`: this handler must never itself be the reason a record is
    dropped. Whether a record reaches here at all is entirely up to each logger's own effective
    level (raised or lowered per-logger by `LogRecords.set_level`, spec/09 §2) — this handler's
    only job, once a record does arrive, is to keep it.

    No `self.format(record)` here — spec/09 §2: "Formatting is applied at report time, not at
    emit time... lets `-v` change format after the fact." A future reporter (spec/10) or
    `cli.py`'s minimal failing-test printer formats lazily, on demand, off `Sink.log_records`.
    """

    def __init__(self, session_sink: Sink) -> None:
        super().__init__(level=logging.NOTSET)
        self._session_sink = session_sink

    def emit(self, record: logging.LogRecord) -> None:
        tc = current_test_context.get()
        sink = tc.sink if tc is not None else self._session_sink
        sink.log_records.append(record)


# ----------------------------------------------------------------------------------- Threads


@final
class ContextPropagatingExecutor(concurrent.futures.ThreadPoolExecutor):
    """`loop.set_default_executor(...)`'s value for the whole run (spec/09 §3).

    Plain `ThreadPoolExecutor.submit` runs `fn` in whatever `contextvars.Context` the worker
    thread happens to have — never a copy of the submitter's, so `loop.run_in_executor(None, fn)`
    would otherwise always land in the session sink, indistinguishable from a genuinely detached
    background thread. This subclass's `submit` wraps `fn` in `contextvars.copy_context().run`,
    capturing the context *at submit time* — correct because `BaseEventLoop.run_in_executor` calls
    `executor.submit` synchronously, during the awaiting task's own step (spec/09 §3's table:
    "Correct because submit-time context is the test's"), so the copy taken here is the calling
    test's context, not some unrelated one.
    """

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        ctx = contextvars.copy_context()
        return super().submit(cast(Callable[..., Any], ctx.run), fn, *args, **kwargs)


# --------------------------------------------------------------------------- Concurrency slots


@final
class WorkerSlots:
    """A free-list of concurrency-slot indices `0..concurrency-1` — `TestInfo.worker` (spec/09
    §7, spec/01 §6).

    `_run.run_suite`'s `asyncio.Semaphore(concurrency)` already bounds how many tests are inside
    their setup/call/teardown envelope at once; this hands each of them a small integer
    identifying *which* of those `concurrency` lanes it occupies, for display purposes (a future
    reporter's per-lane layout, spec/10 — not built this session).

    A plain list-based stack, not `asyncio.Queue` or a lock: `acquire`/`release` never `await`
    (there is no suspension point between "check" and "mutate" for a rival task's own step to
    interleave with), and asyncio is itself single-threaded, so the free list needs no
    synchronization beyond that. `acquire()` raising `IndexError` on an empty pool would mean more
    than `concurrency` tests were simultaneously inside the semaphore's guarded section at once —
    a `run_suite` invariant violation, not a condition this class defends against defensively.
    """

    __slots__ = ("_free",)

    # Review (good, worth stating): I went looking for the two failure modes this class invites --
    # two live tests handed the same slot, and a cancelled test leaking its slot until the pool
    # starves below `concurrency` -- and neither is reachable, for a reason worth writing down
    # because it is a property of the *call site*, not of this class. In `_run.dispatch_one` there
    # is no `await` anywhere between `async with semaphore:` admitting the task, `acquire()`, and
    # the `try:` whose `finally` calls `release()`; likewise none between the `finally`'s `reset`
    # and `release`. asyncio only delivers cancellation at a suspension point, so there is no
    # window in which a task can be killed holding an unreleased slot, and the semaphore caps
    # holders at `concurrency`, so `pop()` from an empty list is genuinely unreachable rather than
    # merely unlikely. Both properties break the moment anyone adds an `await` (or anything that
    # can raise) between those lines -- `marks_of(record.func)` in the `TestContext` construction
    # is already inside the unguarded region and would leak a slot if it ever raised. A one-line
    # comment at the `acquire()` call site saying "nothing between here and the `try` may await or
    # raise" would keep the invariant from being refactored away silently.
    def __init__(self, concurrency: int) -> None:
        self._free: list[int] = list(range(concurrency))

    def acquire(self) -> int:
        return self._free.pop()

    def release(self, slot: int) -> None:
        self._free.append(slot)


# ------------------------------------------------------------------------------- tmp_path(s)

#: spec/09 §5: "a retention policy (`basetemp_retention`, default 3 previous roots)". Keeping the
#: *previous* 3 (plus the new one this run just allocated) bounds disk usage across repeated runs
#: without losing the last few runs' artifacts for a "why did that fail" post-mortem.
DEFAULT_BASETEMP_RETENTION = 3

_SESSION_DIR_RE = re.compile(r"^velox-(\d+)$")


def _current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        # A sandboxed/minimal environment can lack a resolvable username entirely (no /etc/passwd
        # entry, no USER/USERNAME env var) — spec/09 §5's directory naming is cosmetic
        # (`velox-of-<user>`), not load-bearing, so falling back rather than raising keeps
        # tmp_path usable even there (I6: escalate only when it actually matters).
        return "unknown"


def _basetemp_default_root() -> Path:
    return Path(tempfile.gettempdir()) / f"velox-of-{_current_user()}"


def _allocate_session_root(parent: Path, *, retention: int) -> Path:
    """One fresh, numbered `velox-<n>` directory under `parent`, applying the retention policy.

    Numbered by construction (`max(existing) + 1`), not scanned-and-retried under contention —
    same "uniqueness by construction" argument spec/09 §5 makes for `tmp_path` itself applies one
    level up, to the session root. Known, deliberate gap: two `velox` processes racing to allocate
    a session root under the *same* parent (same user, same machine, same instant) could both read
    the same `existing` snapshot and pick the same `n` — a real TOCTOU window, not closed here
    (would need a lock file or an atomic `mkdir`-and-retry loop); acceptable for MVP because two
    concurrent `velox` invocations sharing one basetemp parent is already an unusual setup, and the
    failure mode is a `FileExistsError` on the second `root.mkdir()`, not silent data loss.
    """
    parent.mkdir(parents=True, exist_ok=True)
    existing: list[tuple[int, Path]] = []
    for child in parent.iterdir():
        match = _SESSION_DIR_RE.match(child.name)
        if match is not None and child.is_dir():
            existing.append((int(match.group(1)), child))
    existing.sort(key=lambda pair: pair[0])

    next_n = existing[-1][0] + 1 if existing else 0
    root = parent / f"velox-{next_n}"
    root.mkdir()

    keep = {path for _, path in existing[-retention:]} if retention > 0 else set()
    for _, path in existing:
        if path not in keep:
            shutil.rmtree(path, ignore_errors=True)
    return root


def _resolve_basetemp_root(explicit: Path | None, *, retention: int) -> Path:
    """`--basetemp DIR` (cleared and recreated — the documented warning, spec/09 §5) if given,
    else a fresh numbered root under the platform temp dir with the retention policy applied."""
    if explicit is not None:
        root = Path(explicit).expanduser()
        # Review (must fix): an unguarded `rmtree` of a fully unvalidated, user-supplied path.
        # `--basetemp` is plumbed straight from `argparse` (`type=Path`, no validation in
        # `cli.main`, unlike its neighbours `--concurrency` and `--timeout` which both get
        # hand-rolled checks) to this line. `argparse` turns an empty value into `Path("")`, and
        # `Path("") == PosixPath(".")`, so **`velox --basetemp= tests` deletes the working
        # directory**. Verified in a sandbox:
        #     before: ['precious_source.py', 'subdir', 'tests']
        #     $ velox --basetemp= tests
        #     OSError: [Errno 22] Invalid argument: PosixPath('.')   # rmdir('.') at the very end
        #     after:  []
        # -- every file gone, including the test directory being run, and then it crashed while
        # removing `.` itself, which (via the `install()` bug above) also left stdout orphaned so
        # the traceback the user needed was swallowed. `--basetemp=.`, `--basetemp=$HOME` and
        # `--basetemp=$PWD` are the same one-keystroke mistake with the same result.
        # spec/09 §5 asks for a "documented 'this directory is cleared' warning", and the
        # `--basetemp` help text does carry one, but a help string is not a guard for an
        # irreversible recursive delete. Minimum viable guard, in `cli.main` alongside the other
        # `4`-exit-code checks: reject an empty/`.`/`..` path, reject the cwd and any ancestor of
        # it, and reject an existing non-empty directory that does not look like a previous velox
        # basetemp (no marker file). Belt-and-braces here too: drop a `.velox-basetemp` marker on
        # creation and refuse to `rmtree` a directory that exists without one -- that makes the
        # destructive path opt-in to directories velox itself made, which is what the retention
        # sweep in `_allocate_session_root` already effectively relies on.
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        return root
    return _allocate_session_root(_basetemp_default_root(), retention=retention)


#: What survives unescaped in a `tmp_path` directory name.
_UNSAFE_ID_CHARS = re.compile(r"[^0-9A-Za-z_.-]")
#: Conservative common filesystem component-length ceiling (well under ext4/APFS/NTFS's own
#: 255-byte limits, leaving room for the digest suffix and for `basetemp/` itself).
_MAX_COMPONENT_LEN = 120


def sanitize_test_id(test_id: str) -> str:
    """`test_id`, made safe as a single path component, injectively enough that two distinct ids
    needing escaping never collide (spec/09 §5).

    Same technique as `_collect._escape_segment` (deliberately — same problem, same fix): replace
    unsafe characters, and if that changed anything, append a short digest of the *original*
    string. The digest is what makes this injective rather than merely safe — `a/b` and `a b` both
    escape to `a_b`, but the appended digests differ because they're computed over the un-escaped
    originals, so the two never collide on disk. A test id long enough to still exceed a
    filesystem's component-length limit even after escaping (a heavily parametrized id with many
    long values) is truncated with a digest suffix for the same reason: the truncation itself is
    lossy, so the digest is what keeps two long ids that happen to share a truncated prefix apart.
    """
    # Review (low, latent): not injective, and the counterexample is one line. The digest is
    # appended *only* when escaping changed something, so any id that is already safe is returned
    # verbatim -- including one that happens to look like another id's escaped form. Verified:
    #     sanitize_test_id("a/b")           == "a_b_82badf67"
    #     sanitize_test_id("a_b_82badf67")  == "a_b_82badf67"   # already safe, returned as-is
    # Two distinct inputs, one output. The docstring's careful phrasing ("two distinct ids
    # *needing escaping* never collide") is technically true, but the sentence right after it
    # ("the digest is what makes this injective") and spec/09 §5 ("injective enough to avoid
    # collisions between distinct ids") both claim more than the code delivers. Latent for
    # `tmp_path` itself, since every real test id contains `::` and so always needs escaping --
    # but *not* latent for `TmpPathFactory.mktemp(basename)` in `_builtins.py`, which routes
    # arbitrary user basenames through this same function and where an already-safe basename is
    # the normal case: `mktemp("a/b")` and `mktemp("a_b_82badf67")` both target `a_b_82badf67`.
    # `numbered=True` masks it (they share the counter and get different suffixes), so what
    # actually surfaces is `numbered=False`, as a `FileExistsError` from `mkdir(exist_ok=False)`
    # blaming the wrong caller. Appending the digest unconditionally makes it genuinely injective
    # (at the cost of uglier directory names) -- or keep the conditional digest and note in the
    # docstring that the guarantee is scoped to the always-escaped test-id domain, and give
    # `mktemp` its own sanitizer.
    escaped = _UNSAFE_ID_CHARS.sub("_", test_id)
    if escaped != test_id:
        digest = hashlib.blake2b(test_id.encode(), digest_size=4).hexdigest()
        escaped = f"{escaped}_{digest}"
    if len(escaped) > _MAX_COMPONENT_LEN:
        digest = hashlib.blake2b(test_id.encode(), digest_size=8).hexdigest()
        keep = _MAX_COMPONENT_LEN - len(digest) - 1
        escaped = f"{escaped[:keep]}_{digest}"
    return escaped


# --------------------------------------------------------------------------- Install/uninstall


@final
@dataclass(frozen=True, slots=True)
class CaptureSetup:
    """What `install()` did, and the handles `uninstall()` needs to reverse it exactly — same
    shape as `_rewrite.AssertionSetup`. `session_sink`/`basetemp_root` are also what `run_suite`
    itself needs after the run (the unattributed-output section; where `tmp_path` allocates)."""

    session_sink: Sink
    basetemp_root: Path
    passthrough: bool
    router_out: Router
    router_err: Router
    log_handler: _RoutingHandler
    real_stdout: TextIO
    real_stderr: TextIO


#: What the currently-installed `Router`s/handler decided, so a later idempotent `install()` call
#: — or `uninstall()` — has something to reverse. `None` whenever nothing is installed. Cleared
#: unconditionally by `uninstall()`; no state survives past one `run_suite` call (I1), same
#: contract `_rewrite._installed_setup` already keeps for the assertion-rewrite hook.
_installed: CaptureSetup | None = None


def install(
    *,
    passthrough: bool = False,
    basetemp: Path | None = None,
    retention: int = DEFAULT_BASETEMP_RETENTION,
) -> CaptureSetup:
    """Replace `sys.stdout`/`sys.stderr` with `Router`s and add the root logging handler, for the
    duration of one `run_suite` call. Idempotent: a second `install()` before the matching
    `uninstall()` is a no-op returning the first call's setup, exactly like `_rewrite.install` —
    without that guard, a nested or re-entrant `run_suite` would stack a second `Router` on top of
    the first, and `uninstall()` would only ever unwind the outermost one, leaving `sys.stdout`
    permanently wrapped after the *inner* call's own `uninstall()` runs.
    """
    global _installed
    # Review: the docstring justifies this guard with "a nested or re-entrant `run_suite`", but
    # that scenario cannot occur -- `run_suite` drives an `asyncio.Runner`, and calling it from
    # inside a running test raises `RuntimeError: Runner.run() cannot be called from a running
    # event loop` before capture is ever reached (verified: the outer test just reports FAILED).
    # So the guard is defending a case the architecture already forecloses, while the case it
    # *does* silently change is unmentioned: a second `install()` returns the first call's setup
    # and **discards its `passthrough` and `basetemp` arguments without a word**.
    # `test_install_is_idempotent_and_uninstall_restores_the_real_streams` pins exactly that
    # (`second = install(basetemp=tmp_path / "two")  # ignored`), so it is intended -- but "the
    # caller asked for a different basetemp and got someone else's" is the kind of silent
    # degrade I6 argues against elsewhere in this file (cf. `_require_test_context`'s loud
    # `RuntimeError`). Worth either raising when the arguments disagree with the live setup, or
    # rewriting the docstring's rationale to the real one.
    if _installed is not None:
        return _installed

    session_sink = Sink(label="<unattributed>")
    real_stdout, real_stderr = cast(TextIO, sys.stdout), cast(TextIO, sys.stderr)
    router_out = Router(real_stdout, "stdout", session_sink, passthrough=passthrough)
    router_err = Router(real_stderr, "stderr", session_sink, passthrough=passthrough)
    sys.stdout = cast(Any, router_out)
    sys.stderr = cast(Any, router_err)

    log_handler = _RoutingHandler(session_sink)
    logging.getLogger().addHandler(log_handler)

    # Review (must fix): this is the one fallible step in `install()`, and it runs *after*
    # `sys.stdout`/`sys.stderr` have already been replaced and the root log handler already added,
    # but *before* `_installed` is assigned. If it raises, the process is left permanently broken:
    # the `Router`s stay on `sys.stdout`/`sys.stderr` forever, the root handler stays attached
    # forever, and `uninstall()` can never undo either because `_installed` is still `None` and it
    # returns immediately. `_run.run_suite` cannot save it either -- it calls `install()` on the
    # line *before* its own `try:`, so its `finally` never runs. Verified with the most ordinary
    # typo imaginable, `--basetemp` naming an existing *file*:
    #     run_suite([], basetemp=<a regular file>)
    #     -> NotADirectoryError: [Errno 20] Not a directory
    #     sys.stdout is the real stream?  False     (type: Router)
    #     root logger handlers: 0 -> 1
    #     _capture.installed() is None?   True
    #     after uninstall(), stdout real? False
    # and every subsequent `print()` in that process vanished into an orphaned `Sink`.
    # This is also precisely where the "mirrors `_rewrite.py`'s idempotent install/uninstall
    # pattern" claim in the module docstring stops being true. `_rewrite.install` does all of its
    # fallible work first (`plan(...)`, the cache probe, the `plain`-mode early return) and only
    # then touches global state -- `sys.meta_path.insert(0, hook)` is the last mutation before
    # `_installed_setup = setup`, so a failure leaves nothing installed. Reordering to match
    # (resolve `basetemp_root` first, then swap the streams, then add the handler, then record
    # `_installed`) fixes this with no new machinery; failing that, `install()` needs its own
    # try/except that unwinds the partial install before re-raising.
    basetemp_root = _resolve_basetemp_root(basetemp, retention=retention)

    setup = CaptureSetup(
        session_sink=session_sink,
        basetemp_root=basetemp_root,
        passthrough=passthrough,
        router_out=router_out,
        router_err=router_err,
        log_handler=log_handler,
        real_stdout=real_stdout,
        real_stderr=real_stderr,
    )
    _installed = setup
    return setup


def uninstall() -> None:
    """Restore exactly what `install()` replaced, and forget it. Idempotent: a no-op if nothing
    is installed. Always safe to call from a `finally`, mirroring `_rewrite.uninstall`."""
    global _installed
    if _installed is None:
        return
    sys.stdout = cast(Any, _installed.real_stdout)
    sys.stderr = cast(Any, _installed.real_stderr)
    logging.getLogger().removeHandler(_installed.log_handler)
    _installed = None


def installed() -> CaptureSetup | None:
    """The currently-installed setup, or `None`. Read-only introspection — nothing in this
    package needs it besides the providers below and tests."""
    return _installed


def unattributed_sections(session_sink: Sink) -> list[str]:
    """The session sink's contents, formatted as zero or more report-ready text blocks (spec/09
    §9 Q4: "tee it into a session-level section"). Empty when the session sink caught nothing —
    the common case, since only genuinely detached background threads and end-of-run session
    teardown ever write here (module docstring)."""
    sections: list[str] = []
    if session_sink.out:
        sections.append(f"stdout:\n{session_sink.out}")
    if session_sink.err:
        sections.append(f"stderr:\n{session_sink.err}")
    if session_sink.log_records:
        lines = "\n".join(
            f"{record.levelname} {record.name}: {record.getMessage()}"
            for record in session_sink.log_records
        )
        sections.append(f"log records:\n{lines}")
    return sections


# ------------------------------------------------------------------------- Builtin providers


def _require_test_context() -> TestContext:
    """`current_test_context.get()`, or a loud `RuntimeError` — never a silent `None`-shaped
    fallback (I6). Every provider below runs during `_di._construct`, which only ever runs during
    a test's `setup()`, which `_run.run_suite`'s `dispatch_one` only ever calls with
    `current_test_context` already set (see that function's own docstring) — so `None` here means
    a velox internal bug (this module used outside `run_suite`'s envelope), not a user mistake,
    and the message says so rather than pretending capture "would have degraded gracefully."
    """
    tc = current_test_context.get()
    if tc is None:
        raise RuntimeError(
            "a builtin capture fixture (capture/log_records/test_info/tmp_path) was constructed "
            "outside any test's envelope -- this is a velox internal error: "
            "_run.run_suite's dispatch_one is expected to set current_test_context before "
            "_di.setup runs for every test, with no gap"
        )
    return tc


def _require_installed() -> CaptureSetup:
    """Like `_require_test_context`, for the providers that only need `basetemp_root` — those two
    facts (a `TestContext` set, capture `install()`ed) are supposed to always hold together for
    the duration of `run_suite`, but they're two different pieces of state (one a ContextVar, one
    a module global) with two different lifetimes, so each provider checks whichever one it
    actually depends on rather than assuming the other implies it.
    """
    if _installed is None:
        raise RuntimeError(
            "velox._capture.install() has not been called -- tmp_path/tmp_path_factory are "
            "only usable while a suite is actually running (_run.run_suite calls install() "
            "before dispatching any test)"
        )
    return _installed


type _Closer = Callable[[], Awaitable[None]]


async def capture_provider(
    kwargs: Mapping[str, Any], ctx: BuiltinContext
) -> tuple[Any, _Closer | None]:
    """`velox.capture`'s `BuiltinProvider` (spec/09 §1, spec/01 §6). `ctx` is unused — everything
    this needs (the current `Sink`) comes from `current_test_context`, not from `BuiltinContext`
    (see that class's own docstring for why: ambient per-task state, not something `_di.setup`
    already owns as a plain parameter)."""
    del ctx
    tc = _require_test_context()
    return _builtins.Capture(tc.sink), None


async def log_records_provider(
    kwargs: Mapping[str, Any], ctx: BuiltinContext
) -> tuple[Any, _Closer | None]:
    """`velox.log_records`'s `BuiltinProvider` (spec/09 §2, spec/01 §6). Hands `LogRecords` the
    live list `_RoutingHandler.emit` appends to — not a copy — so records logged after the
    fixture is injected are still visible (mirrors `Capture.out`'s "live" contract)."""
    del ctx
    tc = _require_test_context()
    return _builtins.LogRecords(tc.sink.log_records), None


async def test_info_provider(
    kwargs: Mapping[str, Any], ctx: BuiltinContext
) -> tuple[Any, _Closer | None]:
    """`velox.test_info`'s `BuiltinProvider` (spec/01 §6). `id`/`module_path` come from `ctx`
    (`_di.setup` already owns them); `tags`/`timeout`/`worker` come from `current_test_context`,
    set by `_run.run_suite`'s `dispatch_one` from that test's own marks and the suite-wide
    `--timeout` budget."""
    tc = _require_test_context()
    return (
        _builtins.TestInfo(id=ctx.test_id, tags=tc.tags, timeout=tc.timeout, worker=tc.worker),
        None,
    )


async def tmp_path_provider(
    kwargs: Mapping[str, Any], ctx: BuiltinContext
) -> tuple[Any, _Closer | None]:
    """`velox.tmp_path`'s `BuiltinProvider` (spec/09 §5, spec/01 §6): `basetemp/<sanitized-id>`,
    unique by construction — no scan-and-retry numbering (module/spec docstrings)."""
    setup = _require_installed()
    path = setup.basetemp_root / sanitize_test_id(ctx.test_id)
    path.mkdir(parents=True, exist_ok=True)
    return path, None


async def tmp_path_factory_provider(
    kwargs: Mapping[str, Any], ctx: BuiltinContext
) -> tuple[Any, _Closer | None]:
    """`velox.tmp_path_factory`'s `BuiltinProvider` (spec/09 §5, spec/01 §6). Session-scoped, so
    `_di.ScopeStore`'s own single-flight cache — not this function — is what guarantees this runs
    exactly once per run; the instance it builds holds `basetemp_root` for the rest of the run."""
    del ctx
    setup = _require_installed()
    return _builtins.TmpPathFactory(setup.basetemp_root), None
