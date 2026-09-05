"""The run cache under `rootdir/.velox_cache`: which tests failed last time, and which files
failed to collect at all.

Written once at the end of every run that executed tests, read at the start of a run that asks
for `--lf`/`--ff` (`velox._collection.lastfailed`). Every filesystem failure here is swallowed:
an unreadable, corrupt or version-mismatched cache reads as "nothing recorded", and an
unwritable tree costs the next `--lf` its ordering rather than the run its exit code.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Container, Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CACHE_DIR_NAME",
    "NOTHING_RECORDED",
    "LastRun",
    "ensure_gitignore",
    "load",
    "merge",
    "save",
    "update",
]

#: Shares the directory the assertion rewriter caches its pycs in (`rewrite.resolve_cache_dir`),
#: so a project has one velox-owned directory to ignore rather than two.
CACHE_DIR_NAME = ".velox_cache"

_LAST_RUN_FILE = "lastfailed.json"

#: Bumped when the payload's shape changes. A file written by any other version is discarded
#: whole rather than read defensively field by field.
_SCHEMA_VERSION = 1

_GITIGNORE = "# Created by velox automatically.\n*\n"


@dataclass(frozen=True, slots=True)
class LastRun:
    """What the previous run left behind for `--lf`/`--ff` to act on.

    `failed` holds test ids; `error_files` holds the rootdir-relative paths of files that
    raised during collection, which have no ids to name and so are replayed whole.
    """

    failed: tuple[str, ...] = ()
    error_files: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not self.failed and not self.error_files


NOTHING_RECORDED = LastRun()


def load(rootdir: Path) -> LastRun:
    """What `rootdir`'s cache says the last run found, or `NOTHING_RECORDED`."""
    path = rootdir / CACHE_DIR_NAME / _LAST_RUN_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return NOTHING_RECORDED
    if not isinstance(payload, dict) or payload.get("version") != _SCHEMA_VERSION:
        return NOTHING_RECORDED
    return LastRun(
        failed=_strings(payload.get("failed")), error_files=_strings(payload.get("error_files"))
    )


def save(rootdir: Path, last_run: LastRun) -> None:
    """Replace `rootdir`'s cache with `last_run`, atomically."""
    directory = rootdir / CACHE_DIR_NAME
    payload = {
        "version": _SCHEMA_VERSION,
        "failed": list(last_run.failed),
        "error_files": list(last_run.error_files),
    }
    # A distinct name per process, replaced into place: two velox runs sharing a rootdir must
    # not read a half-written file, and on POSIX `os.replace` is what makes that impossible.
    temporary = directory / f"{_LAST_RUN_FILE}.{os.getpid()}"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _write_gitignore(directory)
        temporary.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
        os.replace(temporary, directory / _LAST_RUN_FILE)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


def update(
    rootdir: Path,
    *,
    failed: Iterable[str],
    errored: Iterable[str],
    settled_ids: Container[str],
    settled_files: Container[str],
) -> None:
    """Merge this run's findings into `rootdir`'s cache and write it back.

    The baseline is re-read here rather than reusing the one loaded at startup. Two runs sharing
    a rootdir both merge into a whole payload and the second to finish wins, so merging against a
    baseline from before the run leaves the window for a lost update open for the entire length
    of the run -- long enough that an editor running a scoped suite while a full run finishes in
    a terminal drops the full run's new failures, silently and into a cache that still looks
    plausible. A run's own findings don't depend on the baseline, so the freshest one on disk is
    strictly the better thing to merge into: whatever the other run recorded is not in this run's
    `settled_*` and so is carried forward, which is the same rule that already keeps a `--maxfail`
    stop from erasing the rest of the suite.

    This narrows the window to the gap between the read and the `os.replace` rather than closing
    it, which is the trade the module means to make: a lock here would sit on the exit path of a
    run that has already reported its result, to buy the last microseconds of a race whose cost
    is a `--lf` that misses a failure the next run re-finds.
    """
    save(
        rootdir,
        merge(
            load(rootdir),
            failed=failed,
            errored=errored,
            settled_ids=settled_ids,
            settled_files=settled_files,
        ),
    )


def merge(
    previous: LastRun,
    *,
    failed: Iterable[str],
    errored: Iterable[str],
    settled_ids: Container[str],
    settled_files: Container[str],
) -> LastRun:
    """`previous` updated with what this run decided, keeping what it never reached.

    An id in `settled_ids` and a path in `settled_files` are ones this run has an answer for --
    a test that produced a result or was skipped, a file collection imported -- so anything
    `previous` said about them is replaced by `failed`/`errored`. Everything else is carried
    forward, which is what keeps a `--maxfail` stop, a Ctrl-C, or a run over one directory from
    erasing failures elsewhere in the suite.
    """
    kept_ids = (test_id for test_id in previous.failed if test_id not in settled_ids)
    kept_files = (path for path in previous.error_files if path not in settled_files)
    return LastRun(
        failed=tuple(sorted({*failed, *kept_ids})),
        error_files=tuple(sorted({*errored, *kept_files})),
    )


def ensure_gitignore(rootdir: Path) -> None:
    """Ignore `rootdir`'s cache directory from inside it, if it exists.

    Called on every exit path rather than only from `save`, since the assertion rewriter writes
    its bytecode into the same directory: a run that collects but never executes still leaves it
    behind, and a project must pick up no diff for having run velox.
    """
    directory = rootdir / CACHE_DIR_NAME
    if directory.is_dir():
        with contextlib.suppress(OSError):
            _write_gitignore(directory)


def _write_gitignore(directory: Path) -> None:
    """Left alone once written, in case the project edited it."""
    gitignore = directory / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_GITIGNORE, encoding="utf-8")


def _strings(value: object) -> tuple[str, ...]:
    """The strings in `value` if it is a list, else nothing -- the cache is a file on disk a
    user can edit, so its every field is read as untrusted."""
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))
