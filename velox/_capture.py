"""Routes captured stdout/stderr, logging, and `tmp_path` allocation for a running suite.

Attribution is by a single `ContextVar`: `_run.run_suite` sets one per test around that
test's whole setup/call/teardown envelope, and every writer here -- `Router`, the
logging handler, the builtin fixture providers -- just reads whichever `Sink` is
currently set and falls back to a session-level sink when nothing is. Because each
`asyncio` task holds its own independent copy of that context, concurrently-dispatched
tests can never observe or clobber each other's sink.
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


#: Default per-stream capture cap, applied independently to stdout and stderr so a
#: chatty stderr logger can't starve stdout's own budget. Measured in characters
#: (`len(str)`), not bytes -- a cheap approximation, good enough for a soft memory cap.
DEFAULT_CAPTURE_LIMIT = 4 * 1024 * 1024


@final
class _CappedBuffer:
    """One capped text stream: the first half of the budget kept as a permanent head,
    the last half as a rolling tail, with the middle dropped and replaced by a marker
    once anything has actually been dropped.

    Guarded by a lock: a test's `loop.run_in_executor(None, fn)` writes into this
    buffer from a worker thread while the test's own task writes from the loop
    thread.
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

            # A single write can exceed the whole tail budget on its own (a runaway
            # print of a huge repr) -- handled directly, keeping only its own trailing
            # slice, rather than relying on the pop-from-the-left loop below, which
            # would otherwise drop it wholesale one queued chunk at a time.
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
        # The marker means "something was actually dropped" (omitted > 0), not "the
        # head/tail split has started" -- conflating the two would print a false
        # "capture limit exceeded" banner over output that was actually kept in full.
        with self._lock:
            head, tail, omitted = "".join(self._head), "".join(self._tail), self._omitted
        if omitted <= 0:
            return head + tail
        marker = f"\n... [{omitted} characters omitted, capture limit exceeded] ...\n"
        return head + marker + tail


#: `Sink.log_records`'s own bound, independent of `DEFAULT_CAPTURE_LIMIT` since it
#: counts records, not characters.
DEFAULT_LOG_RECORD_LIMIT = 2000


@final
class Sink:
    """Everything captured for one test, or for the session: stdout, stderr, and the
    structured `LogRecord`s emitted while it was the active sink. One instance per
    test, created fresh in `_run.run_suite`'s `dispatch_one` and referenced only
    through `current_test_context`.

    `label` is the test id (or `"<unattributed>"` for the session sink) -- used only
    for `-s`'s per-line prefixing and for section headers, playing no role in
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
        # A bounded deque, not a plain list, caps memory the same way _CappedBuffer
        # does for text. deque.append is documented thread-safe, which matters since
        # _RoutingHandler.emit can run on a ContextPropagatingExecutor worker thread.
        self.log_records: deque[logging.LogRecord] = deque(maxlen=DEFAULT_LOG_RECORD_LIMIT)
        self._out_at_line_start = True
        self._err_at_line_start = True

    def write_out(self, s: str) -> None:
        self._out.write(s)

    def write_err(self, s: str) -> None:
        self._err.write(s)

    @property
    def out(self) -> str:
        """This sink's stdout so far, read live -- reflects writes made after a
        `capture` fixture holding this sink was injected, not a snapshot."""
        return self._out.getvalue()

    @property
    def err(self) -> str:
        return self._err.getvalue()


# ----------------------------------------------------------------------------------- Router

# One combined ContextVar rather than a bare Sink ContextVar: every reader below wants
# tags/timeout/worker alongside the sink, so `_run.run_suite`'s dispatch_one sets one
# value per test instead of several with independent reset bookkeeping.


@final
@dataclass(frozen=True, slots=True)
class TestContext:
    """The ambient, per-test-task state a builtin fixture provider needs beyond
    `_fixtures.BuiltinContext`: the current `Sink`, this test's tags/timeout, and which
    concurrency slot it occupies.

    Set once per test by `_run.run_suite`'s `dispatch_one`, via
    `current_test_context.set(...)`/`.reset(token)` wrapped around that test's whole
    setup/call/teardown envelope. This is a `ContextVar.set` inside one `asyncio.Task`,
    not a module-global mutation: every task gets its own independent copy of the
    context at creation, so `.set()` here can only ever be observed by code in this
    test's own task tree.
    """

    sink: Sink
    tags: tuple[str, ...]
    timeout: float | None
    worker: int


current_test_context: ContextVar[TestContext | None] = ContextVar(
    "velox_current_test_context", default=None
)


def _echo(real: TextIO, label: str, text: str, at_line_start: bool) -> bool:
    """Write `text` to `real` with `[label] ` prefixed at the start of each line, for
    `-s`'s passthrough mode. Returns the updated "at a line boundary" state for the
    next call.

    A single write is not guaranteed to be newline-aligned, so `at_line_start` is
    tracked across calls rather than naively prefixing every `\\n`-split segment --
    otherwise a chunk landing mid-line could get a spurious prefix, or miss the one it
    needs at the start of the next.
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
    """`sys.stdout`/`sys.stderr`'s replacement for the whole run. Installed once by
    `install()`, restored exactly by `uninstall()`. `write` asks
    `current_test_context` which `Sink` is active right now and hands the text to it.

    A duck-typed stream covering what stdlib `print`/`logging`/most libraries
    actually call. `fileno()` delegates to the real stream; writes that reach it
    directly bypass capture and are unattributed, the same as any direct fd write.
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
    """The one root `logging.Handler` for the whole run. `emit` retains the structured
    `LogRecord` on whichever `Sink` is active (same lookup as `Router`) and does
    nothing else -- no formatting, so a reporter can format lazily and let `-v` change
    format after the fact.

    Sits at `level=logging.NOTSET`: each logger's own effective level decides what
    reaches this handler.
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
    """`loop.set_default_executor(...)`'s value for the whole run.

    `submit` copies the calling task's `contextvars.Context` at submit time and runs
    `fn` inside it, so output from the executor thread is attributed to the test
    that submitted it.
    """

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        ctx = contextvars.copy_context()
        return super().submit(cast(Callable[..., Any], ctx.run), fn, *args, **kwargs)


