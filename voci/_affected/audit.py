"""M4's audit hook (`plans/affected-tests-plan.md`, Non-code dependencies design section):
`data:`/`dir:` dependencies from `open`/`os.listdir`/`os.scandir`/`sqlite3.connect`, and the
"a spawn makes its collector untrusted" rule.

Installed once per process (`sys.addaudithook`'s own contract: a hook can never be removed), but
inert whenever `install()` hasn't activated it for the run in progress -- `_run_state` is a
`ContextVar`, the same "current, per active run" shape `collector.current_collector` already uses,
so a nested or successive run (`_run/_isolated_worker.py`'s own subprocess, or a second `voci.cli.
main()` call in the same interpreter) only ever sees the innermost `install()`'s `rootdir`/
`session_start`, and the hook itself never needs re-adding. It's also a no-op with no current
collector at all (`collector.current_collector.get() is None`), same as `collector.
record_first_party` -- there is nothing to attribute an audited event to outside a test's or
fixture's own span.

**Never lets a bug here break the operation it observed.** `sys.audit()`'s own contract lets a
hook's exception veto the call it was raised from (a `RuntimeError` from an audit hook can turn a
plain `open()` into a crash) -- `_hook` therefore never lets anything escape its own `try`/`except
Exception`, the same "must never be the reason a run errors" posture `store.py`'s own location
fallback and `world.py`'s own `_read_source` already take, just for a place where the consequence
of getting it wrong is far worse than a coarser dependency.

`exempt_own_spawn` is the one deliberate carve-out from "process spawns are untrusted by default":
`@voci.isolated`'s own subprocess (`_run/isolated.py`) is voci's own controlled spawn, whose
dependencies are already accounted for by the `CollectorRecord` it ships back
(`_run/_isolated_worker.py`), not by anything this audit hook could see from the parent side --
without it, every `@voci.isolated` test would be marked untrusted purely for using the mechanism
the plan's own Failure modes table already credits with soundness ("isolated tests merge their
record").
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path

from voci._affected.collector import Collector, current_collector
from voci._affected.tracer import is_first_party, is_first_party_dir

__all__ = ["exempt_own_spawn", "install", "uninstall"]

#: `subprocess.Popen`'s own audit event, `_posixsubprocess.fork_exec` (which `multiprocessing`
#: calls directly), `os.posix_spawn`, `os.exec`, `os.system` and `os.fork` -- the Non-code
#: dependencies design section's own list. A single `subprocess.Popen(...)` call typically raises
#: both the first and the second of these (the Python-level call, then the C extension it calls
#: into) -- `Collector.mark_untrusted`'s own idempotence (the first reason wins) makes seeing the
#: same spawn twice harmless rather than something this module needs to dedupe itself.
_SPAWN_EVENTS = frozenset(
    {
        "subprocess.Popen",
        "_posixsubprocess.fork_exec",
        "os.posix_spawn",
        "os.exec",
        "os.system",
        "os.fork",
    }
)


@dataclass(frozen=True, slots=True)
class _RunState:
    rootdir: Path
    #: A file whose mtime is after this is something the run itself wrote (an output), not a
    #: dependency to record -- the Non-code dependencies design section's own "files modified
    #: after the session started" exclusion.
    session_start: float


_run_state: ContextVar[_RunState | None] = ContextVar("voci_affected_audit_run", default=None)
_exempt_spawn: ContextVar[bool] = ContextVar("voci_affected_audit_exempt_spawn", default=False)
_hook_installed = False


def install(rootdir: Path, session_start: float) -> Token[_RunState | None]:
    """Activate the audit hook for a run over `rootdir`, adding the process-wide `sys.addaudithook`
    callback the first time this is ever called in this interpreter. Returns a token for
    `uninstall` -- a `ContextVar.reset` token, opaque to every other caller."""
    global _hook_installed
    if not _hook_installed:
        sys.addaudithook(_hook)
        _hook_installed = True
    return _run_state.set(_RunState(rootdir.resolve(), session_start))


def uninstall(token: Token[_RunState | None]) -> None:
    """Deactivate the run `install()` returned `token` for. The process-wide hook itself is never
    removed -- it can't be (module docstring) -- it just goes back to seeing no active run, the
    same inert state as before the first `install()` call ever ran."""
    _run_state.reset(token)


@contextlib.contextmanager
def exempt_own_spawn() -> Iterator[None]:
    """Suppress the untrusted-marking a process spawn would otherwise give its collector, for the
    duration of the `with` block -- `_run/isolated.py`'s own `@voci.isolated` subprocess only (see
    module docstring). Nothing else in voci spawns a process on a test's behalf without the test
    (or a fixture) doing so itself, which is exactly the ordinary case this exempts nothing for."""
    token = _exempt_spawn.set(True)
    try:
        yield
    finally:
        _exempt_spawn.reset(token)


def _hook(event: str, args: tuple[object, ...]) -> None:
    # See the module docstring: nothing here may ever escape and veto the real operation.
    with contextlib.suppress(Exception):
        _dispatch(event, args)


def _dispatch(event: str, args: tuple[object, ...]) -> None:
    state = _run_state.get()
    if state is None:
        return
    collector = current_collector.get()
    if collector is None:
        return
    if event == "open":
        _handle_open(collector, state, args)
    elif event in ("os.listdir", "os.scandir"):
        _handle_dir(collector, state, args)
    elif event == "sqlite3.connect":
        _handle_sqlite(collector, state, args)
    elif event in _SPAWN_EVENTS:
        _handle_spawn(collector, event)


def _is_read_mode(mode: str | None, flags: int) -> bool:
    """Whether an `open` audit event describes a read, not a pure write/create/append -- the
    design section's own "Read-mode `open`" qualifier. `mode` is the builtin `open()`'s own string
    (`"r"`, `"rb"`, `"r+"`, ...) when the call went through it; `None` with `flags` set instead for
    `os.open`, whose `os.O_ACCMODE` bits say the same thing."""
    if mode is not None:
        return not any(flag in mode for flag in ("w", "a", "x"))
    return (flags & os.O_ACCMODE) != os.O_WRONLY


def _handle_open(collector: Collector, state: _RunState, args: tuple[object, ...]) -> None:
    file, mode, flags = args
    if isinstance(file, int):
        return  # a bare fd (os.fdopen and the like) -- no path to resolve
    if not isinstance(mode, str) and mode is not None:
        return
    if not isinstance(flags, int):
        return
    if not _is_read_mode(mode, flags):
        return
    if not isinstance(file, (str, bytes, os.PathLike)):
        return
    _record_data(collector, state, file)


def _handle_sqlite(collector: Collector, state: _RunState, args: tuple[object, ...]) -> None:
    (database,) = args
    # sqlite3.connect accepts a path-like, not just a str -- unlike `open`'s own audit event,
    # nothing about this one is specific to `str`/`bytes`.
    if not isinstance(database, (str, bytes, os.PathLike)):
        return
    if os.fsdecode(database) == ":memory:":
        return
    _record_data(collector, state, database)


def _record_data(
    collector: Collector, state: _RunState, file: str | bytes | os.PathLike[str]
) -> None:
    try:
        resolved = Path(os.fsdecode(file)).resolve()
    except (TypeError, OSError):
        return
    if resolved.suffix == ".py":
        return  # already tracked as source, per DefKey/NameKey -- see the design section
    if not is_first_party(str(resolved), state.rootdir):
        return
    try:
        if resolved.stat().st_mtime > state.session_start:
            return  # written by this run -- an output, not an input
    except OSError:
        return
    collector.record_data(str(resolved))


def _handle_dir(collector: Collector, state: _RunState, args: tuple[object, ...]) -> None:
    (path,) = args
    if path is None:
        path = os.getcwd()
    if isinstance(path, int):
        return
    if not isinstance(path, (str, bytes, os.PathLike)):
        return
    try:
        resolved = Path(os.fsdecode(path)).resolve()
    except (TypeError, OSError):
        return
    if not is_first_party_dir(str(resolved), state.rootdir):
        return
    collector.record_dir(str(resolved))


def _handle_spawn(collector: Collector, event: str) -> None:
    if _exempt_spawn.get():
        return
    collector.mark_untrusted(f"process spawn ({event})")
