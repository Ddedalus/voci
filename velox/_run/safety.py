"""Runtime safety: the three things a run does about a test that misbehaves in a way ordinary
pass/fail reporting can't see.

A blocking call inside an `async def` test -- or inside a fixture, which velox constructs on the
loop -- holds the event loop, so every other concurrently dispatched test stops making progress
at once. `LoopWatchdog` watches the loop's heartbeat from a daemon thread and, when it stops,
prints the stack of whatever is holding it, naming the test the blocking frame belongs to. The
same stack machinery names a sync test still stuck in a blocking call in its worker thread once
the run is over (`stuck_calls`), which is otherwise a process that simply refuses to exit.

A test that returns a value, or that calls an `async def` function and forgets to `await` it,
passes while asserting nothing. `call_misuse` turns both into a failure: `watch_unawaited`
collects the "coroutine ... was never awaited" `RuntimeWarning`s raised while one test's call
phase is running (CPython raises them the moment the last reference to the coroutine drops, so
they land inside the phase that created them), and `install` is what routes those warnings here
for the duration of a run.

Everything here is wired up by `run.py`; nothing in this module knows what a `TestResult` is.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import threading
import time
import traceback
import warnings
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from types import CodeType
from typing import Any, final

from velox._assertions._vendor.saferepr import saferepr

__all__ = [
    "DEFAULT_LOOP_WATCHDOG",
    "LoopWatchdog",
    "Misuse",
    "Unawaited",
    "call_misuse",
    "install",
    "stuck_calls",
    "track_sync_call",
    "uninstall",
    "watch_unawaited",
]

#: Default seconds the event loop may go unresponsive before `LoopWatchdog` says so. Long
#: enough that an ordinary slow fixture (which velox does construct on the loop) doesn't trip
#: it, short enough that a stalled suite is named rather than waited out.
DEFAULT_LOOP_WATCHDOG = 5.0

#: How many stack frames a stall or stuck-call report prints, innermost last. Enough to see the
#: call chain that reached the blocking call without pasting a whole framework's internals.
_STACK_FRAMES = 6

#: Where velox's own frames live. A reported stack is cut at the innermost of them: everything
#: outside is velox dispatching a test (and asyncio dispatching velox), everything inside is the
#: test's own call chain, which is the only part that answers "what is it blocked on".
_VELOX_DIR = str(Path(__file__).resolve().parent.parent)


# ------------------------------------------------------------------------------ stack naming


def _thread_stack(ident: int) -> list[traceback.FrameSummary]:
    """`ident`'s current Python stack from the test's own frame inwards, innermost last. Empty
    if that thread has since finished.

    Falls back to the raw innermost frames when the cut leaves nothing -- velox blocking its own
    loop, with no test frame between it and the blocking call, is still worth seeing.

    Reading another thread's frame from this one is a snapshot of a moving target by
    definition -- the point is naming what it was doing, not a consistent view.
    """
    frame = sys._current_frames().get(ident)
    if frame is None:
        return []
    frames = list(traceback.walk_stack(frame))
    below_velox: list[tuple[Any, int]] = []
    for candidate, lineno in frames:
        if candidate.f_code.co_filename.startswith(_VELOX_DIR):
            break
        below_velox.append((candidate, lineno))
    stack = traceback.StackSummary.extract(reversed(below_velox or frames))
    return stack[-_STACK_FRAMES:]


def _format_stack(stack: Sequence[traceback.FrameSummary]) -> list[str]:
    """`stack` as indented `File "...", line N, in f` lines, source line included -- the
    traceback module's own format, so an editor's "jump to the file:line in this output" still
    works on it."""
    return [f"  {line}" for line in "".join(traceback.format_list(stack)).splitlines()]


def _owning_test(ident: int, test_ids: Mapping[CodeType, str]) -> str | None:
    """The id of the test whose own function frame is on `ident`'s stack, innermost first, or
    `None` if none is -- the loop can be blocked by something no single test owns (a fixture
    two tests share, session teardown), and a code object two records share (one `@velox
    .parametrize`d function, many cases) can't be attributed to either of them."""
    frame = sys._current_frames().get(ident)
    while frame is not None:
        test_id = test_ids.get(frame.f_code)
        if test_id is not None:
            return test_id
        frame = frame.f_back
    return None


