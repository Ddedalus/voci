"""`--watch`: rerun the suite whenever a `.py` file under its roots changes, instead of exiting
after one pass.

Built from three pieces, none of them new here except the middle one: the collection index
(`velox._collection.index`) is what makes a rerun cheap when nothing collection-relevant changed,
a poll loop over `discover_files`'s own walk (below) is the watcher, and `--lf` (`velox._collection
.lastfailed`) is what a change reruns first -- applied automatically from the second run on unless
the invocation already picked `--lf` or `--ff` for itself, in which case that choice is left alone
for every run. An invocation with nothing recorded, or one that just went green, runs the whole
suite under `--lf` regardless (`main`'s own `--lf` help text), so the common loop is: red, fix,
rerun (fast -- just the failures), green, rerun (the whole suite, confirming it).

`run_once` is `cli.main` itself, called again for every iteration -- see that function's own env/
sys.path/import-hook teardown for why repeated in-process calls are safe. Each call re-parses its
argv, re-resolves config and re-discovers files from scratch, so a config edit or a newly created
test file is picked up the same way restarting `velox` by hand would pick it up; only the set of
directories this module polls for a next change is fixed for the whole `--watch` session, at
`cli._watch_scope`'s approximation of what the first run's own resolution would give.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from velox._collection.discovery import discover_files

__all__ = ["run"]

#: How often, in seconds, the idle loop rescans for a change. Small enough that a save feels
#: instant, large enough that watching a large tree costs nothing anyone would notice.
_POLL_INTERVAL = 0.3

#: `cli.main`'s own exit code for "aborted by Ctrl-C" (its `except KeyboardInterrupt` clause). A
#: run stopped that way is the one status `--watch` never reruns from -- every other status,
#: including a usage error, is left in place for the next change to have another go at, since a
#: broken `[tool.velox]` table is exactly the kind of thing a following edit fixes.
_ABORTED = 2

_WATCHING_MESSAGE = "velox: watching for changes (Ctrl-C to stop)"

_Snapshot = dict[str, tuple[int, int]]


def run(
    *,
    base_argv: Sequence[str],
    apply_last_failed: bool,
    roots: Sequence[Path],
    ignore_dirs: frozenset[str],
    initial_wall_start: float,
    run_once: Callable[..., int],
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run `run_once(base_argv, wall_start=initial_wall_start)` once, then again every time a
    `.py` file under `roots` changes, and return whichever call's status was last.

    From the second call on, `--lf` is appended to `base_argv` when `apply_last_failed` says the
    invocation didn't already settle its own last-failed/failed-first ordering. `sleep` is a seam
    for tests, which drive the loop by having it mutate the watched tree and eventually raise
    `KeyboardInterrupt` instead of actually sleeping.
    """
    status = run_once(list(base_argv), wall_start=initial_wall_start)
    if status == _ABORTED:
        return status

    iteration_argv = [*base_argv, "--lf"] if apply_last_failed else list(base_argv)
    snapshot = _scan(roots, ignore_dirs)
    print(_WATCHING_MESSAGE, file=sys.stderr)
    try:
        while True:
            snapshot = _wait_for_change(roots, ignore_dirs, snapshot, sleep=sleep)
            status = run_once(list(iteration_argv), wall_start=time.monotonic())
            if status == _ABORTED:
                return status
            print(_WATCHING_MESSAGE, file=sys.stderr)
    except KeyboardInterrupt:
        # Ctrl-C while idle between runs, rather than during one -- run_once's own handler is
        # what catches one that lands mid-run. Nothing partial to report either way; just stop.
        print(file=sys.stderr)
        return status


def _scan(roots: Sequence[Path], ignore_dirs: frozenset[str]) -> _Snapshot:
    """Every `.py` file under `roots`, stat'd the same way the collection index stats a file
    it's deciding whether to trust: `(mtime_ns, size)`, keyed by absolute path. A file that
    can't be stat'd (removed between the walk and the stat call) contributes nothing -- its
    disappearance is exactly the kind of change the next `_wait_for_change` comparison should
    still catch, via the key vanishing from this snapshot.
    """
    snapshot: _Snapshot = {}
    for path in discover_files(roots, patterns=("*.py",), ignore_dirs=ignore_dirs):
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshot[str(path)] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def _wait_for_change(
    roots: Sequence[Path],
    ignore_dirs: frozenset[str],
    previous: _Snapshot,
    *,
    sleep: Callable[[float], None],
) -> _Snapshot:
    """Poll until `_scan` disagrees with `previous` -- a file edited, added or removed -- and
    return the snapshot that disagreed."""
    while True:
        sleep(_POLL_INTERVAL)
        current = _scan(roots, ignore_dirs)
        if current != previous:
            return current
