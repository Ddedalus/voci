"""The `sys.monitoring` tracer behind affected-test selection (see `plans/affected-tests-plan.md`,
Tracer): a `PY_START` callback that classifies whether running code is first-party, plus the tool
id's own lifecycle.

Only classification lives here -- what to do with a classified code object (attribute it to a
test, a fixture, or nothing at all; mark a test untrusted) is the next pieces of M1, still to
come. `Tracer` is handed a plain callback for "a first-party code object just started running"
rather than assuming any particular collector shape, so this module stays usable on its own.

Non-first-party code is told to `DISABLE` itself for this tool -- sound at any concurrency,
because whether a file is first-party never depends on which test happens to be running. Doing
the same for first-party code would not be: see the plan's "Why not DISABLE everywhere".
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from types import CodeType

from voci._cache import CACHE_DIR_NAME

__all__ = [
    "Tracer",
    "is_first_party",
    "is_first_party_dir",
]

#: `sys.monitoring` reserves 0/1/2/5 for the debugger, coverage, the profiler and the optimizer
#: (`DEBUGGER_ID`, `COVERAGE_ID`, `PROFILER_ID`, `OPTIMIZER_ID`); 3 and 4 are the ids every
#: version of CPython leaves free for a tool like this one.
_CANDIDATE_TOOL_IDS = (3, 4)

_TOOL_NAME = "voci-affected"


class Tracer:
    """Owns one `sys.monitoring` tool id for the life of a run.

    `on_first_party(code)` runs every time a first-party code object starts running -- once per
    `PY_START`, not once per code object, since `DISABLE`-ing first-party code is unsound under
    concurrency (see the plan's "Why not DISABLE everywhere"). A hot loop's def therefore calls
    it on every iteration; a caller that only cares about which code objects ran at all, not how
    often, dedupes by `id(code)` on its own side (the plan's Recording bullet), which a plain
    dict assignment already does for free. Everything non-first-party returns `DISABLE` instead
    and is never seen again.
    """

    def __init__(self, rootdir: Path, on_first_party: Callable[[CodeType], None]) -> None:
        self.rootdir = rootdir.resolve()
        self._on_first_party = on_first_party
        #: `co_filename -> is first-party`, so only the first `PY_START` from a given file pays
        #: for `is_first_party`'s filesystem checks.
        self._first_party: dict[str, bool] = {}
        self.tool_id: int | None = None

    def start(self) -> str | None:
        """Claims the first free candidate tool id and starts watching `PY_START`. Returns
        `None` on success, or a reason a caller should fall back to a full run: every candidate
        id is already taken, by a debugger, `coverage run` under `COVERAGE_CORE=sysmon`, or
        another voci process sharing this interpreter."""
        mon = sys.monitoring
        for candidate in _CANDIDATE_TOOL_IDS:
            try:
                mon.use_tool_id(candidate, _TOOL_NAME)
            except ValueError:
                continue
            self.tool_id = candidate
            break
        else:
            return "no sys.monitoring tool id is free"
        mon.register_callback(self.tool_id, mon.events.PY_START, self._callback)
        mon.set_events(self.tool_id, mon.events.PY_START)
        return None

    def stop(self) -> None:
        """Undoes `start()`. A no-op if `start()` never claimed an id, so a caller can always
        call this in a `finally` without checking whether `start()` succeeded first."""
        if self.tool_id is None:
            return
        mon = sys.monitoring
        mon.set_events(self.tool_id, mon.events.NO_EVENTS)
        mon.register_callback(self.tool_id, mon.events.PY_START, None)
        mon.free_tool_id(self.tool_id)
        self.tool_id = None

    def _callback(self, code: CodeType, instruction_offset: int) -> object:
        del instruction_offset  # part of PY_START's required signature, unused here
        first_party = self._first_party.get(code.co_filename)
        if first_party is None:
            first_party = is_first_party(code.co_filename, self.rootdir)
            self._first_party[code.co_filename] = first_party
        if not first_party:
            return sys.monitoring.DISABLE
        self._on_first_party(code)
        return None


def is_first_party(filename: str, rootdir: Path) -> bool:
    """Whether `filename` -- a `code.co_filename`, not yet known to name a real file -- is
    first-party source under `rootdir`.

    Excluded: anything that isn't a real file on disk (`<string>`, a zipimport member, a
    pyc-only module with no source left); anything under the running interpreter's own
    `sys.prefix`/`sys.base_prefix`/`sys.exec_prefix` (distinct on Debian-family systems, which
    split platform-specific stdlib into `exec_prefix` -- `_assertions/rewrite.py`'s own
    `skip_roots` checks the same three); anything under a directory holding a `pyvenv.cfg`
    between it and `rootdir` (a venv nested inside rootdir -- pytest-testmon #206); and
    `.voci_cache`.
    """
    if not filename or filename[0] == "<":
        return False
    try:
        resolved = Path(filename).resolve()
    except OSError:
        return False
    if not resolved.is_file():
        return False
    rootdir = rootdir.resolve()
    if not resolved.is_relative_to(rootdir):
        return False
    return not _excluded(resolved, rootdir, venv_start=resolved.parent)


def is_first_party_dir(dirname: str, rootdir: Path) -> bool:
    """Whether `dirname` -- a directory the audit hook saw listed via `os.listdir`/`os.scandir`
    (`_affected/audit.py`'s own `dir:` dependency, Non-code dependencies design section) -- is a
    first-party directory under `rootdir`: the same exclusions `is_first_party` applies to a file,
    minus the "must be a file" check its own name promises."""
    if not dirname:
        return False
    try:
        resolved = Path(dirname).resolve()
    except OSError:
        return False
    if not resolved.is_dir():
        return False
    rootdir = rootdir.resolve()
    if not resolved.is_relative_to(rootdir):
        return False
    return not _excluded(resolved, rootdir, venv_start=resolved)


def _excluded(resolved: Path, rootdir: Path, *, venv_start: Path) -> bool:
    """Whether `resolved` -- already confirmed an existing path under `rootdir` -- sits somewhere
    neither `is_first_party` nor `is_first_party_dir` may ever attribute to the project: under the
    running interpreter's own prefix, under a nested venv, or under the cache dir. `venv_start` is
    where `_venv_between`'s own upward walk begins: `resolved.parent` for a file (a file can't
    itself hold a `pyvenv.cfg`, only its directory can), `resolved` itself for a directory --
    `os.listdir()`ing a venv's own root, not just something inside one, must be excluded too."""
    if CACHE_DIR_NAME in resolved.relative_to(rootdir).parts:
        return True
    interpreter_roots = (sys.prefix, sys.base_prefix, sys.exec_prefix)
    if any(resolved.is_relative_to(Path(root).resolve()) for root in interpreter_roots):
        return True
    return _venv_between(venv_start, rootdir)


def _venv_between(start: Path, stop: Path) -> bool:
    """Whether any directory from `start` up to and including `stop` holds a `pyvenv.cfg`.
    `start` is always a descendant of `stop`, since this is only called after `rootdir` was
    already confirmed an ancestor of the file in question, so walking `.parent` from `start`
    reaches `stop` in a bounded number of steps."""
    current = start
    while True:
        if (current / "pyvenv.cfg").is_file():
            return True
        if current == stop:
            return False
        current = current.parent
