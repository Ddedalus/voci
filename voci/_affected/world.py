"""Building a `resolve.World` over the whole first-party tree, the way the session-start/end
driver needs one -- not just the test files `_collection.discovery` walks, since a test's closure
can reach any first-party module.

Every candidate `discover_files` turns up is then filtered through `tracer.is_first_party`, the
exact criterion a running `Tracer` classifies code by -- so a `CollectorRecord`'s `(filename,
qualname)` pairs always resolve against a `World` built from the same file set the tracer would
have attributed code to. `discover_files`'s own `DEFAULT_IGNORE_DIRS` prunes the common cases by
name (`.venv`, `__pycache__`, ...) before ever touching the filesystem for what's inside them, but
it has no notion of a venv under some other name (this very checkout's own `.venv-3.13`, `.python-
version`'s second interpreter) -- `is_first_party`'s own `pyvenv.cfg` walk is what actually rules
those out, so nothing here relies on the ignore list alone to be complete.
"""

from __future__ import annotations

import tokenize
from pathlib import Path

from voci._affected.resolve import World, dotted_name_for
from voci._affected.tracer import is_first_party
from voci._collection.discovery import discover_files

__all__ = ["build_world", "first_party_files"]


def first_party_files(rootdir: Path) -> list[Path]:
    """Every first-party `.py` file under `rootdir`, resolved and sorted -- `discover_files`'s own
    walk, narrowed by `is_first_party` rather than by `discover_files`'s own `ignore_dirs` alone
    (see the module docstring)."""
    resolved_rootdir = rootdir.resolve()
    candidates = discover_files([resolved_rootdir], patterns=("*.py",))
    return [path for path in candidates if is_first_party(str(path), resolved_rootdir)]


def build_world(rootdir: Path) -> tuple[World, dict[Path, tuple[str, str]]]:
    """A `World` over every first-party file under `rootdir`, plus the `path -> (dotted, source)`
    mapping it was built from -- `store.checksums`' own `files` argument, so a caller doesn't need
    to rebuild it separately.

    A file `_read_source` can't decode is left out of both -- the same "must never be the reason a
    run errors" rule the store's own location fallback follows (Storage: "If git is missing or
    fails, fall back to `.voci_cache/`; never error"). A reference into a dropped file falls back
    to whole-module the same way an unresolvable one always does (`World`'s rule 4); it just never
    resolves to source at all, rather than resolving to source that changed.
    """
    resolved_rootdir = rootdir.resolve()
    files: dict[Path, tuple[str, str]] = {}
    for path in first_party_files(resolved_rootdir):
        source = _read_source(path)
        if source is None:
            continue
        files[path] = (dotted_name_for(path, resolved_rootdir), source)
    return World(files), files


def _read_source(path: Path) -> str | None:
    """`path`'s decoded text, honoring a PEP 263 encoding cookie the way Python itself would --
    `tokenize.open` rather than a plain `path.read_text()`, since a first-party file is under no
    obligation to be UTF-8. `None` for anything that can't be read or decoded at all: gone between
    discovery and here, a permission error, or bytes that aren't valid under the encoding they
    themselves declare."""
    try:
        with tokenize.open(path) as f:
            return f.read()
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
