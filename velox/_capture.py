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
- A **`module`-scope fixture's own teardown**, for the module's *last* test specifically, is
  attributed to that test too: `dispatch_one` keeps `current_test_context` set through the
  module-scope-fixture flush it runs once that test's own `remaining_by_module` count reaches
  zero, precisely so this doesn't turn into a fourth, undocumented case. A module fixture's
  teardown print is therefore visible in that one test's `captured_stdout` (if it fails) — the
  most useful place for it, since that's the test whose run actually triggered the flush.
- **Not covered at all, and silently lost rather than merely misattributed**: output from a task a
  test `create_task()`s and never `await`s. The child task's `asyncio.Task` copies this test's
  `TestContext` at creation (spec/09 §3's "context inherited at task creation"), so it keeps
  writing into that test's `Sink` for as long as it runs — including after the parent test has
  already finished and `dispatch_one` has moved on. If the test passed, its `Sink` is simply never
  read again; the orphan's output reaches neither `TestResult.captured_stdout` nor
  `unattributed_output` — there is no third place for it to go once its own `Sink` stops being
  referenced by anything. Not fixable inside this module: the real fix is spec/05 §2's per-test
  `TaskGroup` (a test's own background tasks becoming part of its envelope, cancelled or awaited
  before the test is considered finished), explicitly deferred — see `_run.py`'s own module
  docstring.

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
import threading
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TextIO, cast, final

from velox import _builtins
from velox._fixtures import BuiltinContext

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
#:
#: Measured in **characters** (`len(str)`), not bytes, despite the "MiB" name — `_CappedBuffer`
#: below counts `len(s)` throughout, and CPython's compact-string representation stores 1, 2, or 4
#: bytes per character depending on the widest code point actually present, so a buffer full of
#: astral-plane text (emoji, some CJK extensions) can cost up to 4x this figure in real memory.
#: The cap still bounds memory per test (I5's actual requirement, "bounded", not "bounded at
#: exactly this number") — real byte-accurate accounting would mean encoding every write to
#: measure it, which is real overhead on what can be a hot path (every `print`/log call in every
#: test); not worth it for a soft memory cap where "characters, documented as such" is an honest
#: and cheap enough approximation.
DEFAULT_CAPTURE_LIMIT = 4 * 1024 * 1024


@final
class _CappedBuffer:
    """One capped text stream: the first half of the budget kept as a permanent head, the last
    half as a rolling tail, with the middle dropped and replaced by a marker once something has
    actually been dropped (spec/09 §1: "switches to head+tail truncation with a marker... so a
    runaway print in a loop cannot OOM the run").

    Deliberately not "keep everything, truncate only at render time": that would defeat the whole
    point (I5 — bounded memory, not bounded *display*) since the point is to cap what's ever held
    in memory for one test, not just what's shown. The head is kept because the start of a
    runaway dump is usually the most useful part (the first assertion or the first few log
    lines); the tail is kept because it's usually where a loop's failure actually happened.

    Guarded by its own `threading.Lock`: this module also installs `ContextPropagatingExecutor`
    specifically so a test's `loop.run_in_executor(None, fn)` can write into this same buffer from
    a worker OS thread while the test's own task can still be writing to it from the loop thread
    (spec/09 §3) — the "one `asyncio.Task` at a time" argument that makes the rest of this module
    lock-free (module docstring) does not hold for *this* class, precisely because it is the one
    reachable from both sides of that executor boundary. The lock is cheap (held only across the
    handful of list/int operations in `write`, never across an `await`) and turns what would
    otherwise be a genuine — if GIL-narrow today — read-modify-write race on `_head_size`/
    `_omitted`/the head and tail lists into a real guarantee, not just one that happens to hold
    under the current interpreter.
    """

    __slots__ = (
        "_head",
        "_head_limit",
        "_head_size",
        "_lock",
        "_omitted",
        "_tail",
        "_tail_limit",
        "_tail_size",
    )

    def __init__(self, limit: int = DEFAULT_CAPTURE_LIMIT) -> None:
        self._head_limit = limit // 2
        self._tail_limit = limit - self._head_limit
        self._head: list[str] = []
        self._head_size = 0
        self._tail: list[str] = []
        self._tail_size = 0
        self._omitted = 0
        self._lock = threading.Lock()

    def write(self, s: str) -> None:
        if not s:
            return
        with self._lock:
            if self._head_size < self._head_limit:
                room = self._head_limit - self._head_size
                if len(s) <= room:
                    self._head.append(s)
                    self._head_size += len(s)
                    return
                self._head.append(s[:room])
                self._head_size += room
                s = s[room:]
                if not s:
                    return

            # Tail path. One `write()` call is not bounded in size (a single `print` of a huge
            # repr is exactly the runaway case this class exists to survive), so a chunk that
            # alone is at least the whole tail budget is handled directly — keep only *its own*
            # last `_tail_limit` characters and drop everything queued before it in one step —
            # rather than appending it whole and relying on the general pop-from-the-left loop
            # below, which pops one *list element* at a time and would otherwise discard this
            # entire oversized chunk (dropping the tail budget to empty) instead of keeping the
            # trailing slice of it that actually belongs there.
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
        # The marker means "something was actually dropped" (`_omitted > 0`), not "the head
        # budget was ever exceeded" — those are different conditions. Reaching the tail path in
        # `write` above (head full, so the head/tail split has started) does not by itself imply
        # anything was lost: for any total between `_head_limit + 1` and `limit` inclusive, the
        # overflow fits entirely within the tail budget and nothing is ever dropped. Splicing the
        # marker in whenever the split merely *started* (as an earlier version of this method
        # did, keyed off a single `_truncated` flag set the moment the head filled) produces a
        # false "capture limit exceeded, 0 characters omitted" banner wedged into output that was
        # retained in full — worse than merely misleading, since the banner lands mid-stream and
        # corrupts whatever a reporter or a user grepping the failure block reads next.
        with self._lock:
            head, tail, omitted = "".join(self._head), "".join(self._tail), self._omitted
        if omitted <= 0:
            return head + tail
        marker = f"\n... [{omitted} characters omitted, capture limit exceeded] ...\n"
        return head + marker + tail


#: `Sink.log_records`' own bound — independent of `DEFAULT_CAPTURE_LIMIT`, since it counts
#: records, not characters (spec/09 §2's structured `LogRecord`s aren't a text budget at all).
DEFAULT_LOG_RECORD_LIMIT = 2000


@final
class Sink:
    """Everything captured for one test, or for the session (spec/09 §1): stdout, stderr, and the
    structured `LogRecord`s emitted while it was the active sink (spec/09 §2). One instance per
    test, created fresh in `_run.run_suite`'s `dispatch_one` and referenced only through
    `current_test_context`.

    Not lock-free the way the rest of this module is (module docstring's "race-free by
    construction" is about `current_test_context` attribution, not about this class's own
    internals): `current_test_context` does guarantee only one *task* writes here at a time, but
    `ContextPropagatingExecutor` below exists specifically so a test's `loop.run_in_executor(None,
    fn)` can also reach this same `Sink` from a worker *thread* while the test's own task is still
    writing to it from the loop thread (spec/09 §3). `_CappedBuffer` (below) takes its own lock for
    exactly that reason; `log_records` is a thread-safe `deque`. What is *not* independently
    guarded is the `_out_at_line_start`/`_err_at_line_start` pair `Router` mutates for `-s`'s
    per-line prefixing — a cross-thread race there could misplace a prefix under concurrent `-s` +
    executor-thread output, a narrower and lower-consequence gap (cosmetic, not data loss or
    corruption) than the ones the two guards above close, and left undefended this session.

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

    def __init__(self, label: str, *, limit: int = DEFAULT_CAPTURE_LIMIT) -> None:
        self.label = label
        self._out = _CappedBuffer(limit)
        self._err = _CappedBuffer(limit)
        # A bounded `deque`, not a plain `list`: the OOM `_CappedBuffer` exists to prevent for
        # text is just as reachable through a runaway `logger.info(...)` in a loop — arguably more
        # so, since that's the more common shape a real suite's noise takes. `deque(maxlen=...)`
        # silently drops the *oldest* record once full, trading "no visible marker" for "near-zero
        # extra code" — deliberately simpler than `_CappedBuffer`'s head+tail-plus-marker shape:
        # unlike a capped text stream, dropping is not obviously more informative in one particular
        # place in a record list than another, so there is no clearly-right "which half do I keep"
        # answer to justify the extra machinery a marker record would need. `deque.append` is also
        # documented thread-safe, which matters here for the same reason `_CappedBuffer` now takes
        # its own lock: `_RoutingHandler.emit` can run on a `ContextPropagatingExecutor` worker
        # thread. Each retained `LogRecord` still pins its own `.args` alive for as long as it sits
        # in the deque — deliberately not stripped or reformatted here: `LogRecord` objects are
        # shared with every other handler on the same logger's propagation chain (this handler is
        # rarely the only one attached), so mutating `record.args`/`record.msg` in place to save
        # memory would be a `_RoutingHandler` (or its own caller's) side effect leaking into
        # whatever other handler happens to run after it — worse than the memory cost it would
        # save. The bound itself is what actually addresses the OOM concern the cap exists for;
        # unbounded *retention time* for individual objects logged by reference is accepted as the
        # cost of `log_records` staying genuinely structured (spec/09 §2's "structured
        # `LogRecord`s... not just formatted text"), same trade `caplog` itself makes.
        self.log_records: deque[logging.LogRecord] = deque(maxlen=DEFAULT_LOG_RECORD_LIMIT)
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
    setup/call/teardown envelope (including, for the module's last test, that module's own
    fixture teardown — see the module docstring's session-sink list). This is a `ContextVar.set`
    inside one `asyncio.Task`, not a module-global mutation (I1): every task gets its own
    independent copy of the context at creation (`asyncio.Task.__init__` calls
    `contextvars.copy_context()`), so `.set()` here can only ever be observed by `await`-reachable
    code in *this* test's own task tree.

    That last clause is also this docstring's one honest caveat: "this test's own task tree" is
    not the same set as "code that runs before `.reset(token)` fires". A task the test spawns via
    `create_task(...)` and never `await`s copies this `ContextVar` at *creation* time and keeps its
    own reference to this exact `TestContext` for as long as it runs — `.reset()` in the parent
    task cannot reach a child task's already-copied context, the same way setting a variable in a
    calling function can't retroactively change what a thread already holding a copy of it sees.
    Cross-*test* isolation is unaffected by this (no other test's `Sink` can ever receive that
    orphan's output — only *this* test's own, already-finished one can), but the orphan's writes
    are still real: if this test's `Sink` is never read again (the `PASSED` case, where captured
    output is dropped immediately per spec/09 §6), that output is silently lost, not merely
    delayed or misattributed. Not fixable inside this module — the real fix is spec/05 §2's
    per-test `TaskGroup` (a test's own background tasks becoming part of its envelope, cancelled or
    awaited before the test is considered finished), explicitly deferred, see `_run.py`'s own
    module docstring.
    """

    sink: Sink
    tags: tuple[str, ...]
    timeout: float | None
    worker: int


current_test_context: ContextVar[TestContext | None] = ContextVar(
    "velox_current_test_context", default=None
)


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
    `isatty`/`encoding`/`fileno` cover what stdlib `print`/`logging`/most libraries actually call.
    `fileno()` delegates straight to the real stream (writes that reach it bypass capture entirely
    and are unattributed by construction — no `Sink` in the world can see bytes written directly to
    an fd — but that is already true of any direct fd write, spec/09 §3's last table row, and
    plenty of ordinary code calls `fileno()` merely to check `os.isatty(...)` or pass it to
    `subprocess.run(stdout=...)`, so declining to answer would break more than it protects). Code
    that reaches for `sys.stdout.buffer` (binary-mode access) will still not find one; unlike
    `fileno()`, there is no small delegating answer for that one without this becoming a real fd
    proxy — that gap is the one fd-level `--isolated` capture is roadmapped to close (spec/09 §8).
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

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return self._real.fileno()

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
    This genuinely holds rather than merely looking like it does: see the comment at the
    `acquire()` call site in `_run.dispatch_one` for the exact call-site invariant (no `await`, and
    nothing that can raise, between admission and the `try`/`finally` that releases) this class's
    own correctness leans on without being able to enforce it itself.
    """

    __slots__ = ("_free",)

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


#: Dropped into every basetemp root this module creates (the numbered default *and* an explicit
#: `--basetemp` override) and checked before ever `rmtree`-ing one — the second half of the
#: belt-and-braces guard spec/09 §5's "documented 'this directory is cleared' warning" needs
#: (`cli.py`'s `_invalid_basetemp_argument` is the first half, rejecting the cwd/home/root/an
#: empty path outright before this module is ever reached). A help-text warning is not a guard
#: against an irreversible recursive delete; requiring this marker to already be present is what
#: makes the destructive path opt-in to directories velox itself made, rather than to whatever a
#: `--basetemp` typo happened to point at. Not a security boundary (trivially spoofable by anyone
#: who can write to the target directory) — just a guard against the ordinary mistake, the same
#: spirit as `_allocate_session_root`'s own name-pattern check on what it's willing to sweep.
BASETEMP_MARKER_NAME = ".velox-basetemp"


def _mark_as_basetemp(root: Path) -> None:
    (root / BASETEMP_MARKER_NAME).write_text("")


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
    _mark_as_basetemp(root)

    keep = {path for _, path in existing[-retention:]} if retention > 0 else set()
    for _, path in existing:
        if path not in keep:
            shutil.rmtree(path, ignore_errors=True)
    return root


def _resolve_basetemp_root(explicit: Path | None, *, retention: int) -> Path:
    """`--basetemp DIR` (cleared and recreated — the documented warning, spec/09 §5) if given,
    else a fresh numbered root under the platform temp dir with the retention policy applied.

    `explicit` is assumed to have already passed `cli.py`'s own path-shaped validation
    (`_invalid_basetemp_argument` — not the cwd, not an ancestor of it, not empty/`.`/`..`, not
    the home directory or the filesystem root) — that check is what stops the catastrophic
    mistakes (`--basetemp=`, `--basetemp=$HOME`); this function's own `BASETEMP_MARKER_NAME` check
    below is the second, independent layer, for direct callers of `install()`/this function that
    skip `cli.main` entirely (this package's own tests, an embedder) and for the case `cli.py`
    cannot rule out by shape alone: an existing directory that merely *looks* fine but was never
    actually a velox basetemp.
    """
    if explicit is not None:
        root = Path(explicit).expanduser()
        if root.exists():
            if not (root / BASETEMP_MARKER_NAME).is_file():
                raise ValueError(
                    f"--basetemp {root} already exists and does not look like a previous velox "
                    f"basetemp (no {BASETEMP_MARKER_NAME!r} marker file) -- refusing to delete "
                    f"it. Point --basetemp at a fresh path, or remove the directory yourself "
                    f"first if you're sure it's safe to clear."
                )
            shutil.rmtree(root)
        root.mkdir(parents=True)
        _mark_as_basetemp(root)
        return root
    return _allocate_session_root(_basetemp_default_root(), retention=retention)


#: What survives unescaped in a `tmp_path` directory name.
_UNSAFE_ID_CHARS = re.compile(r"[^0-9A-Za-z_.-]")
#: Conservative common filesystem component-length ceiling (well under ext4/APFS/NTFS's own
#: 255-byte limits, leaving room for the digest suffix and for `basetemp/` itself).
_MAX_COMPONENT_LEN = 120


def sanitize_test_id(test_id: str) -> str:
    """`test_id`, made safe as a single path component, injectively (modulo an actual blake2b
    collision) regardless of whether escaping was needed (spec/09 §5).

    Same starting point as `_collect._escape_segment` (deliberately — same problem, same fix):
    replace unsafe characters, then append a short digest of the *original* string. Unlike
    `_escape_segment`, the digest is appended *unconditionally*, not only when escaping changed
    something — an earlier version of this function did the latter, and it doesn't work: an id
    that happens to need no escaping can still collide with a *different* id's escaped-and-hashed
    output (`sanitize_test_id("a/b")` used to equal the literal string `"a_b_82badf67"`, so an
    already-safe id spelled exactly that way would have collided with `"a/b"`'s sanitized form).
    Appending the digest to every output, computed over the true original in every case, closes
    that: two different inputs can now only collide on a genuine hash collision, not on one
    happening to spell out the other's escaped form. The cost is cosmetic — even an already-safe
    basename like `TmpPathFactory.mktemp("data")` now gets an ugly hash suffix — which is a small
    price for a `tmp_path`/`mktemp` collision being cryptographically implausible instead of a
    one-line reproducer away.

    A test id long enough to still exceed a filesystem's component-length limit even after
    escaping and hashing (a heavily parametrized id with many long values) is truncated with a
    fresh digest suffix for the same reason: the truncation itself is lossy, so a digest computed
    over the untruncated original is what keeps two long ids sharing a truncated prefix apart.
    """
    escaped = _UNSAFE_ID_CHARS.sub("_", test_id)
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
    duration of one `run_suite` call.

    Idempotent, like `_rewrite.install`: calling this again before the matching `uninstall()`
    does not stack a second `Router` on top of the first (which would leave `sys.stdout`
    permanently wrapped after only the *inner* call's `uninstall()` ran) — it returns the live
    setup instead. A mismatched `passthrough`/`basetemp` on that second call raises rather than
    being silently discarded (I6): the live setup already committed to a `passthrough` mode and a
    `basetemp_root` neither can be changed out from under whatever already depends on them (a
    `tmp_path` a test already has a handle to, output already echoed or not), so a caller asking
    for something different is a real conflict, not a harmless no-op to swallow quietly. Asking
    for the *same* thing twice — or leaving an argument at its default, meaning "no opinion" — is
    fine and returns the existing setup, same as before.

    Every fallible step (right now, just resolving `basetemp_root`) runs *before* any process-
    global state is touched: `sys.stdout`/`sys.stderr` are swapped and the root log handler is
    added only once nothing left to do can still raise, so a failure here — an ordinary
    `--basetemp` typo, most likely — leaves the process exactly as it was, with nothing for
    `uninstall()` to need to undo. This is what actually makes "mirrors `_rewrite.py`'s idempotent
    install/uninstall pattern" (module docstring) true: `_rewrite.install` earns that claim the
    same way, doing all of its own fallible work (`plan(...)`, the cache probe) before its own
    first global mutation (`sys.meta_path.insert(0, hook)`).
    """
    global _installed
    if _installed is not None:
        requested_basetemp = None if basetemp is None else Path(basetemp).expanduser()
        mismatched = passthrough != _installed.passthrough or (
            requested_basetemp is not None and requested_basetemp != _installed.basetemp_root
        )
        if mismatched:
            raise RuntimeError(
                f"velox._capture.install() was already called with passthrough="
                f"{_installed.passthrough!r}, basetemp_root={_installed.basetemp_root!r} -- "
                f"this call asked for passthrough={passthrough!r}"
                + (f", basetemp={requested_basetemp!r}" if requested_basetemp is not None else "")
                + ". Call uninstall() first if you actually want to change either."
            )
        return _installed

    # The one fallible step, resolved first and before any global state is touched — see this
    # function's own docstring for why the ordering here is load-bearing, not incidental.
    basetemp_root = _resolve_basetemp_root(basetemp, retention=retention)

    session_sink = Sink(label="<unattributed>")
    real_stdout, real_stderr = cast(TextIO, sys.stdout), cast(TextIO, sys.stderr)
    router_out = Router(real_stdout, "stdout", session_sink, passthrough=passthrough)
    router_err = Router(real_stderr, "stderr", session_sink, passthrough=passthrough)
    sys.stdout = cast(Any, router_out)
    sys.stderr = cast(Any, router_err)

    log_handler = _RoutingHandler(session_sink)
    logging.getLogger().addHandler(log_handler)

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