# --------------------------------------------------------------------------- Concurrency slots


@final
class WorkerSlots:
    """A free-list of concurrency-slot indices `0..concurrency-1`, exposed to tests as
    `TestInfo.worker`. `acquire`/`release` are synchronous, plain list operations.
    """

    __slots__ = ("_free",)

    def __init__(self, concurrency: int) -> None:
        self._free: list[int] = list(range(concurrency))

    def acquire(self) -> int:
        return self._free.pop()

    def release(self, slot: int) -> None:
        self._free.append(slot)


# ------------------------------------------------------------------------------- tmp_path(s)

#: Keeping the previous 3 roots (plus the one just allocated) bounds disk usage across
#: repeated runs without losing the last few runs' artifacts for a post-mortem.
DEFAULT_BASETEMP_RETENTION = 3

_SESSION_DIR_RE = re.compile(r"^velox-(\d+)$")


def _current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        # A sandboxed environment can lack a resolvable username entirely; the
        # directory naming is cosmetic, not load-bearing, so fall back rather than raise.
        return "unknown"


def _basetemp_default_root() -> Path:
    return Path(tempfile.gettempdir()) / f"velox-of-{_current_user()}"


#: Dropped into every basetemp root this module creates, and checked before ever
#: rmtree-ing one -- makes the destructive path opt-in to directories velox itself
#: made, rather than to whatever a `--basetemp` typo happened to point at. Not a
#: security boundary (trivially spoofable), just a guard against the ordinary mistake.
BASETEMP_MARKER_NAME = ".velox-basetemp"


def _mark_as_basetemp(root: Path) -> None:
    (root / BASETEMP_MARKER_NAME).write_text("")


def _allocate_session_root(parent: Path, *, retention: int) -> Path:
    """One fresh, numbered `velox-<n>` directory under `parent`, applying the
    retention policy: only the most recent `retention` previous roots are kept.

    Two velox processes racing to allocate a session root under the same parent at the
    same instant could pick the same `n` -- a known, narrow TOCTOU window; the failure
    mode is a `FileExistsError` on the second `root.mkdir()`, not silent data loss.
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
    """`--basetemp DIR` (cleared and recreated) if given, else a fresh numbered root
    under the platform temp dir with the retention policy applied.

    `explicit` is assumed to have already passed `cli.py`'s own path-shaped validation.
    This function's own `BASETEMP_MARKER_NAME` check is a second, independent layer
    for direct callers that skip `cli.main` entirely.
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
#: Conservative common filesystem component-length ceiling (well under ext4/APFS/NTFS's
#: own 255-byte limits, leaving room for the digest suffix and for `basetemp/` itself).
_MAX_COMPONENT_LEN = 120


