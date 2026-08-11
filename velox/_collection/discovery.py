"""Discovery: the walk that finds candidate test files.

One walk from each given root, filtered by filename pattern and a set of ignored directory names,
returning paths in a stable order.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable
from pathlib import Path

__all__ = ["DEFAULT_IGNORE_DIRS", "DEFAULT_TEST_FILE_PATTERNS", "discover_files"]

#: `[tool.velox] test_file_patterns` default.
DEFAULT_TEST_FILE_PATTERNS: tuple[str, ...] = ("test_*.py", "*_test.py")

#: `[tool.velox] ignore` default.
DEFAULT_IGNORE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        "node_modules",
        ".mypy_cache",
        ".ruff_cache",
        "build",
        "dist",
    }
)


def discover_files(
    roots: Iterable[Path],
    *,
    patterns: Iterable[str] = DEFAULT_TEST_FILE_PATTERNS,
    ignore_dirs: frozenset[str] = DEFAULT_IGNORE_DIRS,
) -> list[Path]:
    """One walk per root in `roots`, filtered to test files.

    - An entry in `roots` that is already a file is taken as-is, with no pattern check against
      it. A directory is walked with `os.scandir`; entries named in `ignore_dirs` are pruned
      without descending into them. A root that doesn't exist contributes nothing here — this
      function has no way to tell "typo'd path" from "a genuinely empty selection" apart, and
      shouldn't guess; `cli.main` validates `PATHS` before any root reaches this function, so a
      bad explicit path is already a usage error by the time discovery would see it.
    - Symlink loops are avoided by tracking visited `(st_dev, st_ino)` pairs.
    - The concatenation across all roots is de-duplicated (overlapping/duplicate roots would
      otherwise hand `collect` the same file twice, producing two `TestRecord`s sharing one id)
      and sorted, so the result — and therefore `collect`'s `index` assignment — is a function of
      the resolved test set, not of `roots`' argv order.

    Returns absolute paths.
    """
    patterns = tuple(patterns)
    visited: set[tuple[int, int]] = set()
    found: list[Path] = []
    for root in roots:
        root = Path(root).resolve()
        if root.is_file():
            # No pattern check: an explicit file is taken as-is, matching pytest's own "you
            # named it, you get it" behavior for explicit paths/ids.
            found.append(root)
            continue
        if not root.is_dir():
            # Doesn't exist (or is some other kind of entry, e.g. a broken symlink) — nothing
            # to walk. See the docstring above for who is responsible for flagging this.
            continue
        found.extend(_walk(root, patterns, ignore_dirs, visited))
    return sorted(dict.fromkeys(found))


def _walk(
    directory: Path,
    patterns: tuple[str, ...],
    ignore_dirs: frozenset[str],
    visited: set[tuple[int, int]],
) -> list[Path]:
    """Depth-first, name-sorted walk of one directory, already known to exist.

    Name-sorted here purely so two calls on an unchanged tree agree with each other file-for-
    file while walking (helpful for debugging and for `visited` order); `discover_files` sorts
    the full concatenation again regardless, so this level's sort is not the source of the
    guarantee callers depend on.
    """
    try:
        st = directory.stat()
    except OSError:
        return []
    key = (st.st_dev, st.st_ino)
    if key in visited:
        # Either a symlink cycle, or two roots/paths that resolve to the same directory —
        # either way, walking it again would duplicate work at best and loop forever at worst.
        return []
    visited.add(key)

    try:
        # `with`, not a bare call: `sorted()` only drains (and thus closes) the scandir iterator
        # on success. An `OSError` raised mid-iteration (directory removed underneath us, EACCES
        # on a network mount) would otherwise leak the open directory handle until GC catches it.
        with os.scandir(directory) as it:
            entries = sorted(it, key=lambda entry: entry.name)
    except OSError:
        return []

    results: list[Path] = []
    for entry in entries:
        try:
            is_dir = entry.is_dir(follow_symlinks=True)
        except OSError:
            # A broken symlink or a permission error on stat — not a file we can collect from
            # and not a directory we can descend into.
            continue
        if is_dir:
            if entry.name in ignore_dirs:
                continue
            results.extend(_walk(Path(entry.path), patterns, ignore_dirs, visited))
        elif _matches(entry.name, patterns):
            results.append(Path(entry.path))
    return results


def _matches(name: str, patterns: Iterable[str]) -> bool:
    """Whether `name` (a bare filename) matches any of `patterns` (`fnmatch` globs)."""
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)
