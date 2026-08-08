"""Discovery: the walk that finds candidate test files (spec/03 §2).

M0 scope: walk + filename filter + directory ignore set, nothing else. No collection cache
(spec/03 §6, roadmap) and no `-k`/`-m` predicates — those act on `TestRecord`, once one exists
(spec/03 §4 step 5), not on paths.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable
from pathlib import Path

__all__ = ["DEFAULT_IGNORE_DIRS", "DEFAULT_TEST_FILE_PATTERNS", "discover_files"]

#: spec/02 §3 `[tool.velox] test_file_patterns` default.
DEFAULT_TEST_FILE_PATTERNS: tuple[str, ...] = ("test_*.py", "*_test.py")

#: spec/02 §3 `[tool.velox] ignore` default.
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


# Review: "root by root" walk order makes logical order a function of argv order, not of the
# test set. spec/03 §4 assigns `index` over "per-file records sorted by relative path", and I2
# wants two runs of the same test set to be byte-identical. `velox tests/b tests/a` and
# `velox tests/a tests/b` are the same test set but yield different indices, different execution
# order and different report order. One sort of the concatenation (here or in `collect`) costs
# nothing at M0 scale and closes the deviation before anything depends on today's behaviour.
def discover_files(
    roots: Iterable[Path],
    *,
    patterns: Iterable[str] = DEFAULT_TEST_FILE_PATTERNS,
    ignore_dirs: frozenset[str] = DEFAULT_IGNORE_DIRS,
) -> list[Path]:
    """One walk per root in `roots`, filtered to test files (spec/03 §2).

    - An entry in `roots` that is already a file is taken as-is, no pattern check against it —
      "if PATHS contains explicit files or ids, the walk is skipped for those entries" (spec/03
      §2). A directory is walked with `os.scandir`; entries named in `ignore_dirs` are pruned
      without descending into them.
    - Symlink loops are avoided by tracking visited `(st_dev, st_ino)` pairs.
    - Directory results are sorted by name at each level so the walk itself is deterministic
      (spec/03 §2) before `collect` does any further sorting.

    Returns absolute paths, in walk order (root by root, each root depth-first, name-sorted).
    This is *not* logical order — `collect` derives that from `(path, lineno)` once functions are
    found inside these files.
    """
    patterns = tuple(patterns)
    visited: set[tuple[int, int]] = set()
    found: list[Path] = []
    # Review: results are never de-duplicated, so the same file can be collected and run twice.
    # `visited` only guards directories. `discover_files([p, p])` and `discover_files([dir,
    # dir/test_x.py])` both return the file twice (verified) — `collect` then imports it twice
    # and emits two `TestRecord`s with the *same* `id` and different `index`, which breaks the
    # "id is unique" assumption every downstream consumer (`--deselect`, `-k`, JUnit, `--lf`)
    # will need. A `dict.fromkeys`-style dedupe on the resolved path fixes it here.
    for root in roots:
        root = Path(root).resolve()
        if root.is_file():
            # No pattern check: an explicit file is taken as-is, matching pytest's own "you
            # named it, you get it" behavior for explicit paths/ids.
            found.append(root)
            continue
        if not root.is_dir():
            # Review: this docstring promise is unkept — nobody downstream complains.
            # `collect` never sees the root (it only gets files) and `cli.main` doesn't check
            # either, so `velox /typo/path` prints "0 tests" and exits 5 (verified), i.e. a
            # typo'd path is indistinguishable from a genuinely empty suite. Per spec/02 §4
            # that is a usage error (4), and per I8 a selector that matched nothing must be
            # named. Either return the unusable roots or validate them in `main`.
            # Doesn't exist (or is some other kind of entry, e.g. a broken symlink) — nothing
            # to walk. Left for `collect`/the caller to complain about, not this function.
            continue
        found.extend(_walk(root, patterns, ignore_dirs, visited))
    return found


def _walk(
    directory: Path,
    patterns: tuple[str, ...],
    ignore_dirs: frozenset[str],
    visited: set[tuple[int, int]],
) -> list[Path]:
    """Depth-first, name-sorted walk of one directory, already known to exist."""
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
        # Review: the scandir iterator is never closed on the error path — `sorted` only frees
        # the fd when it runs to exhaustion, so an OSError raised mid-iteration (deleted dir,
        # EACCES on a network mount) leaks the directory handle until GC. `with os.scandir(...)
        # as it: entries = sorted(it, ...)` is the same code with the leak closed.
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
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