def code_index(records: Sequence[Any]) -> dict[CodeType, str]:
    """`{code object: test id}` for every record whose function is uniquely identifiable by its
    code object, for naming the test a blocked stack belongs to. A code object shared by more
    than one record -- every case of a parametrized test -- is left out rather than attributed
    to whichever record happened to come first.
    """
    index: dict[CodeType, str] = {}
    shared: set[CodeType] = set()
    for record in records:
        code = getattr(record.func, "__code__", None)
        if code is None:
            continue
        if code in index:
            shared.add(code)
        index[code] = record.id
    for code in shared:
        del index[code]
    return index


# -------------------------------------------------------------------------------- watchdog


@final
class LoopWatchdog:
    """Reports the event loop going unresponsive for longer than `threshold` seconds.

    The loop re-arms a `call_later` heartbeat every `threshold / 4` seconds; a daemon thread
    reads the timestamp that heartbeat leaves behind and, once it is `threshold` seconds stale,
    dumps the loop thread's stack through `report`. That stack is the only place the answer
    lives: from the loop's own perspective nothing is happening at all, which is exactly why an
    unwatched stall reads as the suite mysteriously hanging.

    One report per stall, plus one line when the loop comes back -- a loop blocked for minutes
    is one problem, not one problem per poll.
    """

    def __init__(
        self,
        threshold: float,
        *,
        report: Callable[[str], None],
        test_ids: Mapping[CodeType, str] | None = None,
        in_flight: Callable[[], int] = lambda: 0,
    ) -> None:
        self._threshold = threshold
        self._report = report
        self._test_ids = dict(test_ids or {})
        self._in_flight = in_flight
        # Clamped at both ends: fine enough to notice the stall promptly at a small threshold,
        # never more often than once a second at a large one.
        self._interval = min(max(threshold / 4, 0.05), 1.0)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread = 0
        self._beat = 0.0
        self._stalled_at: float | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Arm the heartbeat and start watching. Must be called from inside the loop this
        watches -- that call is what identifies the thread whose stack gets dumped."""
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._beat = time.monotonic()
        self._schedule()
        self._thread = threading.Thread(target=self._watch, name="velox-loop-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Disarm the heartbeat and join the watching thread. Safe from outside the loop, and
        safe to call when `start` never ran."""
        self._done.set()
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        thread, self._thread = self._thread, None
        if thread is not None:
            # The thread only ever waits on `_done`, which is now set, so this is a formality
            # -- bounded anyway rather than trusting that on a machine under load.
            thread.join(timeout=self._interval + 1.0)

    def _schedule(self) -> None:
        if self._loop is not None and not self._done.is_set():
            self._timer = self._loop.call_later(self._interval, self._tick)

    def _tick(self) -> None:
        """The heartbeat itself, running on the loop: a timestamp and a re-arm. Anything that
        keeps the loop from reaching this is what the watching thread reports."""
        now = time.monotonic()
        stalled_at = self._stalled_at
        if stalled_at is not None:
            self._stalled_at = None
            self._report(f"velox: the event loop is running again after {now - stalled_at:.1f}s")
        self._beat = now
        self._schedule()

    def _watch(self) -> None:
        """The daemon thread's whole body. Reads `_beat` -- written by the loop thread, one
        plain attribute assignment, so no lock is involved on either side."""
        while not self._done.wait(self._interval):
            blocked = time.monotonic() - self._beat
            if blocked < self._threshold or self._stalled_at is not None:
                continue
            self._stalled_at = time.monotonic() - blocked
            self._report(self._describe(blocked))

    def _describe(self, blocked: float) -> str:
        lines = [f"velox: the event loop has been blocked for {blocked:.1f}s"]
        owner = _owning_test(self._loop_thread, self._test_ids)
        in_flight = self._in_flight()
        who = f"{owner} is holding it" if owner is not None else "something is holding it"
        lines.append(
            f"  {who}; nothing else runs until it returns "
            f"({in_flight} {'test' if in_flight == 1 else 'tests'} in flight):"
        )
        stack = _thread_stack(self._loop_thread)
        lines.extend(_format_stack(stack) if stack else ["  (the loop thread has since ended)"])
        lines.append(
            "  A blocking call in an `async def` test, or in a fixture, holds the loop for the "
            "whole suite."
        )
        lines.append(
            "  Move the work into a sync `def` test (velox runs those in a worker thread), or "
            "await asyncio.to_thread(...)."
        )
        return "\n".join(lines)


# ------------------------------------------------------------- sync calls, and where they stick

#: `{thread ident: test id}` for every sync test call currently running in a worker thread.
#: Written from the worker threads themselves; `dict` item assignment and deletion are atomic,
#: so the reader (`stuck_calls`, after the run) needs no lock.
_sync_calls: dict[int, str] = {}


