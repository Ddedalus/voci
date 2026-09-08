"""The collection index under `rootdir/.voci_cache`: what a file collected last time it was
actually imported, kept as long as its `(mtime_ns, size)` haven't changed since.

Written alongside the run cache (`voci._cache`) at the end of any run that collected for real.
Read at the start of a run asking for `--collect-only` (`cli.main`): when every file that run
would discover still matches its entry, the run answers straight from here, with none of them
imported -- `spec/03-discovery-and-collection.md` §6's "the cache only ever orders and predicts,
never skips a test" rule, applied to a request that was never going to run one in the first place.
A file whose entry is missing or stale falls back to a real `collect()`, same as any run that
isn't `--collect-only` always does.

Nothing here is proactively invalidated: a stale entry costs a future `--collect-only` one
avoidable import, never a wrong report -- `answer` compares `(mtime_ns, size)` against the live
file before trusting an entry at all, so leaving a stale one in place is always safe and only ever
wasteful.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from voci._cache import CACHE_DIR_NAME
from voci._collection.collect import CollectionResult, display_path
from voci._collection.requires import package_inits

__all__ = ["EMPTY", "Answer", "FileEntry", "Index", "answer", "load", "refresh", "save"]

_FILE_NAME = "collection.json"

#: Bumped when the payload's shape changes, exactly like `voci._cache`'s own -- a file written
#: by any other version is discarded whole rather than read defensively field by field.
_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One file's shape, and the stat it was collected under.

    `ids`/`lines` are the runnable tests this file produced, in collection order, paired
    position for position; `skipped` is the `(id, reason)` pairs of the tests it excluded with a
    skip mark, in the order `--collect-only` prints them. A file that failed to collect, or sits
    under a package `__init__.py` that did, has no entry at all -- see `refresh`.
    """

    mtime_ns: int
    size: int
    ids: tuple[str, ...]
    lines: tuple[int, ...]
    skipped: tuple[tuple[str, str], ...]


Index = Mapping[str, FileEntry]
"""Rootdir-relative path (as `str`, matching `voci._cache`'s own keys) -> what it collected."""

EMPTY: Index = {}


@dataclass(frozen=True, slots=True)
class Answer:
    """What a fully-fresh index says `--collect-only` would find, in file order.

    `paths`/`lines` are parallel to `ids` -- the file each test was collected from (rootdir-
    relative, same form `display_path` gives a real collection's own `TestRecord.path`) and the
    line it's defined at. `skipped_paths` is the same pairing for `skipped`. Both exist for
    `--co-json`, which is the first consumer to need a location rather than just an id -- the
    plain-text `--collect-only` report never prints one.
    """

    ids: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]
    paths: tuple[str, ...]
    lines: tuple[int, ...]
    skipped_paths: tuple[str, ...]


def load(rootdir: Path) -> Index:
    """`rootdir`'s index, or `EMPTY` if there is none, it's unreadable, or its version doesn't
    match -- the same tolerance `_cache.load` gives its own file, for the same reason: this is a
    file on disk a user (or a stray edit, or a `git clean` that missed the gitignore) can put
    anything into. A malformed individual entry is dropped on its own rather than discarding the
    whole file, since one file's stale answer says nothing about the rest.
    """
    path = rootdir / CACHE_DIR_NAME / _FILE_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return EMPTY
    if not isinstance(payload, dict) or payload.get("version") != _SCHEMA_VERSION:
        return EMPTY
    files = payload.get("files")
    if not isinstance(files, dict):
        return EMPTY
    index: dict[str, FileEntry] = {}
    for relpath, raw in files.items():
        if not isinstance(relpath, str):
            continue
        entry = _entry_of(raw)
        if entry is not None:
            index[relpath] = entry
    return index


def save(rootdir: Path, index: Index) -> None:
    """Replace `rootdir`'s index with `index`, atomically -- same mechanics as `_cache.save`
    (a per-process temporary file, `os.replace`d into place), and the same swallow-every-OSError
    policy: an unwritable tree costs a later `--collect-only` its shortcut, not this run its exit
    code.
    """
    directory = rootdir / CACHE_DIR_NAME
    payload = {
        "version": _SCHEMA_VERSION,
        "files": {
            relpath: {
                "mtime_ns": entry.mtime_ns,
                "size": entry.size,
                "ids": list(entry.ids),
                "lines": list(entry.lines),
                "skipped": [list(pair) for pair in entry.skipped],
            }
            for relpath, entry in index.items()
        },
    }
    temporary = directory / f"{_FILE_NAME}.{os.getpid()}"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
        os.replace(temporary, directory / _FILE_NAME)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


def answer(index: Index, files: Iterable[Path], *, rootdir: Path) -> Answer | None:
    """What `--collect-only` would print for `files`, read straight off `index` -- or `None` if
    any one of them is missing or stale, meaning a real `collect()` is unavoidable this time.

    All-or-nothing rather than file-by-file: a real collection has already paid its setup cost
    (the assertion-rewrite hook, the `sys.path` mutation, everything `cli.main` arranges ahead of
    `collect()`) the moment it needs to import even one file, so answering some files from the
    index while importing the rest would spend that cost anyway for a saving `collect()` already
    gives for free. Kept this simple until a suite where the partial case is measured to matter.
    """
    resolved_rootdir = Path(rootdir).resolve()
    ids: list[str] = []
    skipped: list[tuple[str, str]] = []
    paths: list[str] = []
    lines: list[int] = []
    skipped_paths: list[str] = []
    for path in files:
        relpath = str(display_path(path, resolved_rootdir))
        entry = index.get(relpath)
        stat = _stat_of(path)
        if entry is None or stat is None or (entry.mtime_ns, entry.size) != stat:
            return None
        ids.extend(entry.ids)
        skipped.extend(entry.skipped)
        paths.extend([relpath] * len(entry.ids))
        lines.extend(entry.lines)
        skipped_paths.extend([relpath] * len(entry.skipped))
    return Answer(
        ids=tuple(ids),
        skipped=tuple(skipped),
        paths=tuple(paths),
        lines=tuple(lines),
        skipped_paths=tuple(skipped_paths),
    )