def sanitize_test_id(test_id: str) -> str:
    """`test_id`, made safe as a single path component, injectively (modulo an actual
    hash collision): unsafe characters are replaced, then a short digest of the
    original string is appended unconditionally, whether or not escaping changed
    anything, so a safe id can never collide with another id's escaped form.

    A test id long enough to still exceed a filesystem's component-length limit
    after escaping and hashing is truncated with a fresh digest over the
    untruncated original, so two long ids sharing a truncated prefix still can't
    collide.
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
    """What `install()` did, and the handles `uninstall()` needs to reverse it exactly.
    `session_sink`/`basetemp_root` are also what `run_suite` needs after the run: the
    unattributed-output section, and where `tmp_path` allocates."""

    session_sink: Sink
    basetemp_root: Path
    passthrough: bool
    router_out: Router
    router_err: Router
    log_handler: _RoutingHandler
    real_stdout: TextIO
    real_stderr: TextIO


#: What the currently-installed Router/handler decided, so a later idempotent
#: `install()` call -- or `uninstall()` -- has something to reverse. `None` whenever
#: nothing is installed; always cleared by `uninstall()`.
_installed: CaptureSetup | None = None


def install(
    *,
    passthrough: bool = False,
    basetemp: Path | None = None,
    retention: int = DEFAULT_BASETEMP_RETENTION,
) -> CaptureSetup:
    """Replace `sys.stdout`/`sys.stderr` with `Router`s and add the root logging
    handler, for the duration of one `run_suite` call.

    Idempotent: calling this again before the matching `uninstall()` returns the live
    setup rather than stacking a second `Router`. A mismatched `passthrough`/`basetemp`
    on that second call raises rather than being silently discarded, since the live
    setup already committed to values other code may depend on.

    Every fallible step (resolving `basetemp_root`) runs before any process-global
    state is touched, so a failure here -- an ordinary `--basetemp` typo, most likely
    -- leaves the process exactly as it was.
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

    # The one fallible step, resolved before any global state is touched.
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
    """Restore exactly what `install()` replaced, and forget it. Idempotent: a no-op
    if nothing is installed. Always safe to call from a `finally`."""
    global _installed
    if _installed is None:
        return
    sys.stdout = cast(Any, _installed.real_stdout)
    sys.stderr = cast(Any, _installed.real_stderr)
    logging.getLogger().removeHandler(_installed.log_handler)
    _installed = None


def installed() -> CaptureSetup | None:
    """The currently-installed setup, or `None`. Read-only introspection -- nothing in
    this package needs it besides the providers below and tests."""
    return _installed


def unattributed_sections(session_sink: Sink) -> list[str]:
    """The session sink's contents, formatted as zero or more report-ready text
    blocks. Empty in the common case, since only a genuinely detached background
    thread or end-of-run session-scope teardown ever write here."""
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
    """`current_test_context.get()`, or a loud `RuntimeError` -- never a silent
    `None`-shaped fallback. Every provider below runs only during a test's `setup()`,
    which `dispatch_one` always calls with `current_test_context` already set, so
    `None` here means a velox internal bug, not a user mistake.
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
    """Like `_require_test_context`, for the providers that only need
    `basetemp_root`. Checked independently since a `TestContext` being set and capture
    being installed are two different pieces of state with two different lifetimes.
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
    """`velox.capture`'s `BuiltinProvider`. `ctx` is unused -- everything this needs
    (the current `Sink`) comes from `current_test_context`, ambient per-task state."""
    del ctx
    tc = _require_test_context()
    return _builtins.Capture(tc.sink), None


async def log_records_provider(
    kwargs: Mapping[str, Any], ctx: BuiltinContext
) -> tuple[Any, _Closer | None]:
    """`velox.log_records`'s `BuiltinProvider`. Hands `LogRecords` the live list
    `_RoutingHandler.emit` appends to, not a copy, so records logged after injection
    are still visible."""
    del ctx
    tc = _require_test_context()
    return _builtins.LogRecords(tc.sink.log_records), None


async def test_info_provider(
    kwargs: Mapping[str, Any], ctx: BuiltinContext
) -> tuple[Any, _Closer | None]:
    """`velox.test_info`'s `BuiltinProvider`. `id`/`module_path` come from `ctx`;
    `tags`/`timeout`/`worker` come from `current_test_context`."""
    tc = _require_test_context()
    return (
        _builtins.TestInfo(id=ctx.test_id, tags=tc.tags, timeout=tc.timeout, worker=tc.worker),
        None,
    )


async def tmp_path_provider(
    kwargs: Mapping[str, Any], ctx: BuiltinContext
) -> tuple[Any, _Closer | None]:
    """`velox.tmp_path`'s `BuiltinProvider`: `basetemp/<sanitized-id>`, unique by
    construction."""
    setup = _require_installed()
    path = setup.basetemp_root / sanitize_test_id(ctx.test_id)
    path.mkdir(parents=True, exist_ok=True)
    return path, None


async def tmp_path_factory_provider(
    kwargs: Mapping[str, Any], ctx: BuiltinContext
) -> tuple[Any, _Closer | None]:
    """`velox.tmp_path_factory`'s `BuiltinProvider`. Session-scoped:
    `_di.ScopeStore`'s own single-flight cache, not this function, guarantees this
    runs exactly once per run."""
    del ctx
    setup = _require_installed()
    return _builtins.TmpPathFactory(setup.basetemp_root), None
