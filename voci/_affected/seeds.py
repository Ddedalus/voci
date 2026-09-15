"""Turns one test's finished `CollectorRecord` into `resolve.World.closure`'s seed keys, at
session end. The actual `(filename, qualname) -> DefKey | NameKey` mapping is `World.resolve_code`;
this module is the thin per-record driver on top of it, plus the one rule that isn't a per-code
lookup at all: a record naming a file that changed mid-run is dropped outright rather than
resolved against stale qualnames.

Turning the returned seeds into a stored, checksummed dependency set -- calling `World.closure`
over them, keying by test, pruning by LRU -- is `store.py`'s job, not built yet.
"""

from __future__ import annotations

from pathlib import Path

from voci._affected.collector import CollectorRecord
from voci._affected.resolve import DefKey, NameKey, World

__all__ = ["seeds_for_record"]


def seeds_for_record(
    world: World, record: CollectorRecord, *, changed_paths: frozenset[Path] = frozenset()
) -> frozenset[DefKey | NameKey] | None:
    """The seed keys `record`'s traced `(filename, qualname)` pairs resolve to in `world`, or
    `None` if `record` should be dropped instead of stored: it names a path in `changed_paths`,
    meaning that file was edited *during* the run `record` came from, so the qualnames it recorded
    may no longer correspond to what `world` -- built from the tree as it stands now -- parses
    there at all. This is the same rule `--watch`'s own mid-run stat guard applies elsewhere (the
    plan's "A change during a run": "its mid-run stat guard drops the records of files that
    moved"), applied here per record rather than per iteration.

    `world` should be built from the tree's *current* source, since that's what the returned
    seeds are checksummed against next -- not, say, the tree as it stood when `record`'s test
    started, which `resolve_code` has no way to reconstruct from a bare `(filename, qualname)`
    pair regardless.

    An untrusted record (`record.untrusted is not None`) isn't special-cased here: it still
    resolves to seeds like any other, since Selection's own "always selected" rule is what keeps
    an untrusted test running regardless of what its dependencies say, not a reason to skip
    computing them.
    """
    seeds: set[DefKey | NameKey] = set()
    for filename, qualname in record.codes:
        path = Path(filename)
        if path in changed_paths:
            return None
        seeds |= world.resolve_code(path, qualname)
    return frozenset(seeds)