def refresh(
    previous: Index,
    result: CollectionResult,
    *,
    rootdir: Path,
    files: Iterable[Path],
    discovered: Iterable[Path],
    roots: Iterable[Path],
) -> Index:
    """`previous`, updated with what this run's `collect()` (over `files`, producing `result`)
    found.

    A file in `files` that collected without error gets a fresh entry. One that errored, or sits
    under a package `__init__.py` that did, loses whatever entry it had instead -- a stale
    success sitting beside the collection error this run just reported would tell a later
    `--collect-only` the file is fine. A file `discovered` no longer produces, under one of
    `roots`, is dropped the same way `_lastfailed.dead_paths` drops a recorded failure nothing
    will ever settle again. Everything else -- a file outside every root this run walked, or one
    `files` itself never reached (an `--lf` narrowing) -- is carried forward untouched: `answer`
    re-checks its stat before ever trusting it, so an entry this run had no reason to revisit is
    never wrong to keep.
    """
    resolved_rootdir = Path(rootdir).resolve()
    error_paths = {str(error.path) for error in result.errors}
    blocked: set[str] = set()
    for path in files:
        relpath = str(display_path(path, resolved_rootdir))
        if relpath in error_paths:
            continue
        if any(
            str(display_path(init, resolved_rootdir)) in error_paths
            for init in package_inits(path, rootdir)
        ):
            blocked.add(relpath)
    dead = error_paths | blocked

    updated = dict(previous)
    walked = tuple(Path(root).resolve() for root in roots)
    discovered_relpaths = {str(display_path(path, resolved_rootdir)) for path in discovered}
    for relpath in list(updated):
        absolute = resolved_rootdir / relpath
        if relpath not in discovered_relpaths and any(
            absolute.parent.is_relative_to(root) for root in walked
        ):
            del updated[relpath]
    for relpath in dead:
        updated.pop(relpath, None)
    for relpath, entry in _fresh_entries(result, rootdir=rootdir, files=files, skip=dead):
        updated[relpath] = entry
    return updated


def _fresh_entries(
    result: CollectionResult, *, rootdir: Path, files: Iterable[Path], skip: set[str]
) -> Iterable[tuple[str, FileEntry]]:
    """One `(relpath, FileEntry)` pair per file in `files` that isn't in `skip`, built from
    `result` and each file's current stat. A file that has since vanished from disk (removed
    mid-run) contributes nothing -- there is no stat to record it under."""
    resolved_rootdir = Path(rootdir).resolve()
    ids_by_path: dict[str, list[str]] = {}
    lines_by_path: dict[str, list[int]] = {}
    skipped_by_path: dict[str, list[tuple[str, str]]] = {}
    for record in result.records:
        relpath = str(record.path)
        ids_by_path.setdefault(relpath, []).append(record.id)
        lines_by_path.setdefault(relpath, []).append(record.lineno)
    for entry in result.skipped:
        relpath = str(entry.path)
        skipped_by_path.setdefault(relpath, []).append((entry.id, entry.reason))

    for path in files:
        relpath = str(display_path(path, resolved_rootdir))
        if relpath in skip:
            continue
        stat = _stat_of(path)
        if stat is None:
            continue
        mtime_ns, size = stat
        yield (
            relpath,
            FileEntry(
                mtime_ns=mtime_ns,
                size=size,
                ids=tuple(ids_by_path.get(relpath, ())),
                lines=tuple(lines_by_path.get(relpath, ())),
                skipped=tuple(skipped_by_path.get(relpath, ())),
            ),
        )


def _stat_of(path: Path) -> tuple[int, int] | None:
    """`(mtime_ns, size)` for `path`, or `None` if it can't be stat'd -- gone, a broken symlink,
    a permission error."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _entry_of(raw: object) -> FileEntry | None:
    """`raw` read back as a `FileEntry`, or `None` if it isn't shaped like one -- untrusted input,
    same as every field `_cache.load` reads."""
    if not isinstance(raw, dict):
        return None
    mtime_ns, size = raw.get("mtime_ns"), raw.get("size")
    ids, lines, skipped = raw.get("ids"), raw.get("lines"), raw.get("skipped")
    if not isinstance(mtime_ns, int) or not isinstance(size, int):
        return None
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        return None
    if not isinstance(lines, list) or len(lines) != len(ids):
        return None
    if not all(isinstance(item, int) for item in lines):
        return None
    if not isinstance(skipped, list):
        return None
    pairs: list[tuple[str, str]] = []
    for item in skipped:
        if not (
            isinstance(item, list)
            and len(item) == 2
            and isinstance(item[0], str)
            and isinstance(item[1], str)
        ):
            return None
        pairs.append((item[0], item[1]))
    return FileEntry(
        mtime_ns=mtime_ns, size=size, ids=tuple(ids), lines=tuple(lines), skipped=tuple(pairs)
    )