def track_sync_call[T](test_id: str, call: Callable[[], T]) -> Callable[[], T]:
    """`call`, wrapped so that while it runs its worker thread is attributable to `test_id`.

    A sync test's blocking call cannot be interrupted -- cancelling the future that awaits it
    abandons it, it does not stop it -- so a run can end with one still going. This is what lets
    `stuck_calls` name it afterwards instead of leaving a process that won't exit unexplained.
    """

    def tracked() -> T:
        ident = threading.get_ident()
        _sync_calls[ident] = test_id
        try:
            return call()
        finally:
            _sync_calls.pop(ident, None)

    return tracked


def stuck_calls() -> str | None:
    """A report naming every sync test still running in a worker thread, with the stack of the
    call it's stuck in -- or `None`, the ordinary case, when none is.

    Called once the run is over, where a still-running worker thread means one thing: the
    interpreter will not exit until that call returns on its own. Python offers no way to
    interrupt a thread, so naming it is the whole remedy.
    """
    still_running = sorted(_sync_calls.items(), key=lambda item: item[1])
    if not still_running:
        return None
    count = len(still_running)
    lines = [
        f"velox: {count} sync {'test is' if count == 1 else 'tests are'} still running in a "
        f"worker thread and cannot be interrupted:"
    ]
    for ident, test_id in still_running:
        lines.append(f"  {test_id}, blocked at")
        stack = _thread_stack(ident)
        lines.extend(_format_stack(stack) if stack else ["  (it finished while this was printed)"])
    lines.append("  The process exits once the blocking call returns.")
    return "\n".join(lines)


# --------------------------------------------------------------- un-awaited coroutine warnings

#: The exact tail CPython's own warning ends in, matched rather than searched for: it is also
#: what `Unawaited.what` is the message with removed.
_NEVER_AWAITED = " was never awaited"


@final
@dataclass(frozen=True, slots=True)
class Unawaited:
    """One coroutine a test created and never awaited: `what` names it the way CPython's own
    warning does (`coroutine 'fetch_user'`), `where` is the file and line its last reference
    went away at -- the statement that dropped it, which for a coroutine held in a local is the
    end of the test."""

    what: str
    where: str

    def __str__(self) -> str:
        return f"{self.what}{_NEVER_AWAITED} ({self.where})"


#: Where `_showwarning` files an un-awaited coroutine while one test's call phase is running.
#: A `ContextVar`, so the executor thread a sync test's body runs in sees the same list (the
#: context is copied at submit time by `_capture.ContextPropagatingExecutor`) while a
#: concurrently dispatched test's task sees its own.
_unawaited: ContextVar[list[Unawaited] | None] = ContextVar(
    "velox_unawaited_coroutines", default=None
)

#: `warnings.catch_warnings()` entered by `install` and exited by `uninstall`: it saves and
#: restores `warnings.filters` and `warnings.showwarning` together, invalidating the per-module
#: warning registries on both ends the way hand-restoring the two lists would not.
_warnings_scope: warnings.catch_warnings | None = None
_previous_showwarning: Any = None


def install() -> bool:
    """Route "coroutine ... was never awaited" `RuntimeWarning`s to whichever test's call phase
    is running, for the duration of one run. Returns whether this call is the one that installed
    it -- `False` if an enclosing run already did, whose `uninstall` is then not this caller's
    to do.

    The warning is filtered to `always` rather than left at Python's default of once per source
    location: a parametrized test that forgets an `await` forgets it at the same line in every
    case, and only the first would otherwise be reported.
    """
    global _warnings_scope, _previous_showwarning
    if _warnings_scope is not None:
        return False
    scope = warnings.catch_warnings()
    scope.__enter__()
    _previous_showwarning = warnings.showwarning
    warnings.filterwarnings(
        "always", category=RuntimeWarning, message="coroutine .* was never awaited"
    )
    warnings.showwarning = _showwarning
    _warnings_scope = scope
    return True


def uninstall() -> None:
    """Restore what `install` replaced. Idempotent, and safe from a `finally`."""
    global _warnings_scope, _previous_showwarning
    scope, _warnings_scope = _warnings_scope, None
    _previous_showwarning = None
    if scope is not None:
        scope.__exit__(None, None, None)


