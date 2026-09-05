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

__all__ = ["CACHE_DIR_NAME", "NOTHING_RECORDED", "LastRun", "load", "merge", "save"]

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


def _write_gitignore(directory: Path) -> None:
    """Ignore the cache directory from inside it, so a project picks up no diff for having run
    velox once. Left alone once written, in case the project edited it."""
    gitignore = directory / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_GITIGNORE, encoding="utf-8")


def _strings(value: object) -> tuple[str, ...]:
    """The strings in `value` if it is a list, else nothing -- the cache is a file on disk a
    user can edit, so its every field is read as untrusted."""
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))