def _showwarning(
    message: Warning | str,
    category: type[Warning],
    filename: str,
    lineno: int,
    file: Any = None,
    line: str | None = None,
) -> None:
    """`warnings.showwarning`'s replacement for the run. Files an un-awaited coroutine against
    the running test and swallows it -- the test's own failure says it better than a warning
    printed into whatever output the test was producing at the time. Everything else, including
    an un-awaited coroutine with no test to attribute it to, goes where it was already going.
    """
    collected = _unawaited.get()
    if collected is not None and _is_never_awaited(message, category):
        collected.append(
            Unawaited(
                what=str(message).removesuffix(_NEVER_AWAITED),
                where=f"{_short_path(filename)}:{lineno}",
            )
        )
        return
    _previous_showwarning(message, category, filename, lineno, file, line)


def _is_never_awaited(message: Warning | str, category: type[Warning]) -> bool:
    text = str(message)
    return (
        issubclass(category, RuntimeWarning)
        and text.startswith("coroutine ")
        and text.endswith(_NEVER_AWAITED)
    )


@contextmanager
def watch_unawaited() -> Iterator[list[Unawaited]]:
    """Collect the coroutines dropped un-awaited while this context is open.

    CPython reports one the moment its last reference goes away, which for a coroutine created
    and forgotten inside a test body is that statement, and for one held in a local is the frame
    dying as the test returns -- both inside the call phase this wraps. A coroutine kept alive
    past that (parked on a module global, say) is reported against whatever test happens to be
    running when it is finally collected, or against nobody once the run is over.
    """
    collected: list[Unawaited] = []
    token = _unawaited.set(collected)
    try:
        yield collected
    finally:
        _unawaited.reset(token)


# ------------------------------------------------------------------------- the call phase's own


@final
@dataclass(frozen=True, slots=True)
class Misuse:
    """A call phase that raised nothing and still didn't test anything: `detail` is the failure
    text, `summary` its one-line form for the short test summary."""

    detail: str
    summary: str


def call_misuse(returned: object, unawaited: Sequence[Unawaited]) -> Misuse | None:
    """The failure a call phase earns for what it did *besides* raising, or `None` if it earned
    none: a returned value, coroutines it never awaited, or both.

    Both mean the same thing in practice -- a test that ran to the end while checking nothing --
    and neither surfaces as an exception, so without this they read as a pass. A returned
    coroutine is closed here as it is reported: it is un-awaited by definition, and leaving it
    to be collected later would raise a second, duplicate warning against whichever test happens
    to be running by then.
    """
    details: list[str] = []
    summaries: list[str] = []

    if returned is not None:
        awaitable_name = _awaitable_name(returned)
        if awaitable_name is not None:
            if inspect.iscoroutine(returned):
                returned.close()
            summaries.append(f"test returned {awaitable_name} instead of None -- a missing await?")
            details.append(
                f"test returned {awaitable_name} instead of None.\n"
                f"An `async def` function that is called but not awaited never runs; velox "
                f"does not await what a test hands back."
            )
        else:
            summaries.append(f"test returned {saferepr(returned, maxsize=80)} instead of None")
            details.append(
                f"test returned {saferepr(returned)} instead of None.\n"
                f"velox never inspects what a test returns. If that value was the check, "
                f"`assert` it; otherwise drop the `return`."
            )

    if unawaited:
        count = len(unawaited)
        summaries.append(
            f"{unawaited[0].what} was never awaited"
            if count == 1
            else f"{count} coroutines were never awaited: "
            + ", ".join(entry.what for entry in unawaited[:3])
        )
        listed = "\n".join(f"  {entry}" for entry in unawaited)
        details.append(
            f"{count} {'coroutine was' if count == 1 else 'coroutines were'} created and never "
            f"awaited during this test:\n{listed}\n"
            f"An `async def` function that is called but not awaited never runs, so whatever it "
            f"was meant to check went unchecked."
        )

    if not details:
        return None
    return Misuse(detail="\n\n".join(details), summary="; ".join(summaries))


def _awaitable_name(value: object) -> str | None:
    """`coroutine 'fetch_user'` for a coroutine, the same shape for any other awaitable, or
    `None` for something that isn't one at all. Named rather than `repr`d: a coroutine's repr
    carries a memory address, which is noise in a failure message and different every run."""
    if not hasattr(value, "__await__"):
        return None
    name = getattr(value, "__qualname__", None) or getattr(value, "__name__", None)
    kind = "coroutine" if inspect.iscoroutine(value) else type(value).__name__
    return f"{kind} {name!r}" if name else f"a {kind}"


def _short_path(filename: str) -> str:
    """`filename` relative to the current directory when it's under it, else as given -- the
    same "nearby paths read as nearby" treatment `cli` gives the paths it prints."""
    try:
        relative = os.path.relpath(filename, Path.cwd())
    except ValueError:
        return filename
    return filename if relative.startswith("..") else relative
